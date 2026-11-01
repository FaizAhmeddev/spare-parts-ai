"""Segmentation: ABC (value), XYZ (variability), SBC demand pattern
(Syntetos-Boylan-Croston), movement/lifecycle class and VED criticality."""
from __future__ import annotations

import numpy as np
import pandas as pd

KEY = ["part_id", "site_id"]


def abc_class(annual_value: pd.Series, a: float, b: float) -> pd.Series:
    """Pareto on annual usage value. Parts with zero usage are 'C'."""
    v = annual_value.clip(lower=0).sort_values(ascending=False)
    total = v.sum()
    if total <= 0:
        return pd.Series("C", index=annual_value.index)
    cum = v.cumsum() / total
    # an item is A if the cumulative share *before* it is < a
    prev = cum.shift(fill_value=0)
    cls = np.where(prev < a, "A", np.where(prev < b, "B", "C"))
    cls = pd.Series(cls, index=v.index)
    cls[v <= 0] = "C"
    return cls.reindex(annual_value.index)


def sbc_pattern(row: np.ndarray, adi_cut: float, cv2_cut: float) -> tuple[str, float, float]:
    nz = row[row > 0]
    if len(nz) == 0:
        return "no_demand", np.nan, np.nan
    adi = len(row) / len(nz)
    cv2 = (nz.std(ddof=0) / nz.mean()) ** 2 if len(nz) > 1 else 0.0
    if len(nz) < 3:
        return "sparse", adi, cv2
    if adi < adi_cut:
        return ("smooth" if cv2 < cv2_cut else "erratic"), adi, cv2
    return ("intermittent" if cv2 < cv2_cut else "lumpy"), adi, cv2


def classify(total_matrix: pd.DataFrame, ds, as_of: pd.Timestamp, cfg,
             planned_need: pd.Series) -> pd.DataFrame:
    pm = ds["part_master"].set_index("part_id")
    cons = ds["consumption"]
    out = total_matrix.index.to_frame(index=False)
    vals = total_matrix.values
    last12 = vals[:, -12:].sum(axis=1)
    prev12 = vals[:, -24:-12].sum(axis=1) if vals.shape[1] >= 24 else np.zeros(len(vals))
    out["qty_last_12m"] = last12
    out["qty_prev_12m"] = prev12
    out["qty_total_hist"] = vals.sum(axis=1)
    out["months_with_demand"] = (vals > 0).sum(axis=1)
    mean = vals.mean(axis=1)
    std = vals.std(axis=1, ddof=0)
    out["avg_monthly_qty"] = mean
    out["cv"] = np.where(mean > 0, std / np.where(mean > 0, mean, 1), np.nan)

    adi_cut = cfg.get("classification.sbc_adi_cutoff", 1.32)
    cv2_cut = cfg.get("classification.sbc_cv2_cutoff", 0.49)
    pat = [sbc_pattern(r, adi_cut, cv2_cut) for r in vals]
    out["demand_pattern"] = [p[0] for p in pat]
    out["adi"] = [p[1] for p in pat]
    out["cv2"] = [p[2] for p in pat]

    # XYZ
    x_t, y_t = cfg.get("classification.xyz_thresholds.X", .5), cfg.get("classification.xyz_thresholds.Y", 1.0)
    out["xyz_class"] = np.select([out["cv"] <= x_t, out["cv"] <= y_t], ["X", "Y"], "Z")
    out.loc[out["cv"].isna(), "xyz_class"] = "Z"

    # Part attributes
    out["unit_cost_usd"] = out["part_id"].map(pm["unit_cost_usd"]).fillna(0)
    out["criticality_ved"] = out["part_id"].map(pm["criticality_ved"]).fillna("D")
    out["lifecycle_status"] = out["part_id"].map(pm["lifecycle_status"]).fillna("Active")
    out["category"] = out["part_id"].map(pm["category"])
    out["description"] = out["part_id"].map(pm["description"])

    # ABC at part level (network value), applied to every site
    part_value = (out.assign(v=out["qty_last_12m"] * out["unit_cost_usd"])
                  .groupby("part_id")["v"].sum())
    a, b = cfg.get("classification.abc_thresholds.A", .8), cfg.get("classification.abc_thresholds.B", .95)
    out["annual_usage_value_usd"] = out["qty_last_12m"] * out["unit_cost_usd"]
    out["abc_class"] = out["part_id"].map(abc_class(part_value, a, b))

    # Movement / lifecycle class
    last_issue = cons.groupby(KEY)["txn_date"].max()
    out["last_issue_date"] = pd.MultiIndex.from_frame(out[KEY]).map(lambda k: last_issue.get(k, pd.NaT))
    out["last_issue_date"] = pd.to_datetime(out["last_issue_date"])
    out["months_since_last_issue"] = ((as_of - out["last_issue_date"]).dt.days / 30.44).round(1)
    dead_m = cfg.get("classification.dead_months", 24)
    slow_max = cfg.get("classification.slow_moving_max_issues_per_year", 2)
    decl = cfg.get("classification.declining_trend_ratio", 0.5)
    issues_12m = (cons[cons["txn_date"] > as_of - pd.DateOffset(months=12)]
                  .groupby(KEY).size())
    out["issue_events_12m"] = pd.MultiIndex.from_frame(out[KEY]).map(lambda k: issues_12m.get(k, 0)).astype(int)
    pneed = pd.MultiIndex.from_frame(out[KEY]).map(lambda k: planned_need.get(k, 0.0)).astype(float)
    out["planned_need_12m"] = pneed

    conds = [
        out["last_issue_date"].isna() & (out["planned_need_12m"] <= 0),
        (out["months_since_last_issue"] >= dead_m) & (out["planned_need_12m"] <= 0),
        (out["qty_prev_12m"] > 0) & (out["qty_last_12m"] < decl * out["qty_prev_12m"])
        & (out["planned_need_12m"] <= 0),
        out["issue_events_12m"] <= slow_max,
    ]
    out["movement_class"] = np.select(conds, ["never_used", "dead", "declining", "slow"], "regular")
    out.loc[out["lifecycle_status"].isin(["Obsolete", "Superseded"]) &
            (out["movement_class"] != "regular"), "movement_class"] = "obsolete"
    out["segment"] = out["abc_class"] + out["xyz_class"] + "-" + out["criticality_ved"]
    return out
