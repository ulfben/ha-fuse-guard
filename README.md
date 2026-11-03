# Fuse Guard

A Home Assistant integration that keeps every phase of your main fuse under
its rating by turning down one controllable load: a home battery's charge
power, a heat pump's power limit, anything with a `number` setpoint in watts.

- Reads each phase from as many meters as you have and uses the highest fresh
  reading.
- Cuts quickly and in proportion to the overload, then gives power back in
  steps once there is headroom.
- Only writes while the load is active, and never changes its mode, so it can run
  next to a scheduler such as evcc.
- Shadow mode lets you see what it would do before it touches anything.

> **Status: early development.** A new guard starts in **Shadow** mode: it
> decides and logs, but never writes. Compare its decisions with your current
> setup before switching the Mode select to **Active**. See
> [docs/DESIGN.md](docs/DESIGN.md).

## Entities

| Entity | What it shows |
|---|---|
| Mode | Off / Shadow (log only) / Active |
| Status | What the guard is doing; the `summary` attribute is a plain-language line for dashboards |
| Ceiling | The highest setpoint the guard currently allows (W) |
| L1/L2/L3 current | Highest fresh reading per phase, with its source |
| Highest phase current, Headroom | Worst phase, and how far below the limit it is |
| Limiting | On while the load is held below its target |
| Target power | Only when the target is a fixed value |

Every write appears in the logbook, and fires a `fuse_guard_action` event.

## Dashboard card

The integration ships its own card; no resource needs adding. Edit a
dashboard → Add card → **Fuse Guard**. It shows each phase against the fuse
(marker = guard limit), the load against its target, the status line, a
mode switch and, optionally, the last decisions.

```yaml
type: custom:fuse-guard-card
entity: sensor.house_status
show_log: true
```

## Installation (HACS)

1. HACS → ⋮ → Custom repositories → add `https://github.com/ulfben/ha-fuse-guard`
   as an *Integration*.
2. Install **Fuse Guard** and restart Home Assistant.
3. Settings → Devices & services → Add integration → **Fuse Guard**.

## Development

```bash
uv venv && uv pip install -r requirements_test.txt
uv run pytest
```
