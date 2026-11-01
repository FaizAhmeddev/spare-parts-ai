"""Inventory policy: safety stock, reorder point (ROP), order quantity, Max.

Lead-time demand (LTD) is modelled as the sum of
  * unplanned demand  - forecast rate x protection period, floored by the
                        Weibull expected failures of fitted components
  * planned demand    - parts required by PM work orders due inside the
                        protection period (deterministic)
Distribution of unplanned LTD:
  * Normal               when mean LTD is large (central limit)
  * Negative binomial    when variance > mean (lumpy / erratic)
  * Poisson              otherwise (slow movers)
Variance includes lead-time uncertainty:  P*sd^2 + d^2*sL^2
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats

DAYS_PER_MONTH = 30.44


def _nb_params(mu: float, var: float) -> tuple[float, float]:
    r = mu * mu / (var - mu)
    return r, r / (r + mu)


def expected_shortage(mu: float, var: float, s: float, normal_min: float) -> float:
    """E[(LTD - s)+] - expected units short per replenishment cycle."""
    if mu <= 0:
        return 0.0
    var = max(var, mu)
    if mu >= normal_min:
        sd = math.sqrt(var)
        k = (s - mu) / sd
        loss = stats.norm.pdf(k) - k * stats.norm.sf(k)      # standard loss G(k)
        return sd * loss
    s = int(max(math.floor(s), 0))
    x = np.arange(0, s)
    pmf = stats.nbinom.pmf(x, *_nb_params(mu, var)) if var > mu * 1.05 else stats.poisson.pmf(x, mu)
    return float(mu - s + ((s - x) * pmf).sum())


def fill_rate_rop(mu: float, var: float, q: float, target: float, normal_min: float) -> int:
    """Smallest reorder point whose expected fill rate >= target.

    Fill rate = 1 - E[(LTD - s)+] / Q  (Silver, Pyke & Peterson). Unlike a
    cycle-service quantile this stays meaningful for lumpy spare parts where
    most lead times see zero demand but each demand event is large.
    """
    if mu <= 0:
        return 0
    q = max(q, 1.0)
    allowed = (1 - target) * q
    hi = int(math.ceil(mu + 12 * math.sqrt(max(var, mu)) + 2))
    lo = 0
    if mu >= normal_min:
        lo = int(math.floor(mu))            # never plan below mean demand
    for s in range(lo, hi + 1):
        if expected_shortage(mu, var, s, normal_min) <= allowed:
            return s
    return hi


def ltd_quantile(mu: float, var: float, sl: float, normal_min: float) -> float:
    if mu <= 0:
        return 0.0
    var = max(var, mu)
    if mu >= normal_min:
        return mu + stats.norm.ppf(sl) * math.sqrt(var)
    if var > mu * 1.05:
        return float(stats.nbinom.ppf(sl, *_nb_params(mu, var)))
    return float(stats.poisson.ppf(sl, mu))


def prob_ltd_exceeds(mu: float, var: float, stock: float) -> float:
    """P(lead-time demand > stock): risk of running out before a new order arrives."""
    if mu <= 0:
        return 0.0
    var = max(var, mu)
    k = max(math.floor(stock), -1)
    if var > mu * 1.05:
        r = mu * mu / (var - mu)
        p = r / (r + mu)
        return float(stats.nbinom.sf(k, r, p))
    return float(stats.poisson.sf(k, mu))


def round_up_to_moq(q: float, moq: float) -> int:
    moq = max(int(moq or 1), 1)
    if q <= 0:
        return 0
    return int(math.ceil(q / moq) * moq)


def compute_policy(base: pd.DataFrame, cfg) -> pd.DataFrame:
    """`base` holds one row per part-site with the columns produced by the
    pipeline (forecast, lead time, classification, drivers, inventory)."""
    df = base.copy()
    sl_matrix = cfg.get("service_levels")
    review = float(cfg.get("inventory_policy.review_period_days", 7))
    normal_min = float(cfg.get("inventory_policy.normal_approx_min_mean", 10))
    max_cover = float(cfg.get("inventory_policy.max_cover_months", 12))
    floor_frac = float(cfg.get("inventory_policy.demand_floor_fraction", 0.5))
    min_hits = int(cfg.get("inventory_policy.stocking_rule.min_demand_months", 3))
    min_hits_crit = int(cfg.get("inventory_policy.stocking_rule.critical_min_demand_months", 1))
    h_rate = float(cfg.get("costs.holding_rate_annual", 0.22))
    s_cost = float(cfg.get("costs.order_cost_usd", 60))
    ins_on = bool(cfg.get("inventory_policy.insurance_spares.enabled", True))
    ins_crit = set(cfg.get("inventory_policy.insurance_spares.min_asset_criticality", ["A"]))

    df["service_level"] = [sl_matrix.get(v, {}).get(a, 0.95)
                           for v, a in zip(df["criticality_ved"], df["abc_class"])]

    # ---- demand rate with a floor for parts that are still moving
    hist_rate = df["qty_last_12m"].fillna(0) / 12
    moving = df["movement_class"].isin(["regular", "slow"])
    d = df["fc_monthly_rate"].clip(lower=0)
    floor = np.where(moving, floor_frac * hist_rate * df.get("unplanned_share", 1.0), 0)
    df["demand_floor_applied"] = d < floor
    d = np.maximum(d, floor)
    df["fc_monthly_rate_used"] = d

    # ---- stock / non-stock decision (buy-on-demand for very rare parts)
    hits = df["months_with_demand"].fillna(0)
    recent = df["months_since_last_issue"].fillna(999) <= 24
    critical = df["criticality_ved"].isin(["V", "E"])
    df["stock_item"] = ((df["planned_12m"].fillna(0) > 0)
                        | (recent & (hits >= min_hits))
                        | (recent & critical & (hits >= min_hits_crit)))
    df["stock_item"] &= df["lifecycle_status"].eq("Active") | (df["movement_class"] == "regular")

    df["protection_days"] = df["lt_mean_days"] + review
    P = df["protection_days"] / DAYS_PER_MONTH
    sL = df["lt_std_days"] / DAYS_PER_MONTH
    df["ltd_unplanned_forecast"] = d * P
    df["ltd_unplanned"] = np.maximum(df["ltd_unplanned_forecast"], df["rel_failures_in_pp"].fillna(0))
    df["ltd_var"] = P * df["sigma_monthly"].fillna(0) ** 2 + d ** 2 * sL ** 2

    # ---- order quantity (EOQ, capped by cover, rounded to MOQ)
    annual = 12 * d + df["planned_12m"].fillna(0)
    df["annual_demand"] = annual
    unit = df["unit_cost_usd"].clip(lower=0.01)
    eoq = np.sqrt(2 * annual * s_cost / (h_rate * unit))
    cap = np.maximum(df["min_order_qty"].fillna(1), max_cover * annual / 12)
    df["eoq"] = np.round(eoq, 1)
    q_raw = np.maximum(np.minimum(eoq, cap), 1)

    rops, qs, ss, risk, ins = [], [], [], [], []
    for r, qr in zip(df.itertuples(), q_raw):
        insurance = False
        if r.stock_item and (r.ltd_unplanned + r.planned_in_pp) > 0:
            q = round_up_to_moq(qr, r.min_order_qty)
            rop = fill_rate_rop(r.ltd_unplanned, r.ltd_var, q, r.service_level, normal_min)
            rop = int(math.ceil(rop + r.planned_in_pp - 1e-9))
        else:
            q, rop = 0, 0
        if (ins_on and rop == 0 and r.criticality_ved == "V" and r.lifecycle_status == "Active"
                and r.critical_fitted and r.asset_criticality in ins_crit
                and r.insurance_risk_usd > r.unit_cost_usd * h_rate):
            rop, q, insurance = 1, round_up_to_moq(1, r.min_order_qty), True
        rops.append(rop)
        qs.append(q)
        ins.append(insurance)
        ss.append(max(rop - r.ltd_unplanned - r.planned_in_pp, 0))
        risk.append(prob_ltd_exceeds(r.ltd_unplanned, r.ltd_var,
                                     r.inventory_position - r.planned_in_pp))
    df["rop_new"] = rops
    df["order_qty_new"] = qs
    df["safety_stock"] = np.round(ss, 2)
    df["insurance_spare"] = ins
    df["p_stockout_lt"] = np.round(risk, 4)
    df["max_new"] = df["rop_new"] + df["order_qty_new"]
    df["min_new"] = df["rop_new"]

    monthly_total = annual / 12
    daily = monthly_total / DAYS_PER_MONTH
    df["days_of_cover"] = np.where(daily > 0, df["available_qty"] / daily.replace(0, np.nan), np.inf)
    df["months_of_cover"] = np.where(monthly_total > 0, df["on_hand_qty"] / monthly_total.replace(0, np.nan),
                                     np.inf)
    return df
