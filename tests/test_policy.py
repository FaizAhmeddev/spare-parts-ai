import pytest

from spareai.inventory.policy import expected_shortage, fill_rate_rop, round_up_to_moq


def test_no_demand_needs_no_stock():
    assert fill_rate_rop(0, 0, 5, 0.99, 10) == 0


def test_expected_shortage_at_zero_equals_mean():
    assert expected_shortage(2.5, 2.5, 0, 10) == pytest.approx(2.5, rel=1e-6)


def test_higher_service_needs_higher_rop():
    lo = fill_rate_rop(3, 6, 4, 0.90, 10)
    hi = fill_rate_rop(3, 6, 4, 0.99, 10)
    assert hi >= lo


def test_lumpier_demand_needs_more_safety_stock():
    smooth = fill_rate_rop(3, 3, 4, 0.97, 10)
    lumpy = fill_rate_rop(3, 15, 4, 0.97, 10)
    assert lumpy > smooth


def test_normal_branch_for_fast_movers():
    rop = fill_rate_rop(40, 60, 50, 0.98, 10)
    assert rop >= 40


@pytest.mark.parametrize("q,moq,exp", [(7, 5, 10), (10, 5, 10), (0.2, 1, 1), (0, 5, 0), (3, 0, 3)])
def test_moq_rounding(q, moq, exp):
    assert round_up_to_moq(q, moq) == exp
