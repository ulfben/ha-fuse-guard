"""Decision logic for Fuse Guard.

Pure Python (no Home Assistant imports). `decide` is deterministic: same
settings, inputs and state give the same decision, so every branch can be
unit-tested and real nights can be replayed. The decision order is in
docs/DESIGN.md, section 4; it is ported from the automation it replaced
at the reference installation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import StrEnum

from .sources import PhaseSample

# Below this baseline the load's actual power is too noisy to judge tracking.
_TRACKING_MIN_W = 1000
# Tracking tolerance: actual may lag the command by max(300 W, 15 %).
_TRACKING_SLACK_W = 300
_TRACKING_SLACK_SHARE = 0.15


class Status(StrEnum):
    """Values of the Status sensor."""

    OFF = "off"
    STARTUP = "startup"
    FAIL_SAFE = "fail_safe"
    INACTIVE = "inactive"
    ZERO_REQUEST = "zero_request"
    PAUSED = "paused"
    LIMITING = "limiting"
    HOLDING = "holding"
    RECOVERING = "recovering"
    FULL_RATE = "full_rate"


@dataclass(frozen=True)
class Settings:
    """Everything from the config entry that the decision needs."""

    threshold_a: float  # fuse - margin
    watts_per_amp: float  # voltage x phases the load spans
    load_phases: tuple[str, ...]  # phases the load draws from
    max_w: float  # setpoint entity's max
    has_actual: bool  # an actual-power sensor is configured
    min_over_a: float
    base_drop_w: float
    cut_interval_s: float
    response_s: float
    cooldown_s: float
    settling_s: float
    recovery_interval_s: float
    recovery_gain: float
    step_w: float
    startup_hold_s: float
    pause_on_missing_data: bool


@dataclass(frozen=True)
class Inputs:
    """A snapshot of the world at one evaluation."""

    now: float
    phases: dict[str, PhaseSample]
    target_w: float | None  # None: target missing or not a number
    readback_w: float | None  # None: readback missing or stale
    actual_w: float | None  # consumption, sign-corrected; None: missing or stale
    active: bool | None  # None: active indicator missing or stale
    command_changed_ts: float  # last change of the readback or the active indicator


@dataclass(frozen=True)
class GuardState:
    """What the guard remembers between evaluations (persisted)."""

    ceiling_w: float
    started_ts: float
    last_cut_ts: float | None = None
    last_recovery_ts: float | None = None
    last_write_w: float | None = None
    last_write_ts: float | None = None
    # Whether the load was active at the last evaluation that knew. False after a
    # restart, so the first active evaluation always counts as a start.
    was_active: bool = False


@dataclass(frozen=True)
class Decision:
    """The result of one evaluation."""

    status: Status
    reason: str  # same wording as the automation's status text
    ceiling_w: float
    requested_w: float  # min(target, ceiling): what the setpoint should hold
    write_w: float | None  # None: leave the setpoint alone
    readback_w: float
    state: GuardState

    @property
    def message(self) -> str:
        """One-line log text, as the automation wrote to its input_text."""
        return (
            f"{self.reason}; ceiling {int(self.ceiling_w)} W; "
            f"requested {int(self.requested_w)} W; readback {int(self.readback_w)} W"
        )


def _floor_step(value: float, step: float) -> float:
    return math.floor(value / step) * step


def decide(settings: Settings, inputs: Inputs, state: GuardState) -> Decision:
    """Return what to do now (docs/DESIGN.md, section 4)."""
    s, i = settings, inputs
    now = i.now
    samples = [i.phases.get(p) for p in s.load_phases]

    # Validity of everything the decision depends on.
    grid_valid = all(p is not None and p.current is not None for p in samples)
    settings_valid = i.target_w is not None and 0 <= i.target_w and s.max_w >= s.step_w
    readback_valid = i.readback_w is not None and 0 <= i.readback_w <= s.max_w
    startup_ready = now - state.started_ts >= s.startup_hold_s
    active = i.active is True
    inputs_valid = (
        grid_valid and settings_valid and readback_valid and startup_ready and i.active is not None
    )

    target_w = _floor_step(min(i.target_w or 0, s.max_w), s.step_w)
    safe_limit_w = _floor_step(min(max(state.ceiling_w, 0), s.max_w), s.step_w)
    reported_w = i.readback_w if readback_valid else 0.0
    # A device reports a new setpoint some seconds after the write. Until it has
    # settled, trust our own write, or the guard would undo it on the next sample.
    write_pending = (
        state.last_write_w is not None
        and state.last_write_ts is not None
        and 0 <= now - state.last_write_ts < s.settling_s
    )
    readback_w = state.last_write_w if write_pending else reported_w

    currents = [p.current for p in samples if p is not None and p.current is not None]
    current_max = max(currents) if grid_valid else 0.0
    over_a = round(max(0.0, current_max - s.threshold_a), 2)
    over_is_real = grid_valid and settings_valid and over_a >= s.min_over_a
    headroom_a = s.threshold_a - current_max

    # A load that just turned on starts from what the headroom allows, not from
    # the ceiling it had when it last ran: the house may be much busier now, and
    # a scheduler starting the load writes its full power straight away. The
    # start is seen through polled sensors, so the load may already be drawing;
    # its own draw counts as room, or the start would be sized too low.
    starting = inputs_valid and active and not state.was_active
    if starting:
        own_a = max(0.0, i.actual_w) / s.watts_per_amp if i.actual_w is not None else 0.0
        room_a = max(0.0, headroom_a + own_a)
        start_w = _floor_step(room_a * s.watts_per_amp * s.recovery_gain, s.step_w)
        safe_limit_w = min(target_w, start_w)

    # The cut clock. Recovery also waits a cooldown after startup, but a cut is
    # allowed straight away.
    clock_valid = state.last_cut_ts is not None and 0 < state.last_cut_ts <= now
    last_cut = state.last_cut_ts if clock_valid else 0.0
    event_age = now - last_cut if clock_valid else 0.0
    in_cooldown = (clock_valid and event_age < s.cooldown_s) or (
        now - state.started_ts < s.cooldown_s
    )
    down_due = not clock_valid or event_age >= s.cut_interval_s
    # A further cut is judged only on readings taken after the last cut had time
    # to act. A higher reading from before the cut may still be fresh, but it
    # neither triggers nor sizes a cut.
    post_cut = [p.max_since(last_cut + s.response_s) for p in samples if p is not None]
    post_cut = [value for value in post_cut if value is not None]
    cut_over_a = round(max(post_cut) - s.threshold_a, 2) if post_cut else 0.0
    over_sample_fresh = cut_over_a >= s.min_over_a

    baseline_w = min(safe_limit_w, readback_w, target_w)
    actual_valid = i.actual_w is not None
    actual_w = max(0.0, i.actual_w) if i.actual_w is not None else 0.0
    if s.has_actual:
        slack = max(_TRACKING_SLACK_W, baseline_w * _TRACKING_SLACK_SHARE)
        tracking = actual_valid and (
            baseline_w < _TRACKING_MIN_W or actual_w >= max(1.0, baseline_w - slack)
        )
    else:
        tracking = True
    command_settled = now - i.command_changed_ts >= s.settling_s
    recovery_due = (
        state.last_recovery_ts is None or now - state.last_recovery_ts >= s.recovery_interval_s
    )
    recovery_allowed = (
        inputs_valid
        and active
        and not in_cooldown
        and headroom_a > 0
        and (command_settled or baseline_w == 0)
        and tracking
        and (recovery_due or baseline_w == 0)
    )
    recovery_jump_w = max(s.step_w, int(headroom_a * s.watts_per_amp * s.recovery_gain))
    drop_w = s.base_drop_w + cut_over_a * s.watts_per_amp
    cutting = over_is_real and down_due and over_sample_fresh

    if not inputs_valid:
        raw = 0.0 if active and s.pause_on_missing_data else safe_limit_w
    elif not active:
        raw = safe_limit_w
    elif target_w <= 0:
        raw = 0.0
    elif starting:
        # Already sized from the headroom, overload included; the readback may
        # still show what the load ran at last time.
        raw = safe_limit_w
    elif cutting:
        raw = baseline_w - drop_w
    elif recovery_allowed:
        raw = min(target_w, baseline_w + recovery_jump_w)
    else:
        raw = baseline_w
    ceiling_w = _floor_step(min(max(raw, 0.0), s.max_w), s.step_w)
    final_w = max(0.0, min(target_w, ceiling_w))

    cmd_changed = abs(final_w - readback_w) >= s.step_w or (final_w == 0 and readback_w != 0)
    write_w = final_w if active and cmd_changed else None

    if not inputs_valid:
        if not startup_ready:
            status, reason = Status.STARTUP, "FAIL SAFE: invalid/stale inputs or startup"
        else:
            status, reason = Status.FAIL_SAFE, "FAIL SAFE: invalid/stale inputs or startup"
    elif not active:
        status, reason = Status.INACTIVE, "INACTIVE: load not active"
    elif target_w <= 0:
        status, reason = Status.ZERO_REQUEST, "ZERO REQUEST"
    # The status names a phase, not a single evaluation: a cut lasts one
    # evaluation, but the load stays limited through the wait for a fresh
    # reading and the cooldown, then recovers over several steps. `reason` has
    # the detail of the evaluation itself.
    elif starting and ceiling_w < target_w:
        status, reason = Status.LIMITING, "START: limited by headroom"
    elif over_is_real and over_sample_fresh:
        status, reason = Status.LIMITING, "THROTTLE"
    elif over_is_real:
        status, reason = Status.LIMITING, "HOLD: waiting for a reading after the last cut"
    elif in_cooldown and baseline_w < target_w:
        after_cut = clock_valid and event_age < s.cooldown_s
        status = Status.LIMITING if after_cut else Status.HOLDING
        reason = "HOLD: cooldown"
    elif baseline_w >= target_w:
        status, reason = Status.FULL_RATE, "FULL RATE"
    elif not tracking:
        status, reason = Status.HOLDING, "HOLD: load not following command"
    elif recovery_allowed:
        status, reason = Status.RECOVERING, "RECOVER"
    else:
        # Between recovery steps, while there is room to give back.
        status = Status.RECOVERING if headroom_a > 0 else Status.HOLDING
        reason = "HOLD"
    if ceiling_w == 0 and status in (Status.LIMITING, Status.HOLDING) and active:
        status = Status.PAUSED

    # A start into an overload is a cut too: the next cut waits for a reading
    # taken after the load responded.
    stamp = (not inputs_valid and active) or (active and cutting) or (starting and over_is_real)
    new_state = replace(
        state,
        ceiling_w=ceiling_w,
        last_cut_ts=now if stamp else state.last_cut_ts,
        last_recovery_ts=(
            now
            if inputs_valid and active and recovery_allowed and ceiling_w > baseline_w
            else state.last_recovery_ts
        ),
        last_write_w=write_w if write_w is not None else state.last_write_w,
        last_write_ts=now if write_w is not None else state.last_write_ts,
        was_active=inputs_valid and active,
    )
    return Decision(status, reason, ceiling_w, final_w, write_w, reported_w, new_state)
