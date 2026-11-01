"""Policy backtest ("what if we had used SpareAI a year ago?").

1. Truncate all data at a cutoff date (as_of - N months) - no look-ahead.
2. Re-run the analytics on the truncated data to get the AI policy.
3. Replay the actual daily consumption of the following N months through
   two inventory simulations: the AI policy vs the current (legacy) min/max.
4. Compare service (fill rate, stockouts, downtime) and capital (average
   inventory value), and check how many *real* logged stockouts the AI
   policy would have had stock for.

Simplifications (documented for reviewers): both policies start at their own
Max level, lead times are the learned mean, unmet demand is lost (it is
expedited in reality), PM schedules are not reconstructable for the past so
the backtest forecasts total demand.
"""
from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd

from ..analytics.core import analyze
from ..ingestion.loader import Dataset

log = logging.getLogger(__name__)
KEY = ["part_id", "site_id"]


def truncate(ds: Dataset, cutoff: pd.Timestamp) -> Dataset:
    t = {k: v.copy() for k, v in ds.tables.items()}
    t["consumption"] = t["consumption"][t["consumption"]["txn_date"] <= cutoff]
    t["work_orders"] = t["work_orders"][t["work_orders"]["created_date"] <= cutoff]
    t["stockouts"] = t["stockouts"][t["stockouts"]["event_date"] <= cutoff]
    po = t["purchase_orders"]
    po = po[po["order_date"] <= cutoff].copy()
    late = po["received_date"] > cutoff
    po.loc[late, ["received_date", "actual_lead_time_days"]] = np.nan
    t["purchase_orders"] = po
    return Dataset(tables=t, source=ds.source + f" (cut {cutoff.date()})")


def simulate(demand: np.ndarray, rop: np.ndarray, maxv: np.ndarray, moq: np.ndarray,
             lt: np.ndarray, start: np.ndarray) -> dict[str, np.ndarray]:
    """Daily (s,S) simulation vectorised over all part-sites."""
    K, T = demand.shape
    horizon = T + int(lt.max()) + 2
    arrivals = np.zeros((K, horizon))
    stock = start.astype(float).copy()
    on_order = np.zeros(K)
    stock_start_of_day = np.zeros((K, T))
    filled = np.zeros(K)
    short_events = np.zeros(K)
    short_units = np.zeros(K)
    inv_sum = np.zeros(K)
    orders = np.zeros(K)
    bought = np.zeros(K)
    active = maxv > 0
    moq = np.maximum(moq, 1)
    for t in range(T):
        stock += arrivals[:, t]
        on_order -= arrivals[:, t]
        stock_start_of_day[:, t] = stock
        d = demand[:, t]
        f = np.minimum(stock, d)
        stock -= f
        filled += f
        s = d - f
        short_events += s > 0
        short_units += s
        ip = stock + on_order
        trig = active & (ip <= rop)
        if trig.any():
            q = np.maximum(maxv - ip, moq)
            q = np.ceil(q / moq) * moq
            q = np.where(trig, q, 0)
            on_order += q
            orders += trig
            bought += q
            idx = np.flatnonzero(trig)
            arrivals[idx, t + lt[idx]] += q[idx]
        inv_sum += stock
    return {"filled": filled, "short_events": short_events, "short_units": short_units,
            "avg_stock": inv_sum / T, "end_stock": stock, "orders": orders,
            "bought": bought, "stock_by_day": stock_start_of_day}


