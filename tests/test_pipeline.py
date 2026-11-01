import numpy as np


def test_run_succeeds_and_persists(run_result, settings):
    from spareai.storage.warehouse import Warehouse
    wh = Warehouse(settings.path("warehouse.path"))
    run = wh.latest_run()
    assert run["run_id"] == run_result["run_id"]
    assert len(wh.read("recommendations", run["run_id"])) == len(run_result["recommendations"])


def test_every_action_present_and_explained(run_result):
    recs = run_result["recommendations"]
    assert {"REORDER", "STOCK", "CLEAR", "REVIEW"} <= set(recs["action"])
    assert recs["reason"].str.len().gt(10).all()


def test_reorder_quantities_respect_moq(run_result):
    recs, pol = run_result["recommendations"], run_result["policy"]
    m = recs.merge(pol[["part_id", "site_id", "min_order_qty"]], on=["part_id", "site_id"])
    buy = m[m["action"].isin(["REORDER", "STOCK"]) & (m["transfer_qty"] == 0) & (m["recommended_qty"] > 0)]
    assert (buy["recommended_qty"] % buy["min_order_qty"].clip(lower=1) == 0).all()


def test_clear_never_exceeds_on_hand(run_result):
    recs, pol = run_result["recommendations"], run_result["policy"]
    m = recs.merge(pol[["part_id", "site_id", "on_hand_qty"]], on=["part_id", "site_id"])
    clear = m[m["action"] == "CLEAR"]
    assert (clear["recommended_qty"] <= clear["on_hand_qty"]).all()


def test_duplicates_routed_to_review(run_result):
    recs = run_result["recommendations"]
    merge = recs[recs["sub_action"] == "MERGE_DUPLICATE"]
    assert set(merge["part_id"]) <= {"P0301", "P0302", "P0303", "P0304", "P0305", "P0306"}
    assert len(merge) >= 4


def test_dead_parts_are_not_reordered(run_result):
    """Dead parts are only bought as deliberate insurance spares for vital parts."""
    recs, pol = run_result["recommendations"], run_result["policy"]
    m = recs.merge(pol[["part_id", "site_id", "movement_class", "insurance_spare"]], on=["part_id", "site_id"])
    bad = m[m["movement_class"].isin(["dead", "never_used"]) & m["action"].isin(["REORDER", "STOCK"])
            & ~m["insurance_spare"]]
    assert bad.empty


def test_backtest_ai_beats_current_policy(run_result):
    bt = run_result["backtest"].set_index("policy")
    ai, lg = bt.loc["AI policy"], bt.loc["Current min/max"]
    assert ai["fill_rate"] >= lg["fill_rate"]
    assert ai["avg_inventory_value_usd"] < lg["avg_inventory_value_usd"]


def test_policy_is_consistent(run_result):
    pol = run_result["policy"]
    assert (pol["max_new"] >= pol["rop_new"]).all()
    assert (pol["rop_new"] >= 0).all()
    assert pol["service_level"].between(0.5, 1).all()
    assert np.isfinite(pol["fc_monthly_rate"]).all()


def test_excel_report_written(run_result):
    from pathlib import Path
    from openpyxl import load_workbook
    f = run_result["summary"]["reports"][0]
    assert Path(f).exists()
    assert {"Summary", "Reorder", "Clear", "Data Quality"} <= set(load_workbook(f, read_only=True).sheetnames)
