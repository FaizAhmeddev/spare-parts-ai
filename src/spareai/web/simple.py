"""Simple product view: how many pieces were used and how many are needed.

All "used" numbers are summed straight from the issue history (no model),
over exact calendar-month windows ending at the data date. "Required" is the
forecast for the next 12 months (breakdowns + planned maintenance).
"""
from __future__ import annotations

import math

import pandas as pd

STATUS = {  # label, css class, sort order
    "ORDER": ("Order now", "b-REORDER", 0),
    "EXCESS": ("Too much stock", "b-CLEAR", 1),
    "CHECK": ("Check data", "b-REVIEW", 2),
    "OK": ("OK", "b-OK", 3),
}


def windows(as_of: str) -> dict:
    """Month windows ending at the last complete month of the data."""
    ts = pd.Timestamp(as_of)
    end = ts.to_period("M")
    if ts != ts + pd.offsets.MonthEnd(0):      # data date is mid-month
        end = end - 1
    return {
        "last_month": [end],
        "last_year": list(pd.period_range(end=end, periods=12, freq="M")),
        "last_3_years": list(pd.period_range(end=end, periods=36, freq="M")),
        "years": [list(pd.period_range(end=end - 12 * k, periods=12, freq="M")) for k in (2, 1, 0)],
        "next_year": list(pd.period_range(start=end + 1, periods=12, freq="M")),
    }


def label(months: list) -> str:
    a, b = months[0], months[-1]
    return a.strftime("%b %Y") if a == b else f"{a.strftime('%b %Y')} – {b.strftime('%b %Y')}"


def _status(recs: pd.DataFrame) -> pd.DataFrame:
    r = recs.copy()
    r["buy"] = r["recommended_qty"].where(r["action"].isin(["REORDER", "STOCK"]), 0)
    r["clear"] = r["recommended_qty"].where(r["action"] == "CLEAR", 0)
    g = r.groupby("part_id").agg(order_qty=("buy", "sum"), clear_qty=("clear", "sum"),
                                 review=("action", lambda s: (s == "REVIEW").any()),
                                 critical=("urgency", lambda s: (s == "CRITICAL").any()))
    g["status"] = "OK"
    g.loc[g["review"], "status"] = "CHECK"
    g.loc[g["clear_qty"] > 0, "status"] = "EXCESS"
    g.loc[g["order_qty"] > 0, "status"] = "ORDER"
    return g


def product_table(hist: pd.DataFrame, fc: pd.DataFrame, pol: pd.DataFrame,
                  recs: pd.DataFrame, pm: pd.DataFrame, as_of: str) -> pd.DataFrame:
    w = windows(as_of)
    h = hist.copy()
    h["month"] = pd.PeriodIndex(h["month"], freq="M")
    used = pd.DataFrame(index=pm["part_id"])
    for key in ("last_month", "last_year", "last_3_years"):
        used[key] = h[h["month"].isin(w[key])].groupby("part_id")["qty"].sum()
    used = used.fillna(0)
    f = fc.copy()
    f["month"] = pd.PeriodIndex(f["month"], freq="M")
    # round up per site (you can't stock 0.4 of a part), then add sites together
    req = (f[f["month"].isin(w["next_year"])].groupby(["part_id", "site_id"])["total_fc"].sum()
           .map(math.ceil).groupby("part_id").sum())
    used["required_next_year"] = req.reindex(used.index).fillna(0).astype(int)
    stock = pol.groupby("part_id")[["on_hand_qty", "on_order_qty"]].sum()
    used = used.join(stock).fillna({"on_hand_qty": 0, "on_order_qty": 0})
    used = used.join(_status(recs))
    used["status"] = used["status"].fillna("OK")
    used = used.join(pm.set_index("part_id")[["description", "category", "criticality_ved", "unit_cost_usd"]])
    used["status_label"] = used["status"].map(lambda s: STATUS[s][0])
    used["status_css"] = used["status"].map(lambda s: STATUS[s][1])
    used["_o"] = used["status"].map(lambda s: STATUS[s][2])
    used = used.reset_index().sort_values(["_o", "critical", "last_year"], ascending=[True, False, False])
    return used.drop(columns="_o")


def product_detail(part_id: str, hist: pd.DataFrame, fc: pd.DataFrame, as_of: str) -> dict:
    w = windows(as_of)
    h = hist[hist["part_id"] == part_id].copy()
    h["month"] = pd.PeriodIndex(h["month"], freq="M")
    f = fc[fc["part_id"] == part_id].copy()
    f["month"] = pd.PeriodIndex(f["month"], freq="M")

    def used(months):
        return float(h[h["month"].isin(months)]["qty"].sum())

    monthly_used = h.groupby("month")["qty"].sum().reindex(w["last_3_years"], fill_value=0)
    monthly_fc = f.groupby("month")[["unplanned_fc", "planned_qty"]].sum().reindex(w["next_year"], fill_value=0)
    req_unplanned = float(monthly_fc["unplanned_fc"].sum())
    req_planned = float(monthly_fc["planned_qty"].sum())
    by_site = (h[h["month"].isin(w["last_year"])].groupby("site_id")["qty"].sum()
               .rename("used_last_year"))
    req_site = f[f["month"].isin(w["next_year"])].groupby("site_id")["total_fc"].sum().map(math.ceil)
    return {
        "last_month": used(w["last_month"]), "last_month_label": label(w["last_month"]),
        "last_year": used(w["last_year"]), "last_year_label": label(w["last_year"]),
        "last_3_years": used(w["last_3_years"]), "last_3_years_label": label(w["last_3_years"]),
        "required": int(req_site.sum()), "required_label": label(w["next_year"]),
        "required_breakdown": req_unplanned, "required_planned": req_planned,
        "yearly": [(label(m), used(m)) for m in w["years"]],
        "monthly_used": [(p.strftime("%b %y"), float(v)) for p, v in monthly_used.items()],
        "monthly_fc": [(p.strftime("%b %y"), float(r.unplanned_fc), float(r.planned_qty))
                       for p, r in monthly_fc.iterrows()],
        "by_site": pd.concat([by_site, req_site.rename("required_next_year")], axis=1).fillna(0),
    }