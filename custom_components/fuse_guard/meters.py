"""Find meters among the current sensors, and check a phase mapping live.

Pure Python (no Home Assistant imports) so it can be unit-tested directly.

A "meter" is a set of current sensors whose entity ids differ only in the
phase: sensor.current_l1_house / _l2_ / _l3_, or
sensor.meter_phase_a_current_inv_1 / _b_ / _c_. Grouping by entity id instead
of by device also finds meters that have no device (e.g. YAML Modbus).
"""

from __future__ import annotations

import itertools
import statistics
from dataclasses import dataclass, field

PHASES = ("l1", "l2", "l3")
_NUMBERED = {"1": "l1", "2": "l2", "3": "l3", "l1": "l1", "l2": "l2", "l3": "l3"}
# Letters are a guess: many meters label phases A/B/C in a different order
# than the house's L1/L2/L3. The live check catches that.
_LETTERED = {"a": "l1", "b": "l2", "c": "l3"}

# Meters that read this far apart on the same phase are probably on different
# phases. Readings are not simultaneous (some meters report every ~40 s).
DISAGREE_A = 1.5
# A different phase order this close to the best one makes the best one a guess.
AMBIGUOUS_A = 1.0
# Sensors with these words in their entity id are derived values, not meters.
_NOT_METERS = {"headroom", "spread", "limit", "max", "min", "over", "fallback", "allowed"}


@dataclass(frozen=True)
class CurrentEntity:
    """A current sensor, as the flow sees it."""

    entity_id: str
    name: str = ""
    device_name: str | None = None


@dataclass
class Meter:
    """Current sensors that belong together, keyed by guessed phase."""

    key: str  # entity id with the phase token replaced by "*"
    label: str
    sensors: dict[str, str] = field(default_factory=dict)  # phase -> entity id
    lettered: bool = False  # phases came from A/B/C, so the order is a guess


def _phase_token(object_id: str) -> tuple[int, str, bool] | None:
    """(token index, phase, lettered) for the phase part of an object id."""
    tokens = object_id.split("_")
    for i, token in enumerate(tokens):
        if token in ("l1", "l2", "l3"):
            return i, token, False
        if token == "phase" and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            if nxt in _NUMBERED:
                return i + 1, _NUMBERED[nxt], False
            if nxt in _LETTERED:
                return i + 1, _LETTERED[nxt], True
    return None


def meter_key(entity_id: str) -> tuple[str, bool]:
    """(meter key, lettered) for a sensor; a sensor without a phase token is its own meter."""
    domain, _, object_id = entity_id.partition(".")
    found = _phase_token(object_id)
    if found is None:
        return entity_id, False
    index, _, lettered = found
    tokens = object_id.split("_")
    return f"{domain}." + "_".join([*tokens[:index], "*", *tokens[index + 1 :]]), lettered


def find_meters(entities: list[CurrentEntity], phases: int = 3) -> list[Meter]:
    """Group current sensors into meters that cover every phase."""
    groups: dict[str, Meter] = {}
    for entity in entities:
        object_id = entity.entity_id.partition(".")[2]
        found = _phase_token(object_id)
        if found is None or _NOT_METERS & set(object_id.split("_")):
            continue
        _, phase, lettered = found
        key, _ = meter_key(entity.entity_id)
        meter = groups.setdefault(key, Meter(key=key, label="", lettered=lettered))
        if phase in meter.sensors:  # two sensors claim one phase: not a clean meter
            meter.sensors[phase] = ""
        else:
            meter.sensors[phase] = entity.entity_id
        if not meter.label:
            meter.label = entity.device_name or key.partition(".")[2].replace("_", " ")
    wanted = set(PHASES[:phases])
    meters = [
        m for m in groups.values() if wanted <= set(m.sensors) and all(m.sensors[p] for p in wanted)
    ]
    for meter in meters:
        meter.sensors = {p: meter.sensors[p] for p in PHASES[:phases]}
    return sorted(meters, key=lambda m: m.label.lower())


@dataclass(frozen=True)
class Reading:
    """One configured sensor's state at check time."""

    entity_id: str
    name: str
    phase: str
    meter: str  # meter key, or the entity id for a sensor outside any meter
    value: float | None  # None: unavailable or not a number
    age_s: float | None  # since the sensor last reported
    unit: str | None
    lettered: bool = False


@dataclass
class CheckResult:
    """What the check page shows."""

    rows: list[tuple[str, str, str, str, str]]  # phase, sensor, reading, age, verdict
    problems: list[str]
    # entity id -> phase the readings suggest, for one meter that looks swapped
    suggestion: dict[str, str] | None = None
    suggestion_text: str = ""


