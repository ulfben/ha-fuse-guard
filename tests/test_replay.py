"""Replays of the three overloads on one night at the reference installation.

The old automation was in control that night. At 04:39 it cut three times, to
3000 W, because it kept reacting to readings taken before the battery had
responded. These tests pin down what the controller does with the same house
load (see tests/replay.py for how the loop is closed).
"""

from __future__ import annotations

import pytest
from replay import CEILING, REFERENCE, ceiling_changes, load, recorded, replay

FUSE_A = 16.0

# fixture, recorded lowest ceiling, expected lowest ceiling
EVENTS = [
    ("night_0110.csv", 5000, 5500),
    ("night_0318.csv", 5300, 5300),
    ("night_0439.csv", 3000, 5000),
]


@pytest.mark.parametrize(("fixture", "recorded_min", "expected_min"), EVENTS)
def test_one_proportional_cut_then_full_recovery(
    fixture: str, recorded_min: float, expected_min: float
) -> None:
    rows = load(fixture)
    assert min(float(v) for _, v in recorded(rows, CEILING)) == recorded_min

    steps = replay(rows)
    changes = ceiling_changes(steps)
    cuts = [(ts, ceiling) for ts, ceiling, reason in changes if reason == "THROTTLE"]
    assert len(cuts) == 1
    cut_ts = cuts[0][0]
    assert min(ceiling for _, ceiling, _ in changes) == expected_min

    # Once the battery has responded, no reading reaches the fuse rating.
    after = [
        value
        for step in steps
        for sample in step.phases.values()
        if (value := sample.max_since(cut_ts + REFERENCE.response_s)) is not None
    ]
    assert max(after) < FUSE_A

    # Back at the full 6000 W within four minutes.
    back = [ts for ts, ceiling, _ in changes if ts > cut_ts and ceiling == 6000]
    assert back and back[0] - cut_ts < 240
    assert steps[-1].decision.ceiling_w == 6000


@pytest.mark.parametrize("fixture", [e[0] for e in EVENTS])
def test_no_write_flapping(fixture: str) -> None:
    """Each write should stick: no write is undone within the settling time."""
    steps = replay(load(fixture))
    writes = [(s.ts, s.decision.write_w) for s in steps if s.decision.write_w is not None]
    for (t1, w1), (t2, w2) in zip(writes, writes[1:], strict=False):
        assert t2 - t1 >= REFERENCE.settling_s or w2 < w1, (t1, w1, t2, w2)
