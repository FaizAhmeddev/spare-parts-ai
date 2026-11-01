"""Analytical core: from a Dataset to a full inventory policy per part-site.
Re-used by the live pipeline and by the historical backtest."""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..forecasting.engine import run_forecasting
from ..inventory.policy import compute_policy
from . import drivers
from .classification import classify
from .demand import KEY, long_monthly, monthly_matrix, month_index, universe

log = logging.getLogger(__name__)


def _lookup(keys: pd.DataFrame, series: pd.Series, default=0.0) -> np.ndarray:
    idx = pd.MultiIndex.from_frame(keys[KEY])
    return np.array([series.get(k, default) for k in idx])


def stockout_hours_by_ved(ds, cfg) -> dict:
    so = ds["stockouts"]
    default = float(cfg.get("costs.default_downtime_hours_per_stockout", 8))
    if so.empty:
        return {"V": default, "E": default, "D": default, "_all": default}
    # median is robust to the few multi-week outages in the log
    g = so.groupby("part_criticality_ved")["downtime_hours_due_to_stockout"].median().to_dict()
    g["_all"] = so["downtime_hours_due_to_stockout"].median()
    return g


def analyze(ds, as_of: pd.Timestamp, cfg, use_ml: bool = True, use_pm: bool = True,
            use_reliability: bool = True, duplicates: pd.DataFrame | None = None) -> dict:
    months = month_index(as_of, int(cfg.get("analysis.history_months", 36)))
    keys = universe(ds)
    cons = ds["consumption"][ds["consumption"]["txn_date"] <= as_of]
    total = monthly_matrix(cons, months, keys)
    unplanned = monthly_matrix(cons, months, keys, unplanned_only=True) if use_pm else total
    log.info("Demand matrix: %d part-sites x %d months", *total.shape)

    occ = drivers.pm_occurrences(ds, as_of, 365) if use_pm else \
        pd.DataFrame(columns=KEY + ["pm_plan_id", "asset_id", "due_date", "qty"])
    planned_12m = drivers.planned_qty_within(occ, as_of, 365) if use_pm else pd.Series(dtype=float)

    cls = classify(total, ds, as_of, cfg, planned_12m)
    tot12 = total.values[:, -12:].sum(axis=1)
    unp12 = unplanned.values[:, -12:].sum(axis=1)
    cls["unplanned_share"] = np.where(tot12 > 0, unp12 / np.where(tot12 > 0, tot12, 1), 1.0)
    fc_long, choice, _, metrics = run_forecasting(unplanned, ds, cfg, use_ml=use_ml)
    lt = drivers.lead_time_stats(ds)

    base = cls.merge(choice, on=KEY, how="left").merge(lt, on="part_id", how="left")
    base["lt_mean_days"] = base["lt_mean_days"].fillna(30)
    base["lt_std_days"] = base["lt_std_days"].fillna(7)
    base["lt_observations"] = base["lt_observations"].fillna(0)

    # ---- PM calibration: not every PM task actually consumes the part
    # (inspections, condition-based swaps). Learn the ratio of preventive
    # issues to scheduled requirement per part-site, shrunk to the global ratio.
    pm_ratio = pd.Series(1.0, index=pd.MultiIndex.from_frame(base[KEY]))
    if use_pm and len(occ):
        sched = _lookup(base, planned_12m)
        prev12 = (total.values[:, -12:] - unplanned.values[:, -12:]).sum(axis=1)
        prev12 = pd.Series(prev12, index=total.index).reindex(pd.MultiIndex.from_frame(base[KEY])).fillna(0).values
        g = float(np.clip(prev12[sched > 0].sum() / max(sched.sum(), 1e-9), 0.05, 1.0))
        k = float(cfg.get("forecasting.pm_ratio_prior_weight", 4))
        ratio = np.clip((prev12 + k * g) / (sched + k), 0.0, 1.0)
        pm_ratio[:] = np.where(sched > 0, ratio, 1.0)
        log.info("PM consumption ratio: global %.2f", g)
    base["pm_consumption_ratio"] = pm_ratio.values

    review = float(cfg.get("inventory_policy.review_period_days", 7))
    prot = pd.Series((base["lt_mean_days"] + review).values,
                     index=pd.MultiIndex.from_frame(base[KEY]))
    base["planned_12m_scheduled"] = _lookup(base, planned_12m)
    base["planned_12m"] = base["planned_12m_scheduled"] * base["pm_consumption_ratio"]
    base["planned_in_pp"] = (_lookup(base, drivers.planned_qty_within(occ, as_of, prot))
                             * base["pm_consumption_ratio"]) if use_pm else 0.0

    # Reliability-driven demand
    rel_pp = drivers.reliability_demand(ds, as_of, prot, cfg).set_index(KEY)
    rel_yr = drivers.reliability_demand(ds, as_of, 365, cfg).set_index(KEY)
    base["rel_failures_in_pp"] = _lookup(base, rel_pp["expected_failures"]) if use_reliability else 0.0
    base["rel_failures_12m"] = _lookup(base, rel_yr["expected_failures"])
    base["n_assets"] = _lookup(base, rel_yr["n_assets"], 0).astype(int)
    base["installed_qty"] = _lookup(base, rel_yr["installed_qty"], 0)
    base["critical_fitted"] = _lookup(base, rel_yr["critical_fitted"], False).astype(bool)
    base["asset_criticality"] = np.where(_lookup(base, rel_yr["on_critical_asset"], False).astype(bool), "A", "")
    base["max_life_used_pct"] = _lookup(base, rel_yr["max_life_used_pct"], 0.0)
    dt_cost = _lookup(base, rel_yr["median_downtime_cost_hr"], np.nan)

    # Cost of one stockout = median downtime cost/hr of the assets using the
    # part x average downtime hours per stockout (learned from the log).
    hrs = stockout_hours_by_ved(ds, cfg)
    so = ds["stockouts"][ds["stockouts"]["event_date"] <= as_of]
    site_avg_cost = ds["assets"].groupby("site_id")["downtime_cost_per_hour_usd"].median()
    dt_cost = np.where(np.isnan(dt_cost), base["site_id"].map(site_avg_cost).fillna(0) * 0.25, dt_cost)
    base["downtime_cost_per_hr"] = dt_cost
    base["stockout_cost_usd"] = dt_cost * base["criticality_ved"].map(hrs).fillna(hrs["_all"])
    p_any_fail = 1 - np.exp(-base["rel_failures_12m"])
    base["insurance_risk_usd"] = p_any_fail * base["stockout_cost_usd"]
    so12 = so[so["event_date"] > as_of - pd.DateOffset(months=12)].groupby(KEY).size()
    base["stockout_events_12m"] = _lookup(base, so12, 0)

    # Inventory snapshot
    inv = ds["inventory"][KEY + ["on_hand_qty", "reserved_qty", "on_order_qty", "min_level",
                                 "max_level", "reorder_point", "stock_value_usd"]]
    base = base.merge(inv, on=KEY, how="left")
    for c in ["on_hand_qty", "reserved_qty", "on_order_qty", "min_level", "max_level",
              "reorder_point", "stock_value_usd"]:
        base[c] = base[c].fillna(0)
    base["stocked_here"] = base["max_level"] > 0
    base["min_order_qty"] = base["part_id"].map(
        ds["part_master"].set_index("part_id")["min_order_qty"]).fillna(1)
    base["available_qty"] = (base["on_hand_qty"] - base["reserved_qty"]).clip(lower=0)
    base["duplicate_code_stock"] = 0.0
    if duplicates is not None and len(duplicates):
        dmap = dict(zip(duplicates["duplicate_part_id"], duplicates["master_part_id"]))
        extra = (base[base["part_id"].isin(dmap)]
                 .assign(master=lambda d: d["part_id"].map(dmap))
                 .groupby(["master", "site_id"])["available_qty"].sum())
        base["duplicate_code_stock"] = [extra.get((p, s), 0.0) for p, s in zip(base["part_id"], base["site_id"])]
    base["inventory_position"] = base["available_qty"] + base["on_order_qty"] + base["duplicate_code_stock"]

    pol = compute_policy(base, cfg)

    # Forecast table: unplanned + planned by month
    fut_months = pd.PeriodIndex(sorted(fc_long["month"].unique()), freq="M")
    pm_m = drivers.planned_monthly(occ, fut_months)
    if len(pm_m):
        pm_m["month"] = pm_m["month"].astype(str)
        fc_long = fc_long.merge(pm_m, on=KEY + ["month"], how="left")
    else:
        fc_long["planned_qty"] = 0.0
    fc_long["planned_qty"] = fc_long["planned_qty"].fillna(0)
    fc_long["planned_qty"] *= [pm_ratio.get((p, s_), 1.0) for p, s_ in zip(fc_long["part_id"], fc_long["site_id"])]
    fc_long["total_fc"] = fc_long["unplanned_fc"] + fc_long["planned_qty"]

    hist = long_monthly(total, "qty").merge(long_monthly(unplanned, "unplanned_qty"),
                                           on=KEY + ["month"])
    hist = hist[hist["qty"] > 0]   # sparse storage

    rel_detail = rel_yr.reset_index()
    return {"policy": pol, "forecast": fc_long, "monthly_demand": hist,
            "model_metrics": metrics, "reliability": rel_detail, "months": months}
