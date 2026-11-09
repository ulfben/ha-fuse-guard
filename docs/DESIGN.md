# Fuse Guard: design

Status: draft, 2026-11-03. Ported from a Home Assistant automation ("Fuse
Guard, proportional soft landing") that protects a 16 A main fuse at the
reference installation: a 3-phase house with a home battery controlled by evcc.

## 1. What it does

Fuse Guard keeps every phase of the main fuse under its rating by turning down
one controllable load: any Home Assistant `number` entity that sets the
power of that load (battery forced-charge power, heat pump power limit, and
so on). When a phase goes over the limit it cuts the setpoint in proportion to the
overload; when there is headroom again it gives the power back in steps.

It never switches the load off through any other path, never changes the
load's mode, and only writes while the load is active. That makes it safe to run
next to a scheduler like evcc that owns the load's mode.

### Non-goals (v1)

- On/off loads behind smart plugs, and several loads with priorities. Both are planned
  for v2 (section 10). The config already stores loads as a list, so v2 needs no
  migration.
- Export limiting.
- Being the sole Modbus writer for a device.
- Setpoints in amps (EV chargers). Planned for v1.1 (section 10).

### Prior art checked (2026-11-03)

- EVSE Load Balancer: per-phase, but EV chargers only and slow restore
  (15 min). Ideas borrowed for later: breaker-curve tolerance of short spikes,
  longer restore when usage is unstable.
- AvoidBlackout: total power only, on/off shedding.
- SolaX phase envelope protection: SolaX X3 only.

## 2. Configuration (UI)

Setup is a three-step config flow. Everything can be changed later from
**Configure** (options flow), which also has an **Advanced** step with the
timing constants.

### Step 1: Fuse

| Field | Type | Default | Notes |
|---|---|---|---|
| Name | text | "Fuse Guard" | Device and entity name prefix |
| Main fuse | A, 6–80 | 16 | |
| Phases | 1 or 3 | 3 | |
| Safety margin | A, 0–5 | 0.6 | Threshold = fuse − margin |
| Nominal voltage | V, 100–260 | 230 | Used to convert A to W |

### Step 2: Phase current sensors

One multi-entity picker per phase (`sensor`, device class `current`). A phase
reads as the **highest fresh value** among its sensors. "Fresh" means numeric,
≥ 0, not restored, and *reported* (not just changed) within the freshness
timeout, so a meter that keeps sending the same value stays fresh.

| Field | Default | Notes |
|---|---|---|
| L1 / L2 / L3 sensors | – | At least one per phase; unit must be A |
| Freshness timeout | 60 s | Per reading |

Phase mapping is the user's job: they pick whichever sensors measure
physical L1, L2, and L3, so inverters that label phases A/B/C need no extra option.

### Step 3: Controllable load

| Field | Required | Notes |
|---|---|---|
| Setpoint | yes | `number` entity in W. Becomes the entry's unique id, so two guards can't fight over one load |
| Target power | one of | Fixed W value **or** an entity (e.g. `input_number`, a sensor fed by evcc) giving the power the load *wants* |
| Active indicator | no | `binary_sensor` / `input_boolean` / `switch`. When set, the guard only writes while it is `on`. Without it the load counts as always active |
| Setpoint readback | no | Sensor that reports the value the device actually holds. Defaults to the number entity's own state |
| Actual power | no | Sensor of the load's real power, used to check the load follows the command before giving more |
| Actual power sign | no | "Positive while consuming" or "Negative while consuming" (Sungrow battery power is negative while charging) |
| Load phases | yes | "All phases" or one of L1/L2/L3. A single-phase load only reacts to its own phase |

Validation:
- margin < fuse;
- every phase has at least one sensor, with unit A;
- the number entity has unit W and a max ≥ step size;
- fixed target ≤ the number's max.

### Advanced (options only)

Defaults are the values the automation runs with today.

| Setting | Default | Meaning |
|---|---|---|
| Evaluation interval | 10 s | Watchdog tick (state changes also trigger evaluation) |
| Minimum overload | 0.2 A | Over-threshold amount that counts as real |
| Base cut | 310 W | Fixed part of each cut |
| Cut interval | 10 s | Minimum time between cuts |
| Response time | 8 s | A further cut needs a phase reading taken at least this long after the previous cut |
| Cooldown | 60 s | No recovery for this long after a cut |
| Settling time | 30 s | Setpoint must be unchanged this long before recovery |
| Recovery interval | 60 s | Minimum time between recovery steps |
| Recovery gain | 0.6 | Share of measured headroom given back per step |
| Step size | 100 W | Setpoint granularity |
| Startup hold | 60 s | After start or reload, data must be this old before the guard trusts it |
| On missing data | Pause load (0 W) | Or "Hold current setpoint". Only applies while the load is active |

## 3. Entities

All entities belong to one device per entry ("Fuse Guard – <name>").

| Entity | Type | State / purpose |
|---|---|---|
| Mode | `select` | `off` / `shadow` / `active`. Shadow computes and logs everything but never writes. Default after setup: **shadow** |
| Status | `sensor` (enum) | `off`, `startup`, `fail_safe`, `inactive`, `zero_request`, `paused`, `limiting`, `holding`, `recovering`, `full_rate`. The status names a phase that lasts many evaluations: `limiting` from a cut (or a start into an overload) through the wait for a fresh reading and the cooldown; a start with room goes straight to `recovering`, `recovering` while giving power back including the waits between steps, `holding` below target for any other reason (load not following, no room, startup cooldown). Attributes: `reason` (as today's status text), `summary` (plain-language line for dashboards), `ceiling_w`, `requested_w`, `readback_w` |
| Ceiling | `sensor` W | Current safe limit (restored across restarts) |
| Phase L1/L2/L3 current | `sensor` A | Highest fresh reading. Attributes: `source_entity`, `sample_timestamp`, `fresh_sources`, `total_sources`, `spread` |
| Highest phase current | `sensor` A | Max of the phases; this replaces `sensor.main_fuse_current_max` |
| Headroom | `sensor` A | Threshold − highest phase (negative when over) |
| Limiting | `binary_sensor` | On while ceiling < target |
| Cuts / Limited starts / Time over limit / Peak phase current (24 h) | `sensor` | Rolling 24 hours in per-minute buckets (`stats.py`), persisted with the guard's state. Rolling, not reset at midnight: a night of charging spans midnight. Over-limit time counts the gaps after readings over the guard's limit, ignoring gaps over 30 s (restarts). It uses each phase's newest reading, not the highest fresh one the guard decides on, which would keep a slow meter's spike "over" for up to the freshness window (2026.11.6); the peak stays the highest fresh reading. Added in 2026.11.5 so "how did last night go?" doesn't need a recorder query |
| Target power | `number` W | Only created when the target is a fixed value, so it can be changed from dashboards |

Every write fires a `fuse_guard_action` event and writes a logbook entry with
the same text as today's `input_text.sg_fuse_guard_msg`. The status sensor's
history replaces the input_text log.

Diagnostics download (`diagnostics.py`) dumps:
- the config;
- the last 50 decisions (ring buffer);
- the current phase sources.

## 4. Controller

### Structure

- `controller.py`: pure Python, with no Home Assistant imports. It defines:
  - `Settings`;
  - `PhaseSample(current, timestamp, source)`;
  - `Inputs` (phase samples, target, readback, actual power, active, now);
  - `GuardState` (ceiling, last cut time, last change time, last recovery
    time, startup time);
  - `Decision` (new ceiling, write value or None, status, reason, state
    updates).

  The main function is `decide(settings, inputs, state) -> Decision`, which
  is deterministic and unit-tested.
- `sources.py`: picks the highest fresh reading per phase from HA states (also pure,
  given state snapshots).
- `guard.py`: the HA glue. It:
  - subscribes to state changes of every source, the target, the readback, the
    actual power and the active indicator, plus a 10 s timer;
  - serialises evaluations with an `asyncio.Lock`;
  - builds `Inputs`, calls `decide`, calls `number.set_value` when needed, and
    updates the entities;
  - persists `GuardState` with `Store`, so the ceiling and cut clock survive
    restarts.

### Decision order (unchanged from the automation)

1. **Fail safe:** inputs invalid (a phase has no fresh reading, settings
   invalid, readback stale, or inside the startup hold). While active →
   0 W (or hold, per option); while inactive → keep the ceiling.
2. **Inactive:** load not active → keep the ceiling, write nothing.
3. **Zero request:** target ≤ 0 → ceiling 0.
3a. **Start:** the load is active now but wasn't at the last evaluation (or
   the guard just started, or inputs were invalid) → ceiling = min(target,
   room_A × W_per_A × gain), where room_A = headroom + the load's own draw ÷
   W_per_A (the start is seen through polled sensors, so the load may already
   be drawing). The ceiling from the last run is ignored: the house may be much
   busier now, and a scheduler starting the load writes its full power. A
   start into an overload stamps the cut clock, so the next cut waits for a
   reading taken after the load responded. Added in 2026.11.2 after the night of
   2026-11-05, when evcc twice restarted charging at 7000 W into a busy house
   and the phase reached 22 A for about 14 s before the first cut.
4. **Throttle:** overload is real (≥ minimum overload) **and** the cut
   interval has passed **and** a phase reading taken at least the response time
   after the last cut is over the limit (or there is no previous cut) →
   ceiling = baseline − (base cut + overload_A × W_per_A).
5. **Hold, waiting for a reading:** overload is real but no post-cut reading
   yet.
6. **Hold, cooldown:** within the cooldown after the last cut.
7. **Full rate:** baseline ≥ target.
8. **Hold, not following:** the actual power is below baseline −
   max(300 W, 15 %), when baseline ≥ 1000 W.
9. **Recover:** room ≥ step, settled (or baseline 0), recovery interval
   passed (or baseline 0) → ceiling = min(target, baseline +
   min(max(step, headroom_A × W_per_A × gain), room)), where room =
   headroom_A × W_per_A rounded down to whole steps. A step never takes more
   than the headroom: with whole steps only, the last one near the limit
   tipped the phases to 0–0.2 A over it, inside the min-over tolerance, until
   the house got busier and a cut followed (a sawtooth just above the limit,
   seen on 2026-11-08 with no margin).
10. Otherwise **hold**: `recovering` between steps while there is room,
    `holding` ("HOLD: at the limit") when there is none.

Definitions:
- baseline = min(ceiling, readback, target);
- W_per_A = nominal voltage × number of phases of the load (690 W for a 3-phase
  load at 230 V);
- written value = min(target, ceiling), rounded down to the step size.

A write only happens when the load is active, the mode is `active`, and the
value differs from the readback by at least one step (or is 0 while the readback isn't).

### Differences from the automation (deliberate)

- **Recovery cadence** is "at most once per recovery interval since the last
  change". The automation's `:00/:30` wall-clock ticks plus 30 s settling worked
  out to about one step per minute, so the default is 60 s.
- **The ceiling is internal state** persisted with `Store`, not an `input_number`.
  The last-cut timestamp is internal state too, not an `input_datetime`.
- **The startup hold** counts from integration setup, not from the automation's
  `last_changed`.
- **The overload trigger is native:** any state change of a phase source runs an
  evaluation, so no separate "over-limit" sensor is needed. The evaluation runs
  0.5 s after the first change of a burst (`BURST_WAIT_S`), so one meter's
  phases arrive together: a P1 reader updates its entities one by one within
  ~25 ms, and on 2026-11-07 a cut was sized from L2 while L3's higher reading
  was 2 ms away. An inverter polled per register spreads its phases over
  ~0.3 s typically and up to ~2.5 s; the wait covers nine bursts in ten.
- **The guard trusts its own write while it settles.** For `settling_s` after a write, it
  uses the value it wrote instead of the readback. The guard evaluates on every
  phase sample, often within a second of a write, before the device reports the new value.
  Otherwise it would undo its own recovery steps. The replay of 01:10 showed this.
- **A second cut is judged and sized on readings taken after the battery responded.**
  Each phase carries all of its fresh readings. A higher reading from before the last cut
  (e.g. DSMR, which reports about every 40 s) still counts as fresh, but it neither
  triggers nor sizes a cut. In the automation that one stale reading could hide a newer one
  that was still over the limit, and it made cuts deeper than needed.
- **Overload amounts are rounded to 0.01 A before comparing.** 15.6 − 15.4 is
  0.1999… in floating point.

### Replay results (tests/test_replay.py)

The replay simulates the battery responding to the controller's own commands, using
the recorded house load (section 7).

| Event | Old automation | Controller |
|---|---|---|
| 01:10 | 2 cuts, down to 5000 W | 1 cut to 5500 W, full rate again after 3 min |
| 03:18 | 1 cut to 5300 W | 1 cut to 5300 W, full rate after 1 min |
| 04:39 | 3 cuts, down to 3000 W | 1 cut to 5000 W, full rate after 1 min |

In every case no reading taken after the battery responded reached 16 A.

## 5. Package layout

```
custom_components/fuse_guard/
  __init__.py        setup/unload, forwards platforms, update listener
  manifest.json
  const.py           keys and defaults
  config_flow.py     config + options flow
  controller.py      pure decision logic
  sources.py         fresh-reading selection
  guard.py           HA runtime (subscriptions, timer, writes, Store)
  entity.py          base entity / device info
  sensor.py  binary_sensor.py  select.py  number.py
  diagnostics.py
  strings.json  translations/en.json
tests/
  test_controller.py  scenario tests, including replays of 2026-11-02 night
  test_sources.py
  test_config_flow.py
hacs.json  README.md  .github/workflows/{validate,tests}.yml
```

## 6. Migration at the reference installation

1. Install via HACS (custom repository) and configure:
   - phase sensors: Tibber, DSMR and Sungrow (B→L1, C→L2, A→L3);
   - setpoint: `number.battery_forced_charge_discharge_power_inv_1`;
   - readback: `sensor.battery_forced_charge_discharge_power_inv_1`;
   - actual: `sensor.battery_power_inv_1`, negative while consuming;
   - target: `input_number.sg_desired_charge_power`;
   - active: a new template binary sensor (EMS mode 2 and command 170).
2. Leave it in **shadow** mode for several charge windows. Compare its status
   history with `input_text.sg_fuse_guard_msg`.
3. Switch the automation off and the mode to **active**. Keep the automation
   (disabled) as a fallback for a few weeks.
4. Then retire the automation, the helpers it owned and the duplicated template
   sensors. Point the dashboard at the new entities.

## 7. Testing

- Controller unit tests cover every branch in the decision order.
- Replays use the 01:10, 03:18 and 04:39 events of the night of 2026-11-03,
  exported from the recorder as CSV fixtures. The replay closes the loop:
  - house load = recorded phase current − recorded battery draw at that
    sample's time;
  - a simulated battery follows the controller's writes (readback after 2 s,
    power after 8 s).

  This assumes the house doesn't react to the battery, which holds at night.
- Config flow tests use `pytest-homeassistant-custom-component`.
- CI runs:
  - hassfest;
  - HACS validation;
  - ruff;
  - pytest.

## 8. Release

- **Name:** "Fuse Guard" / `fuse_guard`. Checked 2026-11-04: no clash in the HACS
  default list, in Home Assistant core, or in home-assistant/brands.
- **Versioning:** CalVer `YYYY.MM.PATCH`, like Home Assistant itself. The first
  release is `2026.11.0`. Fixes in the same month bump the patch; a new month starts
  at `.0`. Each release is a `vYYYY.MM.PATCH` tag with a GitHub release, which is
  what HACS offers as updates. `manifest.json` carries the same version.

## 9. Open questions

- Should spike tolerance modelled on breaker curves (EVSE Load Balancer's "optimised" mode)
  be an option? It is out of scope for v1.

## 10. Later versions

- v1.1: setpoints in A, with a minimum setpoint (EV chargers below 6 A → 0).
- v2: several loads, each with a priority. There are two kinds:
  - **Adjustable** (today's `number` setpoint).
  - **Switched**: a `switch` for a smart plug in front of an appliance that
    resumes by itself after a power cut (dehumidifier, dishwasher, washing
    machine).

  On overload the guard sheds the lowest-priority loads first. That way a
  switched dishwasher can go before the battery charge is touched, or after it,
  as the user chooses.

  A switched load is configured with:
  - its phase(s) and expected power. The power is used to predict the relief; the
    plug's own power sensor is optional and gives the real value;
  - a **minimum off time** (e.g. 10 min). It stays off at least this long, then
    goes back on only if the headroom covers its expected power;
  - a **cycle limit**, e.g. at most 2 cuts per hour and 6 per day. Once used
    up, the guard skips this load and moves on to the next one;
  - **only cut while drawing power**, e.g. above 50 W. Cutting a dishwasher
    that has finished does nothing useful, and an idle appliance gains nothing
    from a power cycle.

  Switched loads are only turned back **on** by the guard if the guard turned
  them off. It must never switch on something the user turned off.
  Per-phase loads only count against their own phase.
