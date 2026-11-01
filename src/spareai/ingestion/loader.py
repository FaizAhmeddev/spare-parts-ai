"""Load all tables from a source, enforce the data contract, and coerce types."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .schema import DATE, FLOAT, INT, STR, TABLES, TableSpec
from .sources import DataSource

log = logging.getLogger(__name__)


class SchemaError(ValueError):
    """Raised when a source is missing required tables or columns."""


@dataclass
class Dataset:
    tables: dict[str, pd.DataFrame]
    load_issues: list[dict] = field(default_factory=list)
    source: str = ""

    def __getitem__(self, name: str) -> pd.DataFrame:
        return self.tables[name]

    def row_counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.tables.items()}


def _norm_col(c: str) -> str:
    c = str(c).strip().lower()
    c = re.sub(r"[^a-z0-9]+", "_", c)
    return c.strip("_")


def _coerce(series: pd.Series, kind: str) -> pd.Series:
    if kind == STR:
        s = series.astype("object")
        s = s.where(series.notna(), None)
        return s.map(lambda v: str(v).strip() if v is not None else None)
    if kind in (INT, FLOAT):
        s = pd.to_numeric(series, errors="coerce")
        return s.astype(float)
    if kind == DATE:
        return pd.to_datetime(series, errors="coerce").dt.normalize()
    raise ValueError(kind)


def conform(df: pd.DataFrame, spec: TableSpec, issues: list[dict]) -> pd.DataFrame:
    df = df.copy()
    df.columns = [_norm_col(c) for c in df.columns]
    missing = [c for c in spec.columns if c not in df.columns]
    if missing:
        raise SchemaError(f"Table '{spec.name}' is missing required columns: {missing}")
    for col, kind in spec.optional.items():
        if col not in df.columns:
            df[col] = np.nan
    all_cols = {**spec.columns, **spec.optional}
    for col, kind in all_cols.items():
        before_na = df[col].isna().sum()
        df[col] = _coerce(df[col], kind)
        bad = int(df[col].isna().sum() - before_na)
        if bad > 0:
            issues.append({"table": spec.name, "check": "type_coercion",
                           "severity": "WARNING", "key": col,
                           "detail": f"{bad} value(s) in '{col}' could not be read as {kind}"})
    # drop fully empty rows (common in Excel exports)
    df = df.dropna(how="all")
    extra = [c for c in df.columns if c not in all_cols]
    return df[list(all_cols) + extra]


def _derive(tables: dict[str, pd.DataFrame]) -> None:
    """Recompute derived/formula fields instead of trusting the source."""
    inv = tables["inventory"]
    inv["stock_value_usd"] = inv["on_hand_qty"].fillna(0) * inv["unit_cost_usd"].fillna(0)

    wo = tables["work_orders"]
    wo["total_cost_usd"] = wo["labour_cost_usd"].fillna(0) + wo["parts_cost_usd"].fillna(0)

    so = tables["stockouts"]
    so["downtime_cost_usd"] = (so["downtime_hours_due_to_stockout"].fillna(0)
                               * so["asset_downtime_cost_per_hour_usd"].fillna(0))

    po = tables["purchase_orders"]
    po["actual_lead_time_days"] = po["actual_lead_time_days"].fillna(
        (po["received_date"] - po["order_date"]).dt.days)
    po["promised_lead_time_days"] = po["promised_lead_time_days"].fillna(
        (po["promised_date"] - po["order_date"]).dt.days)


def load_dataset(source: DataSource) -> Dataset:
    tables, issues = {}, []
    for name, spec in TABLES.items():
        raw = source.read(name)
        tables[name] = conform(raw, spec, issues)
        log.info("Loaded %-18s %6d rows", name, len(tables[name]))
    _derive(tables)
    return Dataset(tables=tables, load_issues=issues, source=source.describe())
