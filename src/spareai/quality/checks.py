"""Data-quality checks. Runs before any analytics so planners can trust
(or fix) the inputs. Each issue carries a severity and a suggested fix."""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher

import pandas as pd

from ..ingestion.loader import Dataset
from ..ingestion.schema import TABLES

log = logging.getLogger(__name__)

ABBREVIATIONS = {
    "fltr": "filter", "flt": "filter", "brg": "bearing", "brgs": "bearing",
    "mtr": "motor", "vlv": "valve", "assy": "assembly", "kit": "kit",
    "gskt": "gasket", "sns": "sensor", "snsr": "sensor", "blt": "belt",
    "cplg": "coupling", "hyd": "hydraulic", "elec": "electric",
}
OPEN_PO_STATUSES = {"open", "overdue"}


def _issue(rows, check, table, severity, key, detail, fix=""):
    rows.append({"check": check, "table": table, "severity": severity,
                 "key": str(key), "detail": detail, "recommendation": fix})


def _norm_text(s: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", str(s).lower())
    return " ".join(ABBREVIATIONS.get(t, t) for t in tokens)


def _num_tokens(s: str) -> frozenset:
    return frozenset(re.findall(r"\d+(?:\.\d+)?", str(s)))


def _tokens_equivalent(a: str, b: str) -> bool:
    """True when two normalised descriptions differ only by typos.

    'coolant filter cnc' ~ 'coolant filtr cnc' -> True
    'gearbox kit large'  ~ 'gearbox kit small' -> False (a real variant)
    """
    ta, tb = set(a.split()), set(b.split())
    da, db = ta - tb, tb - ta
    if not da and not db:
        return True
    if len(da) != len(db):
        return False
    return all(max(SequenceMatcher(None, x, y).ratio() for y in db) >= 0.8 for x in da)


def find_duplicate_parts(pm: pd.DataFrame, threshold: float = 0.8) -> pd.DataFrame:
    """Detect the same physical part created twice under different IDs.

    A pair is a duplicate when the normalised part number matches, or when
    they share category + all numeric tokens (sizes, ratings) and the
    normalised descriptions are highly similar. Numeric tokens stop
    'Bearing 6203' being matched with 'Bearing 6204'.
    """
    pm = pm.copy()
    pm["_pn"] = pm["part_number"].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
    pm["_desc"] = pm["description"].map(_norm_text)
    pm["_nums"] = pm["description"].map(_num_tokens)
    pm = pm.sort_values(["created_date", "part_id"], na_position="last")
    out, seen = [], set()
    records = pm.to_dict("records")
    for i, a in enumerate(records):
        for b in records[i + 1:]:
            if b["part_id"] in seen:
                continue
            reason, score = None, 0.0
            if a["_pn"] and a["_pn"] == b["_pn"]:
                reason, score = "identical part number (format differs)", 1.0
            elif a["category"] == b["category"] and a["_nums"] == b["_nums"]:
                score = SequenceMatcher(None, a["_desc"], b["_desc"]).ratio()
                if score >= threshold and _tokens_equivalent(a["_desc"], b["_desc"]):
                    reason = "same description after normalising abbreviations/typos"
            if reason:
                # later-created / higher id record is treated as the duplicate
                keep, dup = (a, b) if a["part_id"] < b["part_id"] else (b, a)
                seen.add(dup["part_id"])
                out.append({"duplicate_part_id": dup["part_id"], "master_part_id": keep["part_id"],
                            "similarity": round(score, 3), "reason": reason,
                            "duplicate_desc": dup["description"], "master_desc": keep["description"]})
    return pd.DataFrame(out, columns=["duplicate_part_id", "master_part_id", "similarity",
                                      "reason", "duplicate_desc", "master_desc"])


def run_quality_checks(ds: Dataset, as_of: pd.Timestamp, cfg) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (issues, duplicate_pairs)."""
    rows: list[dict] = [dict(i, recommendation="Fix the source value") for i in ds.load_issues]
    t = ds.tables

    # 1. Primary keys / referential integrity / sign checks (generic, from contract)
    for name, spec in TABLES.items():
        df = t[name]
        pk = list(spec.primary_key)
        nulls = df[pk].isna().any(axis=1).sum()
        if nulls:
            _issue(rows, "null_primary_key", name, "ERROR", ",".join(pk),
                   f"{nulls} row(s) with empty key", "Rows are excluded; fix at source")
        dups = df[df.duplicated(pk, keep=False) & df[pk].notna().all(axis=1)]
        for key, _ in dups.groupby(pk):
            _issue(rows, "duplicate_primary_key", name, "ERROR", key,
                   "Key appears more than once", "De-duplicate at source")
        for col, (ref_t, ref_c) in spec.foreign_keys.items():
            valid = set(t[ref_t][ref_c].dropna())
            bad = df[df[col].notna() & ~df[col].isin(valid)]
            if len(bad):
                sample = ", ".join(map(str, bad[col].unique()[:5]))
                _issue(rows, "orphan_foreign_key", name, "ERROR", col,
                       f"{len(bad)} row(s) reference unknown {ref_t}.{ref_c} (e.g. {sample})",
                       f"Create the missing {ref_t} record or correct the reference")
        for col in spec.non_negative:
            neg = (df[col] < 0).sum()
            if neg:
                _issue(rows, "negative_value", name, "ERROR", col, f"{neg} negative value(s)",
                       "Negative quantities/costs are not allowed")

    pm, inv, po, cons = t["part_master"], t["inventory"], t["purchase_orders"], t["consumption"]
    pm_idx = pm.set_index("part_id")

    # 2. Duplicate part masters
    dup = find_duplicate_parts(pm, cfg.get("recommendation.fuzzy_duplicate_threshold", 0.8))
    for r in dup.itertuples():
        stock = inv.loc[inv.part_id == r.duplicate_part_id, "stock_value_usd"].sum()
        _issue(rows, "duplicate_part", "part_master", "ERROR", r.duplicate_part_id,
               f"Looks like a duplicate of {r.master_part_id} ({r.reason}; "
               f"'{r.duplicate_desc}' vs '{r.master_desc}'); ${stock:,.0f} stock held under the duplicate",
               f"Merge into {r.master_part_id}: move stock, redirect BOM/PM, block the duplicate")

    # 3. Ordering mistakes: open POs on obsolete / dead parts
    last_issue = cons.groupby("part_id")["txn_date"].max()
    dead_cut = as_of - pd.DateOffset(months=int(cfg.get("classification.dead_months", 24)))
    open_po = po[po["status"].str.lower().isin(OPEN_PO_STATUSES)]
    for r in open_po.itertuples():
        status = pm_idx["lifecycle_status"].get(r.part_id, "")
        li = last_issue.get(r.part_id, pd.NaT)
        is_dead = pd.isna(li) or li < dead_cut
        if status in ("Obsolete", "Superseded") or is_dead:
            why = f"part is {status}" if status in ("Obsolete", "Superseded") else \
                  ("part has never been issued" if pd.isna(li) else f"no issue since {li.date()}")
            _issue(rows, "open_po_on_dead_part", "purchase_orders", "ERROR", r.po_id,
                   f"Open PO for {r.part_id} ({r.qty_ordered:.0f} units) but {why}",
                   "Cancel or reduce the PO with the supplier")

    # 4. Overdue purchase orders
    overdue = open_po[open_po["promised_date"] < as_of]
    for r in overdue.itertuples():
        late = (as_of - r.promised_date).days
        _issue(rows, "overdue_po", "purchase_orders", "WARNING", r.po_id,
               f"{r.part_id} from {r.supplier_id} is {late} days past promised date",
               "Expedite with supplier")

    # 5. PO receipt anomalies
    over = po[po["qty_received"].fillna(0) > po["qty_ordered"]]
    for r in over.itertuples():
        _issue(rows, "over_receipt", "purchase_orders", "WARNING", r.po_id,
               f"Received {r.qty_received:.0f} > ordered {r.qty_ordered:.0f}", "Check GRN")

    # 6. Inventory sanity
    for r in inv[inv["reserved_qty"] > inv["on_hand_qty"]].itertuples():
        _issue(rows, "reserved_exceeds_on_hand", "inventory", "WARNING", f"{r.part_id}@{r.site_id}",
               f"Reserved {r.reserved_qty:.0f} > on hand {r.on_hand_qty:.0f}", "Check reservations")
    for r in inv[inv["min_level"] > inv["max_level"]].itertuples():
        _issue(rows, "min_above_max", "inventory", "WARNING", f"{r.part_id}@{r.site_id}",
               f"Min {r.min_level:.0f} > Max {r.max_level:.0f}", "Correct MRP parameters")
    m = inv.merge(pm[["part_id", "unit_cost_usd"]], on="part_id", suffixes=("", "_master"))
    diff = m[(m["unit_cost_usd_master"] > 0) &
             ((m["unit_cost_usd"] - m["unit_cost_usd_master"]).abs() / m["unit_cost_usd_master"] > 0.05)]
    for r in diff.itertuples():
        _issue(rows, "unit_cost_mismatch", "inventory", "INFO", f"{r.part_id}@{r.site_id}",
               f"Inventory cost {r.unit_cost_usd:.2f} vs master {r.unit_cost_usd_master:.2f}",
               "Align valuation")

    # 7. Superseded / obsolete parts still in stock
    held = inv.merge(pm[["part_id", "lifecycle_status", "superseded_by_part_id"]], on="part_id")
    held = held[held["lifecycle_status"].isin(["Obsolete", "Superseded"]) & (held["on_hand_qty"] > 0)]
    for r in held.itertuples():
        nxt = f" (replaced by {r.superseded_by_part_id})" if isinstance(r.superseded_by_part_id, str) else ""
        _issue(rows, "inactive_part_in_stock", "inventory", "WARNING", f"{r.part_id}@{r.site_id}",
               f"{r.lifecycle_status}{nxt} part holds {r.on_hand_qty:.0f} units (${r.stock_value_usd:,.0f})",
               "Use up, return, or dispose")

    # 8. Overdue preventive maintenance
    mp = t["maintenance_plans"]
    od = mp[(mp["plan_status"] == "Active") & (mp["next_due_date"] < as_of)]
    for r in od.itertuples():
        _issue(rows, "overdue_pm", "maintenance_plans", "WARNING", r.pm_plan_id,
               f"PM on {r.asset_id} due {r.next_due_date.date()} ({(as_of - r.next_due_date).days} days overdue)"
               f" needs {r.qty_required:.0f} x {r.part_id}", "Schedule the PM; parts are reserved in demand")

    # 9. Consumption against retired assets
    assets = t["assets"]
    ret = assets[assets["status"].str.lower() == "retired"][["asset_id", "retired_date"]]
    c = cons.merge(ret, on="asset_id")
    c = c[c["retired_date"].notna() & (c["txn_date"] > c["retired_date"])]
    if len(c):
        _issue(rows, "issue_to_retired_asset", "consumption", "WARNING", "asset_id",
               f"{len(c)} issue(s) booked to assets after retirement", "Check work order asset coding")

    # 10. BOM items past expected life (informational: drives reliability demand)
    bom = t["bom"]
    past = bom[bom["hours_since_replacement"] > bom["expected_life_hours"]]
    if len(past):
        _issue(rows, "component_past_expected_life", "bom", "INFO", "hours_since_replacement",
               f"{len(past)} fitted component(s) are past expected life",
               "Included in reliability-based demand; consider planned replacement")

    # 11. Future-dated transactions
    fut = cons[cons["txn_date"] > as_of + pd.Timedelta(days=1)]
    if len(fut):
        _issue(rows, "future_dated_issue", "consumption", "ERROR", "txn_date",
               f"{len(fut)} issue(s) dated after the analysis date", "Fix dates at source")

    issues = pd.DataFrame(rows, columns=["check", "table", "severity", "key", "detail", "recommendation"])
    sev_order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
    issues = issues.sort_values(["severity", "check"], key=lambda s: s.map(sev_order) if s.name == "severity" else s)
    log.info("Data quality: %d issues (%s)", len(issues), issues["severity"].value_counts().to_dict())
    return issues.reset_index(drop=True), dup
