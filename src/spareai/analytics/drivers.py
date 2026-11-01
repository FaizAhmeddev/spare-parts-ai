"""Demand and supply drivers beyond history:
- planned demand from preventive-maintenance schedules
- failure-based demand from asset reliability (Weibull)
- supplier lead-time distribution learned from purchase orders
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

KEY = ["part_id", "site_id"]


# ------------------------------------------------------------------ PM demand
def pm_occurrences(ds, as_of: pd.Timestamp, horizon_days: int) -> pd.DataFrame:
    """Explode active PM plans on active assets into dated part requirements."""
    mp = ds["maintenance_plans"]
    assets = ds["assets"].set_index("asset_id")["status"]
    mp = mp[(mp["plan_status"] == "Active") & (mp["asset_id"].map(assets) == "Active")]
    end = as_of + pd.Timedelta(days=horizon_days)
    rows = []
    for r in mp.itertuples():
        if pd.isna(r.next_due_date) or not r.frequency_days or r.frequency_days <= 0:
            continue
        due = max(r.next_due_date, as_of)          # overdue PMs are needed now
        while due <= end:
            rows.append((r.part_id, r.site_id, r.pm_plan_id, r.asset_id, due, r.qty_required))
            due = due + pd.Timedelta(days=int(r.frequency_days))
    return pd.DataFrame(rows, columns=KEY + ["pm_plan_id", "asset_id", "due_date", "qty"])


def planned_qty_within(occ: pd.DataFrame, as_of: pd.Timestamp, days: pd.Series | int) -> pd.Series:
    """Planned qty per part-site due within `days` (scalar or per part-site Series)."""
    if occ.empty:
        return pd.Series(dtype=float)
    o = occ.copy()
    o["days_out"] = (o["due_date"] - as_of).dt.days
    if isinstance(days, pd.Series):
        o["limit"] = pd.MultiIndex.from_frame(o[KEY]).map(lambda k: days.get(k, 0))
    else:
        o["limit"] = days
    o = o[o["days_out"] <= o["limit"]]
    return o.groupby(KEY)["qty"].sum()


def planned_monthly(occ: pd.DataFrame, months: pd.PeriodIndex) -> pd.DataFrame:
    if occ.empty:
        return pd.DataFrame(columns=KEY + ["month", "planned_qty"])
    o = occ.assign(month=occ["due_date"].dt.to_period("M"))
    o = o[o["month"].isin(months)]
    g = o.groupby(KEY + ["month"])["qty"].sum().rename("planned_qty").reset_index()
    return g


# --------------------------------------------------------------- reliability
def reliability_demand(ds, as_of: pd.Timestamp, window_days: pd.Series | float, cfg) -> pd.DataFrame:
    """Expected failures per part-site within a window using a Weibull model.

    eta is set so the Weibull mean equals the BOM expected life;
    conditional probability of failure in (t, t+dt] given survival to t:
        P = 1 - exp(-((t+dt)/eta)^beta + (t/eta)^beta)
    """
    bom = ds["bom"].merge(ds["assets"][["asset_id", "site_id", "status", "install_date",
                                        "run_hours", "criticality", "downtime_cost_per_hour_usd"]],
                          on="asset_id", how="left")
    bom = bom[bom["status"] == "Active"].copy()
    pm = ds["part_master"].set_index("part_id")
    bom["category"] = bom["part_id"].map(pm["category"])
    betas = cfg.get("reliability.beta_by_category", {}) or {}
    default_beta = cfg.get("reliability.default_beta", 1.5)
    bom["beta"] = bom["category"].map(betas).fillna(default_beta).astype(float)

    age_days = (as_of - bom["install_date"]).dt.days.clip(lower=30)
    bom["hours_per_day"] = (bom["run_hours"] / age_days).clip(upper=24).fillna(16)
    bom["eta"] = bom["expected_life_hours"] / bom["beta"].map(lambda b: math.gamma(1 + 1 / b))

    if isinstance(window_days, pd.Series):
        wd = pd.MultiIndex.from_frame(bom[KEY]).map(lambda k: window_days.get(k, 30)).astype(float)
    else:
        wd = float(window_days)
    t = bom["hours_since_replacement"].clip(lower=0)
    dt = bom["hours_per_day"] * wd
    eta = bom["eta"].replace(0, np.nan)
    h = ((t + dt) / eta) ** bom["beta"] - (t / eta) ** bom["beta"]
    bom["p_fail"] = (1 - np.exp(-h)).fillna(0)
    bom["expected_failures"] = bom["p_fail"] * bom["qty_per_asset"]
    bom["life_used_pct"] = (t / bom["expected_life_hours"]).round(3)
    bom["downtime_risk_usd"] = bom["p_fail"] * bom["downtime_cost_per_hour_usd"]  # per hour of outage

    agg = bom.groupby(KEY).agg(
        expected_failures=("expected_failures", "sum"),
        max_p_fail=("p_fail", "max"),
        installed_qty=("qty_per_asset", "sum"),
        n_assets=("asset_id", "nunique"),
        critical_fitted=("critical_component", lambda s: (s == "Y").any()),
        on_critical_asset=("criticality", lambda s: (s == "A").any()),
        max_downtime_cost_hr=("downtime_cost_per_hour_usd", "max"),
        median_downtime_cost_hr=("downtime_cost_per_hour_usd", "median"),
        max_life_used_pct=("life_used_pct", "max"),
    ).reset_index()
    return agg


# ----------------------------------------------------------------- lead time
def lead_time_stats(ds, prior_weight: float = 3.0) -> pd.DataFrame:
    """Per-part lead-time mean/std (days) from received POs, shrunk toward the
    preferred supplier's published figures when history is thin."""
    pm = ds["part_master"][["part_id", "preferred_supplier_id", "lead_time_days_std"]]
    sup = ds["suppliers"].set_index("supplier_id")
    po = ds["purchase_orders"]
    rec = po[po["received_date"].notna() & po["actual_lead_time_days"].notna()]
    rec = rec.sort_values("received_date").groupby("part_id").tail(12)
    hist = rec.groupby("part_id")["actual_lead_time_days"].agg(["mean", "std", "count"])
    late = rec.assign(late=rec["actual_lead_time_days"] > rec["promised_lead_time_days"]) \
              .groupby("part_id")["late"].mean()

    out = pm.copy()
    s_mean = out["preferred_supplier_id"].map(sup["avg_lead_time_days"])
    s_std = out["preferred_supplier_id"].map(sup["lead_time_std_days"])
    prior_mean = s_mean.fillna(out["lead_time_days_std"]).fillna(30)
    prior_std = s_std.fillna(prior_mean * 0.25)
    n = out["part_id"].map(hist["count"]).fillna(0)
    h_mean = out["part_id"].map(hist["mean"]).fillna(prior_mean)
    h_std = out["part_id"].map(hist["std"]).fillna(prior_std)
    k = prior_weight
    out["lt_mean_days"] = ((n * h_mean + k * prior_mean) / (n + k)).round(1)
    out["lt_std_days"] = np.sqrt((n * h_std ** 2 + k * prior_std ** 2) / (n + k)).round(1)
    out["lt_observations"] = n.astype(int)
    out["late_delivery_rate"] = out["part_id"].map(late).round(2)
    out["supplier_on_time_pct"] = out["preferred_supplier_id"].map(sup["on_time_delivery_pct"])
    return out[["part_id", "preferred_supplier_id", "lt_mean_days", "lt_std_days",
                "lt_observations", "late_delivery_rate", "supplier_on_time_pct"]]
