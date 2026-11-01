"""Monthly demand series construction."""
from __future__ import annotations

import pandas as pd

UNPLANNED_REASONS = {"corrective", "emergency", "breakdown", "unplanned"}
KEY = ["part_id", "site_id"]


def month_index(as_of: pd.Timestamp, months: int) -> pd.PeriodIndex:
    """Last `months` complete months ending at as_of (inclusive if as_of is month-end)."""
    end = as_of.to_period("M")
    if as_of != as_of + pd.offsets.MonthEnd(0):
        end = end - 1
    return pd.period_range(end=end, periods=months, freq="M")


def universe(ds) -> pd.DataFrame:
    """All part-site combinations that need a decision."""
    parts = [ds["inventory"][KEY], ds["consumption"][KEY],
             ds["maintenance_plans"][ds["maintenance_plans"]["plan_status"] == "Active"][KEY]]
    u = pd.concat(parts).dropna().drop_duplicates().sort_values(KEY).reset_index(drop=True)
    return u


def monthly_matrix(cons: pd.DataFrame, months: pd.PeriodIndex, keys: pd.DataFrame,
                   unplanned_only: bool = False) -> pd.DataFrame:
    """Wide matrix: rows = part-site, columns = months, values = qty issued."""
    c = cons
    if unplanned_only:
        c = c[c["issue_reason"].str.lower().isin(UNPLANNED_REASONS)]
    c = c.assign(month=c["txn_date"].dt.to_period("M"))
    c = c[c["month"].isin(months)]
    g = c.groupby(KEY + ["month"])["qty_issued"].sum().unstack("month")
    g = g.reindex(columns=months, fill_value=0)
    idx = pd.MultiIndex.from_frame(keys[KEY])
    return g.reindex(idx).fillna(0.0)


def long_monthly(matrix: pd.DataFrame, value_name: str = "qty") -> pd.DataFrame:
    out = matrix.stack().rename(value_name).reset_index()
    out.columns = KEY + ["month", value_name]
    out["month"] = out["month"].astype(str)
    return out
