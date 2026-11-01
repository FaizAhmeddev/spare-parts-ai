import pandas as pd
import pytest

from spareai.ingestion.loader import SchemaError, conform
from spareai.ingestion.schema import TABLES
from spareai.quality.checks import find_duplicate_parts, run_quality_checks

PLANTED_DUPLICATES = {("P0301", "P0263"), ("P0302", "P0089"), ("P0303", "P0227"),
                      ("P0304", "P0045"), ("P0305", "P0253"), ("P0306", "P0119")}


def test_all_tables_loaded(dataset):
    counts = dataset.row_counts()
    assert counts["part_master"] == 306
    assert counts["consumption"] > 6000
    assert set(counts) == set(TABLES)


def test_finds_exactly_the_planted_duplicates(dataset):
    dup = find_duplicate_parts(dataset["part_master"])
    assert set(zip(dup["duplicate_part_id"], dup["master_part_id"])) == PLANTED_DUPLICATES


def test_size_variants_are_not_duplicates():
    pm = pd.DataFrame({"part_id": ["A", "B", "C", "D"], "part_number": ["X1", "X2", "X3", "X4"],
                       "description": ["Bearing 6203-2RS", "Bearing 6204-2RS",
                                       "Gearbox Kit Large", "Gearbox Kit Small"],
                       "category": ["Bearings"] * 2 + ["Gear"] * 2, "created_date": pd.NaT})
    assert find_duplicate_parts(pm).empty


def test_flags_open_pos_on_dead_parts(dataset, settings):
    as_of = dataset["consumption"]["txn_date"].max()
    dq, _ = run_quality_checks(dataset, as_of, settings)
    dead_po = dq[dq["check"] == "open_po_on_dead_part"]
    assert len(dead_po) >= 4
    assert (dq["severity"].isin(["ERROR", "WARNING", "INFO"])).all()


def test_missing_required_column_is_rejected():
    df = pd.DataFrame({"part_id": ["P1"]})
    with pytest.raises(SchemaError):
        conform(df, TABLES["part_master"], [])


def test_column_names_are_normalised():
    df = pd.DataFrame({"Supplier ID": ["S1"], "Supplier Name": ["x"], "Avg Lead Time Days": [5],
                       "Lead Time Std Days": [1], "On Time Delivery %": [90]})
    df.columns = ["Supplier ID", "Supplier Name", "Avg Lead Time Days", "Lead Time Std Days",
                  "On-Time Delivery Pct"]
    out = conform(df, TABLES["suppliers"], [])
    assert "supplier_id" in out.columns and "on_time_delivery_pct" in out.columns
