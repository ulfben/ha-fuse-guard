"""Unit tests for the decision logic, one per branch of the decision order."""

from __future__ import annotations

from dataclasses import replace

from custom_components.fuse_guard.controller import (
    Decision,
    GuardState,
    Inputs,
    Settings,
    Status,
    decide,
)
from custom_components.fuse_guard.sources import PhaseSample

T0 = 1_000_000.0  # integration start
NOW = T0 + 3600  # well past the startup hold

SETTINGS = Settings(
    threshold_a=15.4,
    watts_per_amp=690,
    load_phases=("l1", "l2", "l3"),
    max_w=10000,
    has_actual=True,
    min_over_a=0.2,
    base_drop_w=310,
    cut_interval_s=10,
    response_s=8,
    cooldown_s=60,
    settling_s=30,
    recovery_interval_s=60,
    recovery_gain=0.6,
    step_w=100,
    startup_hold_s=60,
    pause_on_missing_data=True,
)


def phases(l1: float = 8, l2: float = 8, l3: float = 8, ts: float = NOW) -> dict:
    return {
        key: PhaseSample(value, f"sensor.{key}", ts, 1, None)
        for key, value in (("l1", l1), ("l2", l2), ("l3", l3))
    }


def inputs(**changes) -> Inputs:
    base = Inputs(
        now=NOW,
        phases=phases(),
        target_w=6000,
        readback_w=6000,
        actual_w=6000,
        active=True,
        command_changed_ts=NOW - 600,
    )
    return replace(base, **changes)


def state(**changes) -> GuardState:
    base = GuardState(ceiling_w=6000, started_ts=T0, last_cut_ts=NOW - 3000)
    return replace(base, **changes)


def run(i: Inputs | None = None, st: GuardState | None = None, **settings) -> Decision:
    return decide(replace(SETTINGS, **settings), i or inputs(), st or state())


def test_full_rate_writes_nothing() -> None:
    d = run()
    assert d.status is Status.FULL_RATE
    assert d.ceiling_w == 6000
    assert d.write_w is None
    assert d.message == "FULL RATE; ceiling 6000 W; requested 6000 W; readback 6000 W"


def test_overload_cuts_in_proportion() -> None:
    # L3 at 16.4 A is 1.0 A over 15.4 A: cut 310 + 690 = 1000 W.
    d = run(inputs(phases=phases(l3=16.4)))
    assert d.status is Status.LIMITING
    assert d.ceiling_w == 5000
    assert d.write_w == 5000
    assert d.state.last_cut_ts == NOW


def test_small_overload_is_ignored() -> None:
    d = run(inputs(phases=phases(l3=15.55)))
    assert d.status is Status.FULL_RATE
    assert d.write_w is None


def test_no_second_cut_before_cut_interval() -> None:
    d = run(inputs(phases=phases(l3=16.4)), state(ceiling_w=5000, last_cut_ts=NOW - 5))
    assert d.status is Status.HOLDING
    assert d.ceiling_w == 5000


def test_no_second_cut_on_reading_from_before_the_battery_responded() -> None:
    # Reading taken 3 s after the last cut: the battery hasn't followed yet.
    last_cut = NOW - 20
    d = run(
        inputs(phases=phases(l3=16.4, ts=last_cut + 3), readback_w=5000),
        state(ceiling_w=5000, last_cut_ts=last_cut),
    )
    assert d.reason == "HOLD: waiting for a reading after the last cut"
    assert d.ceiling_w == 5000
    assert d.state.last_cut_ts == last_cut


def test_second_cut_on_fresh_reading() -> None:
    last_cut = NOW - 20
    d = run(
        inputs(phases=phases(l3=16.4, ts=last_cut + 9), readback_w=5000),
        state(ceiling_w=5000, last_cut_ts=last_cut),
    )
    assert d.status is Status.LIMITING
    assert d.ceiling_w == 4000


def test_cut_to_zero_is_paused() -> None:
    d = run(inputs(phases=phases(l3=25), readback_w=1000), state(ceiling_w=1000))
    assert d.status is Status.PAUSED
    assert d.ceiling_w == 0
    assert d.write_w == 0


def test_cooldown_holds_after_cut() -> None:
    d = run(
        inputs(readback_w=5000, actual_w=5000),
        state(ceiling_w=5000, last_cut_ts=NOW - 30),
    )
    assert d.reason == "HOLD: cooldown"
    assert d.write_w is None


def test_recovery_jumps_by_share_of_headroom() -> None:
    # Highest phase 10.4 A: 5 A headroom x 690 x 0.6 = 2070 W -> 5000 + 2070 = 7070,
    # capped at the 6000 W target.
    d = run(
        inputs(phases=phases(l3=10.4), readback_w=5000, actual_w=5000),
        state(ceiling_w=5000, last_cut_ts=NOW - 120),
    )
    assert d.status is Status.RECOVERING
    assert d.ceiling_w == 6000
    assert d.write_w == 6000
    assert d.state.last_recovery_ts == NOW


def test_recovery_step_is_partial_with_little_headroom() -> None:
    # 1 A headroom: 414 W -> 3000 + 414 = 3414 -> 3400 W.
    d = run(
        inputs(phases=phases(l3=14.4), readback_w=3000, actual_w=3000),
        state(ceiling_w=3000, last_cut_ts=NOW - 120),
    )
    assert d.ceiling_w == 3400


def test_recovery_waits_for_recovery_interval() -> None:
    d = run(
        inputs(phases=phases(l3=10.4), readback_w=3000, actual_w=3000),
        state(ceiling_w=3000, last_cut_ts=NOW - 120, last_recovery_ts=NOW - 30),
    )
    assert d.reason == "HOLD"
    assert d.ceiling_w == 3000


