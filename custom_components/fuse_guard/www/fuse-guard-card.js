/*
 * Fuse Guard card. Served by the fuse_guard integration, which also adds it as a
 * dashboard resource (you never add one by hand). Config:
 *
 *   type: custom:fuse-guard-card
 *   entity: sensor.house_status   # the guard's Status sensor
 *   name: Main fuse               # optional
 *   show_log: true                # optional: last decisions
 *   show_meters: true             # optional: which meter each phase uses
 *
 * The card finds the guard's other entities (mode, phases, ceiling) through
 * the entity registry: same device, keyed by translation key.
 */

const DOMAIN = "fuse_guard";
const MODES = [
  ["off", "Off"],
  ["shadow", "Shadow"],
  ["active", "Active"],
];
const STATUS_COLOR = {
  off: "var(--disabled-text-color)",
  startup: "var(--secondary-text-color)",
  fail_safe: "var(--error-color)",
  inactive: "var(--secondary-text-color)",
  zero_request: "var(--secondary-text-color)",
  paused: "var(--warning-color)",
  limiting: "var(--warning-color)",
  holding: "var(--warning-color)",
  recovering: "var(--info-color, var(--primary-color))",
  full_rate: "var(--success-color)",
};

// The load isn't drawing in these, so the allowed power is only a figure.
const LOAD_IDLE = new Set(["off", "inactive", "zero_request"]);

const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

// Format a time the way the user's HA profile asks: time_format is
// "language" (the language's default), "system" (the browser's), "12" or "24",
// and time_zone "server" shows the server's clock instead of the browser's.
function formatDuration(seconds) {
  const s = Math.round(Number(seconds) || 0);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  return s % 60 ? `${m} min ${s % 60} s` : `${m} min`;
}

function formatTime(hass, iso) {
  const locale = hass.locale || {};
  const options = { hour: "2-digit", minute: "2-digit", second: "2-digit" };
  if (locale.time_format === "12") options.hour12 = true;
  if (locale.time_format === "24") options.hourCycle = "h23";
  if (locale.time_zone === "server" && hass.config?.time_zone) options.timeZone = hass.config.time_zone;
  const language = locale.time_format === "system" ? undefined : locale.language;
  try {
    return new Date(iso).toLocaleTimeString(language, options);
  } catch {
    return new Date(iso).toLocaleTimeString(undefined, options);
  }
}

const number = (state) => {
  const value = Number.parseFloat(state?.state);
  return Number.isFinite(value) ? value : null;
};

function guardEntities(hass, statusEntityId) {
  const registry = hass.entities || {};
  const deviceId = registry[statusEntityId]?.device_id;
  const byKey = {};
  if (!deviceId) return byKey;
  for (const entry of Object.values(registry)) {
    if (entry.platform === DOMAIN && entry.device_id === deviceId && entry.translation_key) {
      byKey[entry.translation_key] = entry.entity_id;
    }
  }
  return byKey;
}

function firstStatusEntity(hass) {
  return Object.values(hass.entities || {}).find(
    (e) => e.platform === DOMAIN && e.translation_key === "status",
  )?.entity_id;
}

class FuseGuardCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement("fuse-guard-card-editor");
  }

  static getStubConfig(hass) {
    return { entity: firstStatusEntity(hass) || "", show_log: false };
  }

  setConfig(config) {
    if (!config.entity) throw new Error("Pick the Fuse Guard Status sensor (entity).");
    this._config = config;
    this._signature = null;
    if (this._hass) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._config) this._render();
  }

  getCardSize() {
    return 4 + (this._config?.show_log ? 3 : 0) + (this._config?.show_meters ? 2 : 0);
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  _render() {
    const hass = this._hass;
    const status = hass.states[this._config.entity];
    const ids = guardEntities(hass, this._config.entity);
    const watched = [this._config.entity, ...Object.values(ids)].map((id) => hass.states[id]);
    // Only redraw when one of our entities changed.
    const signature = watched.map((s) => s?.last_updated ?? "-").join("|");
    if (signature === this._signature) return;
    this._signature = signature;

    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    if (!status) {
      this.shadowRoot.innerHTML = `<ha-card><div class="warning">Entity not found: ${escapeHtml(this._config.entity)}</div></ha-card>${STYLE}`;
      return;
    }

    const a = status.attributes;
    const fuse = Number(a.fuse_a) || 16;
    const threshold = Number(a.threshold_a) || fuse;
    const mode = hass.states[ids.mode]?.state ?? a.mode;
    const deviceId = hass.entities?.[this._config.entity]?.device_id;
    const title = this._config.name || hass.devices?.[deviceId]?.name || "Fuse Guard";
    const color = STATUS_COLOR[status.state] || "var(--primary-text-color)";

    // Each bar ends at the fuse rating, shown beside the value. A marker shows
    // the guard's limit only when a margin puts it below the fuse.
    const hasMarker = threshold < fuse;

    const phases = ["l1", "l2", "l3"]
      .filter((key) => ids[`current_${key}`])
      .map((key) => {
        const state = hass.states[ids[`current_${key}`]];
        const current = number(state);
        const source = state?.attributes?.source_entity;
        const sourceName = hass.states[source]?.attributes?.friendly_name || source || "";
        const level =
          current === null ? "unknown" : current >= fuse ? "over" : current >= threshold ? "near" : "ok";
        const width = current === null ? 0 : Math.min(100, (current / fuse) * 100);
        return `
          <div class="phase" title="${escapeHtml(sourceName)}">
            <span class="label">${key.toUpperCase()}</span>
            <div class="track">
              <div class="fill ${level}" style="width:${width}%"></div>
              ${hasMarker ? `<div class="marker" style="left:${(threshold / fuse) * 100}%"></div>` : ""}
            </div>
            <span class="value">${current === null ? "–" : current.toFixed(1)}<span class="of"> / ${fuse} A</span></span>
          </div>`;
      })
      .join("");

    // What the guard lets the load use, against its target. Not what the load
    // draws: greyed out while the load is off, or it would look like it runs.
    const ceiling = number(hass.states[ids.ceiling]);
    const target = Number(a.target_w);
    const idle = LOAD_IDLE.has(status.state);
    const loadRow =
      ceiling !== null && target > 0
        ? `<div class="phase load${idle ? " idle" : ""}" title="${
            idle ? "The load is off; this is what it may use when it starts" : "What the guard lets the load use"
          }">
             <span class="label">Allowed</span>
             <div class="track"><div class="fill ${idle ? "idle" : ceiling < target ? "near" : "ok"}"
               style="width:${Math.min(100, (ceiling / target) * 100)}%"></div></div>
             <span class="value">${(Math.min(ceiling, target) / 1000).toFixed(1)}<span class="of"> / ${(target / 1000).toFixed(1)} kW</span></span>
           </div>`
        : "";

    // Which meter each phase uses, how many of its meters are fresh, and how far
    // they disagree: the first thing to look at when a reading seems off.
    const meterName = (entityId) => {
      const entry = hass.entities?.[entityId];
      const device = entry?.device_id ? hass.devices?.[entry.device_id] : undefined;
      return (
        device?.name_by_user || device?.name || hass.states[entityId]?.attributes?.friendly_name || entityId || "–"
      );
    };
    const meters = this._config.show_meters
      ? `<div class="meters">${["l1", "l2", "l3"]
          .filter((key) => ids[`current_${key}`])
          .map((key) => {
            const attrs = hass.states[ids[`current_${key}`]]?.attributes || {};
            const fresh = attrs.fresh_sources ?? "–";
            const total = attrs.total_sources ?? "–";
            const spread = attrs.spread != null ? `±${Number(attrs.spread).toFixed(2)} A` : "";
            return `<span class="label">${key.toUpperCase()}</span>
              <span class="meter" title="${escapeHtml(attrs.source_entity)}">${escapeHtml(
                attrs.source_entity ? meterName(attrs.source_entity) : "–",
              )}</span>
              <span class="value${fresh !== total ? " stale" : ""}" title="Meters that reported recently">${fresh}/${total}</span>
              <span class="value" title="Disagreement between the fresh meters">${spread}</span>`;
          })
          .join("")}</div>`
      : "";

    const log =
      this._config.show_log && Array.isArray(a.recent) && a.recent.length
        ? `<ul class="log">${a.recent
            .map((r) => {
              const time = formatTime(hass, r.time);
              return `<li><span class="time">${escapeHtml(time)}</span>${escapeHtml(r.message)}</li>`;
            })
            .join("")}</ul>`
        : "";

    // What the guard did over the last 24 hours: "how did last night go?"
    const day = a.last_24h;
    const dayLine = day
      ? `<div class="legend day">Last 24 h: ${day.cuts} ${day.cuts === 1 ? "cut" : "cuts"} · ${formatDuration(
          day.over_s,
        )} over the limit${
          day.peak_a != null && day.peak_ts
            ? ` · peak ${Number(day.peak_a).toFixed(1)} A at ${escapeHtml(
                formatTime(hass, new Date(day.peak_ts * 1000).toISOString()),
              )}`
            : ""
        }</div>`
      : "";

    const modes = ids.mode
      ? `<div class="modes" role="group" aria-label="Mode">${MODES.map(
          ([value, label]) =>
            `<button data-mode="${value}" class="${value === mode ? "on" : ""}" aria-pressed="${value === mode}">${label}</button>`,
        ).join("")}</div>`
      : "";

    this.shadowRoot.innerHTML = `
      <ha-card>
        <div class="header">
          <div class="title">${escapeHtml(title)}</div>
          ${modes}
        </div>
        <button class="summary" style="--status-color:${color}">
          <ha-icon icon="mdi:fuse"></ha-icon>
          <span>${escapeHtml(a.summary || status.state)}</span>
        </button>
        <div class="phases">${phases}${loadRow}</div>
        ${hasMarker ? `<div class="legend">Marker: guard limit ${threshold.toFixed(1)} A</div>` : ""}
        ${dayLine}
        ${meters}
        ${log}
      </ha-card>${STYLE}`;

    this.shadowRoot.querySelector(".summary").addEventListener("click", () => {
      this.dispatchEvent(
        new CustomEvent("hass-more-info", {
          detail: { entityId: this._config.entity },
          bubbles: true,
          composed: true,
        }),
      );
    });
    this.shadowRoot.querySelectorAll(".modes button").forEach((button) =>
      button.addEventListener("click", () => this._setMode(ids.mode, button.dataset.mode, mode)),
    );
  }

  _setMode(entityId, option, current) {
    if (option === current) return;
    if (
      option === "active" &&
      !window.confirm("Let Fuse Guard change the load's setpoint? In Shadow mode it only logs.")
    ) {
      return;
    }
    this._hass.callService("select", "select_option", { entity_id: entityId, option });
  }
}

