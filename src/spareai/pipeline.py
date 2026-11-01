"""End-to-end pipeline:
ingest -> validate -> data quality -> analytics -> recommendations ->
backtest -> persist (warehouse) -> reports.
"""
from __future__ import annotations

import logging
import time
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

from .analytics.core import analyze
from .config import Settings, load_settings
from .evaluation.backtest import run_policy_backtest
from .ingestion.loader import load_dataset
from .ingestion.sources import DataSource, build_source
from .logging_setup import setup_logging
from .quality.checks import OPEN_PO_STATUSES, run_quality_checks
from .recommendation.engine import INACTIVE, recommend
from .storage.warehouse import Warehouse

log = logging.getLogger("spareai.pipeline")

POLICY_COLS = [
    "part_id", "site_id", "description", "category", "criticality_ved", "abc_class", "xyz_class",
    "segment", "demand_pattern", "movement_class", "lifecycle_status", "unit_cost_usd",
    "qty_last_12m", "qty_prev_12m", "months_with_demand", "months_since_last_issue", "adi", "cv2",
    "method", "fc_monthly_rate", "fc_monthly_rate_used", "demand_floor_applied", "stock_item",
    "unplanned_share", "fc_12m_qty", "sigma_monthly", "backtest_wape",
    "planned_12m_scheduled", "pm_consumption_ratio", "planned_12m", "planned_in_pp", "rel_failures_in_pp", "rel_failures_12m", "n_assets",
    "installed_qty", "critical_fitted", "max_life_used_pct", "preferred_supplier_id",
    "lt_mean_days", "lt_std_days", "lt_observations", "late_delivery_rate", "service_level",
    "protection_days", "ltd_unplanned", "safety_stock", "rop_new", "order_qty_new", "min_new",
    "max_new", "eoq", "insurance_spare", "on_hand_qty", "reserved_qty", "on_order_qty",
    "available_qty", "inventory_position", "duplicate_code_stock", "min_level", "max_level",
    "reorder_point", "stock_value_usd", "days_of_cover", "months_of_cover", "p_stockout_lt",
    "stockout_cost_usd", "stockout_events_12m", "annual_demand", "annual_usage_value_usd",
]


def resolve_as_of(ds, settings: Settings) -> pd.Timestamp:
    v = settings.get("analysis.as_of_date")
    if v:
        return pd.Timestamp(v).normalize()
    return ds["consumption"]["txn_date"].max().normalize()


def holdout_accuracy(metrics: pd.DataFrame) -> dict:
    """Out-of-sample WAPE on the last backtest window (3-month cumulative
    demand) for the per-series selected model vs every single method."""
    def wape(df):
        a = df["holdout_actual"].sum()
        return float((df["holdout_fc"] - df["holdout_actual"]).abs().sum() / a) if a else float("nan")
    out = {"SpareAI (selected per part)": wape(metrics[metrics["selected"]])}
    for m, g in metrics.groupby("method"):
        out[m] = wape(g)
    return out


def build_summary(pol, recs, dq, bt, metrics, choice_wape) -> dict:
    inv_value = float(pol["stock_value_usd"].sum())
    inactive = pol["movement_class"].isin(INACTIVE)
    by_action = recs.groupby("action").agg(n=("part_id", "size"), value=("value_usd", "sum"),
                                           saving=("annual_saving_usd", "sum"))
    def a(action, col): return float(by_action[col].get(action, 0))
    s = {
        "inventory_value_usd": inv_value,
        "part_sites": int(len(pol)),
        "parts": int(pol["part_id"].nunique()),
        "sites": int(pol["site_id"].nunique()),
        "actions": {k: int(v) for k, v in by_action["n"].items()},
        "reorder_value_usd": a("REORDER", "value") + a("STOCK", "value"),
        "clear_value_usd": a("CLEAR", "value"),
        "annual_saving_usd": float(recs["annual_saving_usd"].sum()),
        "critical_reorders": int(((recs["action"].isin(["REORDER", "STOCK"])) &
                                  (recs["urgency"] == "CRITICAL")).sum()),
        "stockout_risk_usd": float(recs["stockout_risk_usd"].sum()),
        "dead_stock_value_usd": float(pol.loc[inactive, "stock_value_usd"].sum()),
        "dead_stock_pct": float(pol.loc[inactive, "stock_value_usd"].sum() / inv_value) if inv_value else 0,
        "zero_stock_part_sites": int(((pol["on_hand_qty"] <= 0) & (pol["stocked_here"]
                                       if "stocked_here" in pol else True)).sum()),
        "dq": dq["severity"].value_counts().to_dict(),
        "forecast_holdout_wape": choice_wape,
    }
    if bt is not None:
        b = bt.set_index("policy")
        s["backtest"] = {k: {kk: (float(vv) if isinstance(vv, (int, float, np.floating, np.integer)) else vv)
                             for kk, vv in row.items()} for k, row in b.to_dict("index").items()}
        s["backtest_cutoff"] = bt.attrs.get("cutoff")
    return s