def run_policy_backtest(ds: Dataset, as_of: pd.Timestamp, cfg, use_ml: bool = True):
    n_months = int(cfg.get("analysis.policy_backtest_months", 12))
    cutoff = (as_of - pd.DateOffset(months=n_months)).normalize()
    log.info("Policy backtest: cutoff %s, replay %d months", cutoff.date(), n_months)
    ds_cut = truncate(ds, cutoff)
    res = analyze(ds_cut, cutoff, cfg, use_ml=use_ml, use_pm=False, use_reliability=False)
    pol = res["policy"].set_index(KEY)

    inv = ds["inventory"].set_index(KEY)
    keys = inv.index
    days = pd.date_range(cutoff + pd.Timedelta(days=1), as_of, freq="D")
    cons = ds["consumption"]
    c = cons[(cons["txn_date"] > cutoff) & (cons["txn_date"] <= as_of)]
    dm = c.groupby(KEY + ["txn_date"])["qty_issued"].sum().unstack("txn_date")
    dm = dm.reindex(index=keys, columns=days).fillna(0.0)
    D = dm.values

    pol = pol.reindex(keys)
    lt = np.maximum(np.round(pol["lt_mean_days"].fillna(30).values), 1).astype(int)
    moq = pol["min_order_qty"].fillna(1).values
    cost = inv["unit_cost_usd"].fillna(0).values
    so_cost = pol["stockout_cost_usd"].fillna(0).values

    ai_rop = pol["rop_new"].fillna(0).values.astype(float)
    ai_max = pol["max_new"].fillna(0).values.astype(float)
    lg_rop = inv["reorder_point"].fillna(0).values.astype(float)
    lg_max = inv["max_level"].fillna(0).values.astype(float)

    sims = {"AI policy": simulate(D, ai_rop, ai_max, moq, lt, ai_max),
            "Current min/max": simulate(D, lg_rop, lg_max, moq, lt, lg_max)}
    demand_units = D.sum()
    rows = []
    detail = pd.DataFrame(index=keys).reset_index()
    for name, s in sims.items():
        rows.append({
            "policy": name,
            "fill_rate": s["filled"].sum() / demand_units if demand_units else 1.0,
            "stockout_events": int(s["short_events"].sum()),
            "parts_with_stockout": int((s["short_events"] > 0).sum()),
            "units_short": float(s["short_units"].sum()),
            "est_downtime_cost_usd": float((s["short_events"] * so_cost).sum()),
            "avg_inventory_value_usd": float((s["avg_stock"] * cost).sum()),
            "end_inventory_value_usd": float((s["end_stock"] * cost).sum()),
            "purchase_orders": int(s["orders"].sum()),
            "purchase_value_usd": float((s["bought"] * cost).sum()),
        })
        tag = "ai" if name.startswith("AI") else "legacy"
        detail[f"{tag}_short_events"] = s["short_events"]
        detail[f"{tag}_avg_inv_value"] = s["avg_stock"] * cost
    summary = pd.DataFrame(rows)
    detail["demand_units"] = D.sum(axis=1)

    # Validation against real stockout log
    so = ds["stockouts"]
    so = so[(so["event_date"] > cutoff) & (so["event_date"] <= as_of)].copy()
    pos = {k: i for i, k in enumerate(keys)}
    day_pos = {d: i for i, d in enumerate(days)}
    covered_ai, covered_lg = [], []
    for r in so.itertuples():
        i, j = pos.get((r.part_id, r.site_id)), day_pos.get(r.event_date)
        if i is None or j is None:
            covered_ai.append(False)
            covered_lg.append(False)
            continue
        covered_ai.append(bool(sims["AI policy"]["stock_by_day"][i, j] >= 1))
        covered_lg.append(bool(sims["Current min/max"]["stock_by_day"][i, j] >= 1))
    so["ai_had_stock"] = covered_ai
    so["legacy_had_stock"] = covered_lg
    so["ai_rop_at_cutoff"] = [pol["rop_new"].get((p, s), 0) for p, s in zip(so["part_id"], so["site_id"])]
    val = so[["stockout_id", "event_date", "part_id", "site_id", "part_criticality_ved",
              "downtime_hours_due_to_stockout", "downtime_cost_usd", "ai_rop_at_cutoff",
              "ai_had_stock", "legacy_had_stock"]]

    summary.attrs["cutoff"] = str(cutoff.date())
    log.info("Backtest:\n%s", summary.to_string(index=False))
    return summary, detail, val, cutoff
