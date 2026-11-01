"""Planner-ready Excel workbook: one sheet per action list + KPIs."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")
HEADER_FONT = Font(color="FFFFFF", bold=True)
ACTION_FILL = {"REORDER": "FDE2E1", "STOCK": "FFF1D6", "CLEAR": "E1F0E5",
               "REVIEW": "E6E3F5", "ADJUST": "E3EEF9"}
MONEY_HINTS = ("usd", "value", "saving", "cost")


def _write(ws_df: pd.DataFrame, writer, sheet: str, widths: dict | None = None):
    ws_df.to_excel(writer, sheet_name=sheet, index=False)
    ws = writer.sheets[sheet]
    for cell in ws[1]:
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.freeze_panes = "A2"
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(ws_df.columns, start=1):
        letter = get_column_letter(i)
        w = (widths or {}).get(col)
        if w is None:
            sample = ws_df[col].astype(str).head(200).str.len()
            w = min(max(len(str(col)) + 2, int(sample.quantile(0.9)) + 2 if len(sample) else 10), 60)
        ws.column_dimensions[letter].width = w
        lc = str(col).lower()
        if any(h in lc for h in MONEY_HINTS) and pd.api.types.is_numeric_dtype(ws_df[col]):
            for c in ws[letter][1:]:
                c.number_format = '#,##0'
        elif lc in ("fill_rate", "service_level", "p_stockout_lt", "wape", "backtest_wape", "holdout_wape"):
            for c in ws[letter][1:]:
                c.number_format = '0.0%'
    if "action" in ws_df.columns:
        a_idx = list(ws_df.columns).index("action") + 1
        for row in ws.iter_rows(min_row=2):
            color = ACTION_FILL.get(row[a_idx - 1].value)
            if color:
                row[a_idx - 1].fill = PatternFill("solid", fgColor=color)
    return ws


def write_excel_report(path: Path, summary: dict, recs: pd.DataFrame, pol: pd.DataFrame,
                       dq: pd.DataFrame, bt: pd.DataFrame | None, bt_val: pd.DataFrame | None,
                       metrics: pd.DataFrame) -> None:
    path = Path(path)
    cols = ["priority", "action", "sub_action", "urgency", "part_id", "site_id", "description",
            "category", "recommended_qty", "value_usd", "annual_saving_usd", "stockout_risk_usd",
            "transfer_from", "transfer_qty", "disposal_route", "confidence", "reason"]
    recs = recs[[c for c in cols if c in recs.columns]]
    kpi = [
        ("Analysis date", summary.get("as_of_date")),
        ("Part-site combinations analysed", summary["part_sites"]),
        ("Inventory value (USD)", round(summary["inventory_value_usd"])),
        ("Dead / obsolete stock value (USD)", round(summary["dead_stock_value_usd"])),
        ("Dead stock share", f"{summary['dead_stock_pct']:.1%}"),
        ("Value to buy - REORDER + STOCK (USD)", round(summary["reorder_value_usd"])),
        ("Value to clear (USD)", round(summary["clear_value_usd"])),
        ("Annual holding-cost saving (USD)", round(summary["annual_saving_usd"])),
        ("Critical reorders", summary["critical_reorders"]),
    ]
    for k, v in summary.get("actions", {}).items():
        kpi.append((f"Lines: {k}", v))
    if "backtest" in summary:
        for pol_name, m in summary["backtest"].items():
            kpi.append((f"Backtest {pol_name}: fill rate", f"{m['fill_rate']:.1%}"))
            kpi.append((f"Backtest {pol_name}: avg inventory (USD)", round(m["avg_inventory_value_usd"])))
            kpi.append((f"Backtest {pol_name}: stockout events", m["stockout_events"]))
    kpi_df = pd.DataFrame(kpi, columns=["KPI", "Value"])

    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        _write(kpi_df, xw, "Summary", {"KPI": 45, "Value": 22})
        _write(recs[recs["action"] != "OK"], xw, "All Actions", {"reason": 110, "description": 32})
        _write(recs[recs["action"] == "REORDER"], xw, "Reorder", {"reason": 110, "description": 32})
        _write(recs[recs["action"] == "STOCK"], xw, "Stock", {"reason": 110, "description": 32})
        _write(recs[recs["action"] == "CLEAR"], xw, "Clear", {"reason": 110, "description": 32})
        _write(recs[recs["action"].isin(["REVIEW", "ADJUST"])], xw, "Review & Adjust",
               {"reason": 110, "description": 32})
        _write(pol, xw, "Policy Detail")
        _write(dq, xw, "Data Quality", {"detail": 90, "recommendation": 50})
        if bt is not None:
            _write(bt, xw, "Backtest")
        if bt_val is not None:
            _write(bt_val, xw, "Stockout Validation")
        acc = pd.DataFrame([{"method": k, "holdout_wape": v}
                            for k, v in summary.get("forecast_holdout_wape", {}).items()])
        sel = metrics[metrics["selected"]].groupby("method").size().rename("series_selected")
        acc = acc.merge(sel, left_on="method", right_index=True, how="left").fillna({"series_selected": 0})
        _write(acc, xw, "Forecast Accuracy")