def run_pipeline(settings: Settings | None = None, source: DataSource | None = None,
                 use_ml: bool = True, backtest: bool = True, reports: bool = True) -> dict:
    settings = settings or load_settings()
    setup_logging(settings.get("logging.level", "INFO"), settings.path("logging.file"))
    wh = Warehouse(settings.path("warehouse.path"))
    t0 = time.time()
    source = source or build_source(settings)
    run_id = wh.start_run("", source.describe())
    log.info("=== SpareAI run %s | source %s ===", run_id, source.describe())
    try:
        ds = load_dataset(source)
        as_of = resolve_as_of(ds, settings)
        log.info("Analysis date: %s", as_of.date())

        dq, dups = run_quality_checks(ds, as_of, settings)
        po = ds["purchase_orders"]
        open_po = po[po["status"].str.lower().isin(OPEN_PO_STATUSES)]
        dead_po_ids = set(dq.loc[dq["check"] == "open_po_on_dead_part", "key"])
        open_po_dead = open_po[open_po["po_id"].isin(dead_po_ids)]

        res = analyze(ds, as_of, settings, use_ml=use_ml, duplicates=dups)
        pol = res["policy"]
        recs = recommend(pol, settings, dups, open_po_dead)

        bt = bt_detail = bt_val = None
        if backtest:
            bt, bt_detail, bt_val, _ = run_policy_backtest(ds, as_of, settings, use_ml=use_ml)

        choice_wape = holdout_accuracy(res["model_metrics"])
        summary = build_summary(pol, recs, dq, bt, res["model_metrics"], choice_wape)
        summary["as_of_date"] = str(as_of.date())
        summary["runtime_sec"] = round(time.time() - t0, 1)

        # ---------------- persist
        pol_out = pol[[c for c in POLICY_COLS if c in pol.columns]].replace([np.inf, -np.inf], np.nan)
        wh.write("policy", pol_out, run_id)
        wh.write("recommendations", recs, run_id)
        wh.write("forecast", res["forecast"], run_id)
        wh.write("monthly_demand", res["monthly_demand"], run_id)
        wh.write("model_metrics", res["model_metrics"], run_id)
        wh.write("reliability", res["reliability"], run_id)
        wh.write("dq_issues", dq, run_id)
        if bt is not None:
            wh.write("backtest_summary", bt, run_id)
            wh.write("backtest_detail", bt_detail, run_id)
            wh.write("stockout_validation", bt_val, run_id)
        # reference data for the dashboard (latest snapshot only)
        for name in ("part_master", "assets", "bom", "purchase_orders", "maintenance_plans",
                     "suppliers", "stockouts"):
            wh.write(f"ref_{name}", ds[name], replace=True)

        out_files = []
        if reports:
            from .reporting.excel_report import write_excel_report
            out_dir = settings.path("output.dir")
            out_dir.mkdir(parents=True, exist_ok=True)
            xlsx = out_dir / f"SpareAI_Recommendations_{as_of.date()}.xlsx"
            write_excel_report(xlsx, summary, recs, pol_out, dq, bt, bt_val, res["model_metrics"])
            out_files.append(str(xlsx))
            log.info("Report written: %s", xlsx)
        summary["reports"] = out_files

        wh.finish_run(run_id, "SUCCESS", ds.row_counts(), summary)
        with wh.connect() as con:
            con.execute("UPDATE runs SET as_of_date=? WHERE run_id=?", (str(as_of.date()), run_id))
        wh.purge_runs(keep=20)
        log.info("=== Run %s finished in %.1fs ===", run_id, time.time() - t0)
        return {"run_id": run_id, "summary": summary, "recommendations": recs, "policy": pol,
                "dq": dq, "backtest": bt, "stockout_validation": bt_val}
    except Exception as exc:
        log.error("Run failed: %s", exc)
        wh.finish_run(run_id, "FAILED", error=traceback.format_exc()[-2000:])
        raise