const STYLE = `<style>
  ha-card { padding: 16px; }
  .header { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
  .title { font-size: 1.15em; font-weight: 500; }
  .modes { display: inline-flex; border: 1px solid var(--divider-color); border-radius: 18px; overflow: hidden; }
  .modes button { border: 0; background: none; color: var(--secondary-text-color); padding: 4px 12px;
    font: inherit; font-size: 0.85em; cursor: pointer; }
  .modes button.on { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .summary { display: flex; align-items: center; gap: 8px; margin: 12px 0; padding: 0; border: 0;
    background: none; font: inherit; color: var(--primary-text-color); cursor: pointer; text-align: left; }
  .summary ha-icon { color: var(--status-color); }
  .phases { display: grid; gap: 8px; }
  .phase { display: grid; grid-template-columns: 3em 1fr 7.5em; align-items: center; gap: 8px; }
  .label { color: var(--secondary-text-color); font-size: 0.9em; }
  .value { text-align: right; font-variant-numeric: tabular-nums; }
  .track { position: relative; height: 10px; border-radius: 5px; background: var(--secondary-background-color, rgba(127,127,127,0.15)); overflow: hidden; }
  .fill { height: 100%; border-radius: 5px; transition: width 0.4s ease; }
  .fill.ok { background: var(--success-color); }
  .fill.near { background: var(--warning-color); }
  .fill.over { background: var(--error-color); }
  .marker { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--primary-text-color); opacity: 0.6; }
  .load { margin-top: 4px; }
  .fill.idle { background: var(--disabled-text-color, rgba(127,127,127,0.5)); }
  .load.idle .value { color: var(--secondary-text-color); }
  .meters { display: grid; grid-template-columns: 3em 1fr auto auto; gap: 4px 12px; align-items: baseline;
    margin: 12px 0 0; padding: 8px 0 0; border-top: 1px solid var(--divider-color); font-size: 0.85em; }
  .meter { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meters .value.stale { color: var(--warning-color); }
  .value .of { font-size: 0.8em; color: var(--secondary-text-color); }
  .legend { margin-top: 8px; font-size: 0.8em; color: var(--secondary-text-color); }
  .log { list-style: none; margin: 12px 0 0; padding: 8px 0 0; border-top: 1px solid var(--divider-color);
    font-size: 0.85em; display: grid; gap: 4px; }
  .log .time { color: var(--secondary-text-color); margin-right: 8px; font-variant-numeric: tabular-nums; }
  .warning { color: var(--error-color); }
</style>`;

class FuseGuardCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.schema = [
        {
          name: "entity",
          required: true,
          selector: { entity: { filter: { integration: DOMAIN, domain: "sensor", device_class: "enum" } } },
        },
        { name: "name", selector: { text: {} } },
        { name: "show_meters", selector: { boolean: {} } },
        { name: "show_log", selector: { boolean: {} } },
      ];
      this._form.computeLabel = (field) =>
        ({
          entity: "Fuse Guard status sensor",
          name: "Title",
          show_meters: "Show meters per phase",
          show_log: "Show recent decisions",
        })[field.name];
      this._form.addEventListener("value-changed", (event) => {
        this.dispatchEvent(
          new CustomEvent("config-changed", { detail: { config: event.detail.value }, bubbles: true, composed: true }),
        );
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = this._config;
  }
}

if (!customElements.get("fuse-guard-card")) {
  customElements.define("fuse-guard-card", FuseGuardCard);
  customElements.define("fuse-guard-card-editor", FuseGuardCardEditor);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "fuse-guard-card",
    name: "Fuse Guard",
    description: "Phase currents against the main fuse, what the guard is doing, and its mode.",
    preview: true,
    documentationURL: "https://github.com/ulfben/ha-fuse-guard",
  });
}
