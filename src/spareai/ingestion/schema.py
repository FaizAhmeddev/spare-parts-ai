"""Data contract: the logical tables SpareAI needs, independent of source.

Any source (Excel, CSV, ERP database) must deliver these columns. Optional
columns may be missing - they are filled with defaults. Formula columns from
the Excel prototype (stock_value_usd, total_cost_usd, downtime_cost_usd) are
*recomputed* by the engine, never trusted from the source.
"""
from __future__ import annotations

from dataclasses import dataclass, field

STR, INT, FLOAT, DATE = "str", "int", "float", "date"


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: dict[str, str]                       # required columns -> type
    primary_key: tuple[str, ...]
    optional: dict[str, str] = field(default_factory=dict)
    foreign_keys: dict[str, tuple[str, str]] = field(default_factory=dict)
    non_negative: tuple[str, ...] = ()
    description: str = ""


TABLES: dict[str, TableSpec] = {
    "part_master": TableSpec(
        name="part_master",
        description="One row per stock-keeping spare part (material master).",
        columns={
            "part_id": STR, "part_number": STR, "description": STR,
            "category": STR, "uom": STR, "unit_cost_usd": FLOAT,
            "criticality_ved": STR, "preferred_supplier_id": STR,
            "lead_time_days_std": FLOAT, "min_order_qty": INT,
            "lifecycle_status": STR,
        },
        optional={
            "manufacturer": STR, "shelf_life_months": FLOAT,
            "alternate_supplier_id": STR, "superseded_by_part_id": STR,
            "created_date": DATE,
        },
        primary_key=("part_id",),
        foreign_keys={"preferred_supplier_id": ("suppliers", "supplier_id")},
        non_negative=("unit_cost_usd", "lead_time_days_std", "min_order_qty"),
    ),
    "suppliers": TableSpec(
        name="suppliers",
        description="Vendor master with delivery performance.",
        columns={
            "supplier_id": STR, "supplier_name": STR,
            "avg_lead_time_days": FLOAT, "lead_time_std_days": FLOAT,
            "on_time_delivery_pct": FLOAT,
        },
        optional={"country": STR, "categories_supplied": STR,
                  "quality_rating": FLOAT, "payment_terms": STR,
                  "currency": STR, "min_order_value_usd": FLOAT},
        primary_key=("supplier_id",),
        non_negative=("avg_lead_time_days", "lead_time_std_days"),
    ),
    "assets": TableSpec(
        name="assets",
        description="Equipment register.",
        columns={
            "asset_id": STR, "asset_name": STR, "asset_type": STR,
            "site_id": STR, "criticality": STR, "status": STR,
            "install_date": DATE, "run_hours": FLOAT,
            "downtime_cost_per_hour_usd": FLOAT,
        },
        optional={"asset_tag": STR, "production_line": STR,
                  "manufacturer": STR, "model": STR, "retired_date": DATE},
        primary_key=("asset_id",),
        non_negative=("run_hours", "downtime_cost_per_hour_usd"),
    ),
    "bom": TableSpec(
        name="bom",
        description="Which parts are fitted on which assets (equipment BOM).",
        columns={
            "bom_id": STR, "asset_id": STR, "part_id": STR,
            "qty_per_asset": INT, "critical_component": STR,
            "expected_life_hours": FLOAT, "hours_since_replacement": FLOAT,
        },
        optional={"last_replaced_date": DATE},
        primary_key=("bom_id",),
        foreign_keys={"asset_id": ("assets", "asset_id"),
                      "part_id": ("part_master", "part_id")},
        non_negative=("qty_per_asset", "expected_life_hours",
                      "hours_since_replacement"),
    ),
    "inventory": TableSpec(
        name="inventory",
        description="Current stock position per part and site (snapshot).",
        columns={
            "part_id": STR, "site_id": STR, "on_hand_qty": INT,
            "reserved_qty": INT, "on_order_qty": INT, "min_level": INT,
            "max_level": INT, "reorder_point": INT, "unit_cost_usd": FLOAT,
        },
        optional={"bin_location": STR, "last_receipt_date": DATE,
                  "last_issue_date": DATE, "stock_value_usd": FLOAT},
        primary_key=("part_id", "site_id"),
        foreign_keys={"part_id": ("part_master", "part_id")},
        non_negative=("on_hand_qty", "reserved_qty", "on_order_qty"),
    ),
    "consumption": TableSpec(
        name="consumption",
        description="Goods issues of spare parts (the demand signal).",
        columns={
            "txn_id": STR, "txn_date": DATE, "part_id": STR, "site_id": STR,
            "qty_issued": FLOAT, "issue_reason": STR,
        },
        optional={"asset_id": STR, "wo_id": STR, "unit_cost_usd": FLOAT,
                  "txn_type": STR},
        primary_key=("txn_id",),
        foreign_keys={"part_id": ("part_master", "part_id"),
                      "wo_id": ("work_orders", "wo_id")},
    ),
    "work_orders": TableSpec(
        name="work_orders",
        description="Maintenance work orders.",
        columns={
            "wo_id": STR, "asset_id": STR, "site_id": STR, "wo_type": STR,
            "created_date": DATE, "status": STR,
        },
        optional={"priority": INT, "failure_code": STR, "description": STR,
                  "completed_date": DATE, "downtime_hours": FLOAT,
                  "labour_hours": FLOAT, "labour_cost_usd": FLOAT,
                  "parts_cost_usd": FLOAT, "total_cost_usd": FLOAT},
        primary_key=("wo_id",),
        foreign_keys={"asset_id": ("assets", "asset_id")},
    ),
    "purchase_orders": TableSpec(
        name="purchase_orders",
        description="Purchase order lines with promised and actual delivery.",
        columns={
            "po_id": STR, "part_id": STR, "supplier_id": STR, "site_id": STR,
            "order_date": DATE, "promised_date": DATE, "qty_ordered": FLOAT,
            "status": STR,
        },
        optional={"received_date": DATE, "qty_received": FLOAT,
                  "unit_price_usd": FLOAT, "expedited_flag": STR,
                  "promised_lead_time_days": FLOAT,
                  "actual_lead_time_days": FLOAT},
        primary_key=("po_id",),
        foreign_keys={"part_id": ("part_master", "part_id"),
                      "supplier_id": ("suppliers", "supplier_id")},
    ),
    "maintenance_plans": TableSpec(
        name="maintenance_plans",
        description="Preventive maintenance schedules and the parts they use.",
        columns={
            "pm_plan_id": STR, "asset_id": STR, "site_id": STR,
            "frequency_days": FLOAT, "next_due_date": DATE, "part_id": STR,
            "qty_required": FLOAT, "plan_status": STR,
        },
        optional={"pm_task": STR, "last_done_date": DATE,
                  "est_downtime_hours": FLOAT},
        primary_key=("pm_plan_id", "part_id"),   # one row per plan x part
        foreign_keys={"asset_id": ("assets", "asset_id"),
                      "part_id": ("part_master", "part_id")},
    ),
    "stockouts": TableSpec(
        name="stockouts",
        description="Logged stockout incidents (used for validation).",
        columns={
            "stockout_id": STR, "event_date": DATE, "part_id": STR,
            "site_id": STR, "downtime_hours_due_to_stockout": FLOAT,
        },
        optional={"asset_id": STR, "wo_id": STR, "part_criticality_ved": STR,
                  "days_out_of_stock": FLOAT,
                  "asset_downtime_cost_per_hour_usd": FLOAT,
                  "expedite_cost_usd": FLOAT, "resolution": STR,
                  "downtime_cost_usd": FLOAT},
        primary_key=("stockout_id",),
        foreign_keys={"part_id": ("part_master", "part_id")},
    ),
}