def test_recovery_waits_for_settling() -> None:
    d = run(
        inputs(phases=phases(l3=10.4), readback_w=3000, actual_w=3000, command_changed_ts=NOW - 10),
        state(ceiling_w=3000, last_cut_ts=NOW - 120),
    )
    assert d.reason == "HOLD"


def test_recovery_from_zero_skips_settling_and_interval() -> None:
    d = run(
        inputs(phases=phases(l3=10.4), readback_w=0, actual_w=0, command_changed_ts=NOW - 1),
        state(ceiling_w=0, last_cut_ts=NOW - 120, last_recovery_ts=NOW - 1),
    )
    assert d.status is Status.RECOVERING
    assert d.ceiling_w == 2000  # 5 A x 690 x 0.6 = 2070 -> 2000


def test_no_recovery_while_load_not_following() -> None:
    d = run(
        inputs(phases=phases(l3=10.4), readback_w=4000, actual_w=2000),
        state(ceiling_w=4000, last_cut_ts=NOW - 120),
    )
    assert d.reason == "HOLD: load not following command"
    assert d.ceiling_w == 4000


def test_tracking_not_checked_without_actual_sensor() -> None:
    d = run(
        inputs(phases=phases(l3=10.4), readback_w=4000, actual_w=None),
        state(ceiling_w=4000, last_cut_ts=NOW - 120),
        has_actual=False,
    )
    assert d.status is Status.RECOVERING


def test_missing_phase_while_active_pauses() -> None:
    p = phases()
    p["l2"] = PhaseSample(None, "", 0, 0, None)
    d = run(inputs(phases=p))
    assert d.status is Status.FAIL_SAFE
    assert d.ceiling_w == 0
    assert d.write_w == 0
    assert d.state.last_cut_ts == NOW


def test_missing_phase_with_hold_option_keeps_ceiling() -> None:
    p = phases()
    p["l2"] = PhaseSample(None, "", 0, 0, None)
    d = run(inputs(phases=p), pause_on_missing_data=False)
    assert d.status is Status.FAIL_SAFE
    assert d.ceiling_w == 6000
    assert d.write_w is None


def test_missing_data_while_inactive_keeps_ceiling() -> None:
    d = run(inputs(readback_w=None, active=False))
    assert d.status is Status.FAIL_SAFE
    assert d.ceiling_w == 6000
    assert d.write_w is None


def test_unknown_active_state_never_writes() -> None:
    d = run(inputs(active=None, phases=phases(l3=20)))
    assert d.status is Status.FAIL_SAFE
    assert d.write_w is None


def test_startup_hold() -> None:
    d = run(st=state(started_ts=NOW - 30))
    assert d.status is Status.STARTUP


def test_inactive_keeps_ceiling_and_never_writes() -> None:
    d = run(inputs(active=False, phases=phases(l3=20)), state(ceiling_w=3000))
    assert d.status is Status.INACTIVE
    assert d.ceiling_w == 3000
    assert d.write_w is None


def test_zero_target() -> None:
    d = run(inputs(target_w=0))
    assert d.status is Status.ZERO_REQUEST
    assert d.write_w == 0


def test_single_phase_load_ignores_other_phases() -> None:
    d = run(inputs(phases=phases(l3=20)), load_phases=("l1",), watts_per_amp=230)
    assert d.status is Status.FULL_RATE


def test_no_recovery_during_startup_cooldown() -> None:
    d = run(
        inputs(readback_w=3000, actual_w=3000, phases=phases(l3=10)),
        state(ceiling_w=3000, last_cut_ts=None, started_ts=NOW - 30),
        startup_hold_s=0,
    )
    assert d.reason == "HOLD: cooldown"
    assert d.state.last_cut_ts is None


def test_cut_allowed_right_after_startup() -> None:
    d = run(
        inputs(phases=phases(l3=16.4, ts=NOW)),
        state(last_cut_ts=None, started_ts=NOW),
        startup_hold_s=0,
    )
    assert d.status is Status.LIMITING
    assert d.ceiling_w == 5000


def test_setpoint_follows_lower_target_without_new_ceiling() -> None:
    d = run(inputs(target_w=4000))
    assert d.ceiling_w == 4000
    assert d.write_w == 4000


def test_second_cut_ignores_stale_high_reading() -> None:
    # One meter still shows 17.4 A from before the cut; a newer meter says 16.0 A.
    # The second cut is sized on the newer reading: 310 + 0.6 x 690 = 724 W.
    last_cut = NOW - 20
    p = phases()
    p["l3"] = PhaseSample(
        17.4, "sensor.dsmr", last_cut - 5, 2, 1.4, ((17.4, last_cut - 5), (16.0, last_cut + 10))
    )
    d = run(inputs(phases=p, readback_w=5000), state(ceiling_w=5000, last_cut_ts=last_cut))
    assert d.status is Status.LIMITING
    assert d.ceiling_w == 4200


def test_stale_high_reading_alone_does_not_cut_again() -> None:
    last_cut = NOW - 20
    p = phases()
    p["l3"] = PhaseSample(
        17.4, "sensor.dsmr", last_cut - 5, 2, 3.4, ((17.4, last_cut - 5), (14.0, last_cut + 10))
    )
    d = run(inputs(phases=p, readback_w=5000), state(ceiling_w=5000, last_cut_ts=last_cut))
    assert d.reason == "HOLD: waiting for a reading after the last cut"
    assert d.ceiling_w == 5000
