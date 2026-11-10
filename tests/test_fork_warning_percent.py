"""Fork (tyler5136): percent-of-interval warning window."""

from custom_components.maintenance_supporter.helpers.status import effective_warning_days
from custom_components.maintenance_supporter.models.maintenance_task import (
    MaintenanceTask,
    global_warning_percent,
    set_global_warning_percent,
)


def test_percent_off_keeps_fixed_days():
    assert effective_warning_days(7, 30) == 7
    assert effective_warning_days(7, 30, 0) == 7


def test_percent_replaces_fixed_days():
    assert effective_warning_days(7, 30, 20) == 6
    assert effective_warning_days(7, 365, 20) == 73
    assert effective_warning_days(14, 90, 20) == 18


def test_percent_rounds_up_and_caps_below_span():
    assert effective_warning_days(0, 7, 20) == 2  # ceil(1.4)
    assert effective_warning_days(0, 2, 90) == 1  # capped at span - 1
    assert effective_warning_days(0, 1, 20) == 0  # one-day task: no window


def test_no_span_ignores_percent():
    assert effective_warning_days(5, None, 20) == 5
    assert effective_warning_days(5, 0, 20) == 5


def test_global_percent_flows_into_model():
    try:
        set_global_warning_percent(20)
        task = MaintenanceTask.from_dict(
            {"id": "t1", "name": "Salt", "type": "custom", "schedule_type": "time_based", "interval_days": 30, "warning_days": 7}
        )
        assert global_warning_percent() == 20
        assert task.warning_percent == 20
        assert task.effective_warning_days == 6
        set_global_warning_percent(999)
        assert global_warning_percent() == 0
    finally:
        set_global_warning_percent(0)
