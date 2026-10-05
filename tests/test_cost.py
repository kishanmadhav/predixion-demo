import pytest

from cost.fargate_cost import (
    X86_US_EAST_1,
    Assumptions,
    peak_provisioned,
    scheduled_scaling,
    task_hourly_cost,
    tasks_for,
)


def test_task_hourly_cost_for_1_vcpu_2_gb_x86() -> None:
    # 1 x $0.04048 + 2 x $0.004445
    assert task_hourly_cost(1.0, 2.0, X86_US_EAST_1) == pytest.approx(0.04937)


def test_tasks_needed_uses_littles_law_plus_a_spare() -> None:
    a = Assumptions()
    # 10 calls/min x 4 min = 40 concurrent; 40 / 20 per task = 2, +1 spare
    assert tasks_for(10, a) == 3
    # 50 calls/min x 4 min = 200 concurrent -> 10, +1 spare
    assert tasks_for(50, a) == 11


def test_peak_provisioned_runs_the_peak_fleet_all_month() -> None:
    est = peak_provisioned(Assumptions())
    assert est.task_hours == pytest.approx(11 * 730)
    assert est.monthly_usd == pytest.approx(11 * 730 * 0.04937, rel=1e-6)


def test_scheduled_scaling_adds_burst_tasks_only_for_the_window() -> None:
    a = Assumptions()
    est = scheduled_scaling(a)
    days = 730 / 24
    burst_hours = 2.0 + a.prewarm_hours + a.drain_hours
    expected_task_hours = 3 * 730 + (11 - 3) * burst_hours * days
    assert est.task_hours == pytest.approx(expected_task_hours)
    assert est.monthly_usd == pytest.approx(expected_task_hours * 0.04937, rel=1e-6)


def test_scheduled_scaling_is_much_cheaper_than_peak_provisioning() -> None:
    a = Assumptions()
    assert scheduled_scaling(a).monthly_usd < 0.4 * peak_provisioned(a).monthly_usd


def test_monthly_call_volume() -> None:
    a = Assumptions()
    # 22 h x 60 x 10 + 2 h x 60 x 50 = 19,200 calls/day
    assert a.calls_per_day == 19_200