def check(
    readings: list[Reading], freshness_s: float, max_plausible_a: float | None = None
) -> CheckResult:
    """Look for stale, wrong-unit, implausible, duplicated and swapped sensors."""
    problems: list[str] = []
    rows: list[tuple[str, str, str, str, str]] = []
    phases_of: dict[str, set[str]] = {}
    for r in readings:
        phases_of.setdefault(r.entity_id, set()).add(r.phase)

    def plausible(r: Reading) -> bool:
        return max_plausible_a is None or r.value is None or 0 <= r.value <= max_plausible_a

    usable = [
        r
        for r in readings
        if r.value is not None and r.age_s is not None and r.age_s <= freshness_s and plausible(r)
    ]
    by_phase = {p: [r.value for r in usable if r.phase == p] for p in PHASES}

    for r in sorted(readings, key=lambda r: (r.phase, r.name)):
        verdict = "✓"
        if r.value is None:
            verdict = "⚠ no reading"
            problems.append(f"{r.name} has no numeric reading right now.")
        elif r.age_s is None or r.age_s > freshness_s:
            verdict = "⚠ stale"
            problems.append(
                f"{r.name} last reported {_age(r.age_s)} ago, longer than the "
                f"{int(freshness_s)} s freshness timeout. The guard would ignore it."
            )
        elif not plausible(r):
            verdict = "⚠ implausible"
            problems.append(
                f"{r.name} reads {r.value:.1f} A, which can't be a phase current on this "
                "fuse. It may be an error code or a disconnected meter."
            )
        elif r.unit not in (None, "A"):
            verdict = "⚠ unit"
            problems.append(f"{r.name} reports in {r.unit}, not A.")
        elif len(phases_of[r.entity_id]) > 1:
            verdict = "⚠ on two phases"
            problems.append(f"{r.name} is picked for more than one phase.")
        else:
            others = [o.value for o in usable if o.phase == r.phase and o.entity_id != r.entity_id]
            if others and abs(r.value - statistics.median(others)) > DISAGREE_A:
                verdict = "⚠ disagrees"
        value = "–" if r.value is None else f"{r.value:.2f} A"
        rows.append((r.phase.upper(), r.name, value, _age(r.age_s), verdict))

    for phase in PHASES:
        values = by_phase[phase]
        if len(values) >= 2 and max(values) - min(values) > DISAGREE_A:
            problems.append(
                f"The {phase.upper()} sensors disagree by {max(values) - min(values):.1f} A. "
                "One of them may be on another phase."
            )

    result = CheckResult(rows, problems)
    _suggest(usable, result)
    if len({r.meter for r in readings}) < 2:
        result.problems.append(
            "With one meter, the phase mapping can't be cross-checked. Make sure "
            "its L1/L2/L3 match the house's phases."
        )
    return result


def _suggest(usable: list[Reading], result: CheckResult) -> None:
    """Find a meter whose phases fit the other meters better in another order."""
    meters: dict[str, list[Reading]] = {}
    for r in usable:
        meters.setdefault(r.meter, []).append(r)
    candidates = []
    for key, own in meters.items():
        others = [r for r in usable if r.meter != key]
        reference = {
            p: statistics.median([r.value for r in others if r.phase == p])
            for p in PHASES
            if any(r.phase == p for r in others)
        }
        phases = [r.phase for r in own]
        if len(own) < 2 or len(set(phases)) != len(phases) or not set(phases) <= set(reference):
            continue

        def cost(
            order: tuple[str, ...], own: list[Reading] = own, reference: dict = reference
        ) -> float:
            return sum(abs(r.value - reference[p]) for r, p in zip(own, order, strict=True))

        current = cost(tuple(phases))
        orders = sorted(itertools.permutations(phases), key=cost)
        best = orders[0]
        if current > DISAGREE_A and cost(best) < current - DISAGREE_A:
            # Another order nearly as good means two phases carry similar
            # currents right now, so the readings can't tell them apart.
            rivals = [o for o in orders[1:] if cost(o) < cost(best) + AMBIGUOUS_A]
            if rivals:
                unsure = sorted({p for o in rivals for p, q in zip(best, o, strict=True) if p != q})
                sure = [
                    f"{r.name} → {p.upper()}"
                    for r, p in zip(own, best, strict=True)
                    if all(o[i] == p for o in rivals for i in [own.index(r)])
                ]
                result.problems.append(
                    (f"{', '.join(sure)} fits the other meters. " if sure else "")
                    + f"{' and '.join(p.upper() for p in unsure)} carry similar currents "
                    "right now, so the readings can't tell which sensor belongs to which. "
                    "Read the sensors again when their loads differ, or set them by hand."
                )
                continue
            candidates.append((own[0].lettered, current - cost(best), key, own, best))
    if not candidates:
        return
    # With two meters either one could be "wrong"; one labelled A/B/C is the
    # likelier culprit. Among equals, take the biggest improvement.
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0] and len(meters) == 2:
        return  # can't tell which of two meters is wrong
    _, _, _, own, best = candidates[0]
    result.suggestion = {r.entity_id: p for r, p in zip(own, best, strict=True)}
    result.suggestion_text = ", ".join(
        f"{r.name} → {p.upper()}" for r, p in zip(own, best, strict=True) if r.phase != p
    )


def _age(age_s: float | None) -> str:
    if age_s is None:
        return "never"
    if age_s < 120:
        return f"{int(age_s)} s"
    return f"{int(age_s // 60)} min"


def markdown_table(result: CheckResult) -> str:
    lines = ["| Phase | Sensor | Reading | Reported | |", "|---|---|---|---|---|"]
    lines += [f"| {' | '.join(cell.replace('|', '/') for cell in row)} |" for row in result.rows]
    return "\n".join(lines)
