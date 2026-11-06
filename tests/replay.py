"""Replay recorder exports through the controller, closing the loop.

The fixtures in tests/fixtures are recorder exports from the reference
installation (16 A fuse, 3 phases), taken while
the original automation was in control. Its writes are baked into the recorded
battery readback, battery power and phase currents. To judge the controller on
its own, the replay:

- takes the house load per phase as the recorded current minus the recorded
  battery draw (the battery charges on all three phases);
- simulates the battery: the readback follows each write after READBACK_LAG_S
  and the charge power after RESPONSE_LAG_S (measured: 7–10 s);
- feeds the controller house load plus simulated battery draw.

It assumes the house doesn't react to the battery, which is true at night.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

from custom_components.fuse_guard.controller import Decision, GuardState, Inputs, Settings, decide
from custom_components.fuse_guard.sources import PhaseSample

FIXTURES = Path(__file__).parent / "fixtures"

PHASE_ENTITIES = {
    "sensor.main_fuse_current_l1_virtual": "l1",
    "sensor.main_fuse_current_l2_virtual": "l2",
    "sensor.main_fuse_current_l3_virtual": "l3",
}
READBACK = "sensor.battery_forced_charge_discharge_power_inv_1"
ACTUAL = "sensor.battery_power_inv_1"  # negative while charging
TARGET = "input_number.sg_desired_charge_power"
EMS_MODE = "sensor.ems_mode_selection_raw_inv_1"
COMMAND = "sensor.battery_forced_charge_discharge_cmd_raw_inv_1"
CEILING = "input_number.sg_safe_charge_limit"
MESSAGE = "input_text.sg_fuse_guard_msg"

REFERENCE = Settings(
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


@dataclass(frozen=True)
class Row:
    ts: float
    entity_id: str
    state: str
    sample_ts: float | None


@dataclass(frozen=True)
class Step:
    ts: float
    decision: Decision
    phases: dict[str, PhaseSample]  # what the controller saw (simulated currents)


def load(name: str) -> list[Row]:
    with open(FIXTURES / name, newline="") as handle:
        return [
            Row(
                float(r["ts"]),
                r["entity_id"],
                r["state"],
                float(r["sample_ts"]) if r["sample_ts"] else None,
            )
            for r in csv.DictReader(handle)
        ]


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def recorded(rows: list[Row], entity_id: str) -> list[tuple[float, str]]:
    return [(r.ts, r.state) for r in rows if r.entity_id == entity_id]


READBACK_LAG_S = 2
RESPONSE_LAG_S = 8


class _Battery:
    """Setpoint register plus a charge power that follows it after a delay."""

    def __init__(self, setpoint: float, ts: float) -> None:
        self.writes: list[tuple[float, float]] = [(ts - 3600, setpoint)]

    def write(self, ts: float, value: float) -> None:
        self.writes.append((ts, value))

    def _at(self, ts: float) -> tuple[float, float]:
        """(value, time it was written) of the newest write at or before ts."""
        return next(((w_ts, v) for w_ts, v in reversed(self.writes) if w_ts <= ts))[::-1]

    def readback(self, ts: float) -> float:
        return self._at(ts - READBACK_LAG_S)[0]

    def readback_changed(self, ts: float) -> float:
        return self._at(ts - READBACK_LAG_S)[1] + READBACK_LAG_S

    def power(self, ts: float) -> float:
        return self._at(ts - RESPONSE_LAG_S)[0]


def replay(rows: list[Row], settings: Settings = REFERENCE, tick_s: float = 10) -> list[Step]:
    """Evaluate on every phase sample and on a fixed tick, like the guard will."""
    rows = sorted(rows, key=lambda r: r.ts)
    # The export starts each entity with its last state before the window; those
    # rows sit in the second before the window starts. Load them all first.
    start = math.floor(rows[0].ts) + 1
    latest: dict[str, Row] = {r.entity_id: r for r in rows if r.ts < start}
    rows = [r for r in rows if r.ts >= start]
    battery = _Battery(_number(latest[READBACK].state) or 0.0, start)
    mode_changed_ts = start - 3600
    state = GuardState(
        ceiling_w=_number(latest[CEILING].state) or settings.max_w,
        started_ts=start - 3600,
        last_cut_ts=start - 3600,
        was_active=latest[EMS_MODE].state == "2" and latest[COMMAND].state == "170",
    )
    steps: list[Step] = []

    # Recorded battery draw over time (charging is negative in the export).
    draws = [
        (r.ts, -(_number(r.state) or 0.0)) for r in [latest[ACTUAL], *rows] if r.entity_id == ACTUAL
    ]

    def recorded_draw(ts: float) -> float:
        """Battery draw when a phase sample was taken; samples can be ~40 s old."""
        return next((d for d_ts, d in reversed(draws) if d_ts <= ts), draws[0][1])

    def evaluate(now: float) -> None:
        nonlocal state
        phases = {}
        for entity_id, key in PHASE_ENTITIES.items():
            row = latest[entity_id]
            current = _number(row.state)
            sample_ts = row.sample_ts or row.ts
            if current is not None:
                house = current - recorded_draw(sample_ts) / settings.watts_per_amp
                current = max(0.0, house + battery.power(sample_ts) / settings.watts_per_amp)
            phases[key] = PhaseSample(current, entity_id, sample_ts, 1, None)
        active = latest[EMS_MODE].state == "2" and latest[COMMAND].state == "170"
        inputs = Inputs(
            now=now,
            phases=phases,
            target_w=_number(latest[TARGET].state),
            readback_w=battery.readback(now),
            actual_w=battery.power(now),
            active=active,
            command_changed_ts=max(mode_changed_ts, battery.readback_changed(now)),
        )
        decision = decide(settings, inputs, state)
        state = decision.state
        if decision.write_w is not None:
            battery.write(now, decision.write_w)
        steps.append(Step(now, decision, phases))

    next_tick = math.ceil(start / tick_s) * tick_s
    for row in rows:
        while next_tick < row.ts:
            evaluate(next_tick)
            next_tick += tick_s
        previous = latest.get(row.entity_id)
        latest[row.entity_id] = row
        if row.entity_id in (EMS_MODE, COMMAND) and previous and previous.state != row.state:
            mode_changed_ts = row.ts
        if row.entity_id in PHASE_ENTITIES:
            evaluate(row.ts)
    return steps


def ceiling_changes(steps: list[Step]) -> list[tuple[float, float, str]]:
    """(time, new ceiling, reason) for every step that changed the ceiling."""
    out: list[tuple[float, float, str]] = []
    last = None
    for step in steps:
        if step.decision.ceiling_w != last:
            out.append((step.ts, step.decision.ceiling_w, step.decision.reason))
            last = step.decision.ceiling_w
    return out
