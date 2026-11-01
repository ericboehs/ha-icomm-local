/*
 * iCOMM Water Heater card: a Lovelace card for the icomm_local integration.
 *
 *   type: custom:icomm-water-heater-card
 *   entity: water_heater.garage
 *   power_entity: sensor.vue_garage_water_heater_power   # optional: a measured meter
 *
 * HA's own thermostat card (the native dial, +/- and mode bar) on top, then
 * what that card can't show:
 * - Write progress: queued, sent, heater got it, applied (from the tank's own
 *   telemetry), or "not applied", with the time of each step.
 * - Live watts and what's heating (compressor or element), time to recover,
 *   hot water level with tank top/bottom, and vacation.
 * The sibling sensors are found on the water heater's device by their entity
 * ID suffix, so a renamed device still works.
 */

const RECENT_WRITE_S = 600;       // show the last write's steps for 10 min

const MODES = ["heat_pump", "eco", "electric"];
const MODE_LABEL = { heat_pump: "heat pump", eco: "hybrid", hybrid: "hybrid",
  electric: "electric", vacation: "vacation" };

const SUFFIXES = {
  power: "_power", recover: "_time_to_recover", deficit: "_heat_deficit",
  top: "_water_tank_top", bottom: "_water_tank_bottom", level: "_hot_water_level",
  state: "_operating_state", element: "_heating_element", pending: "_pending_write",
  health: "_relay_health", active: "_active_setpoint",
};

const fmtMin = (m) => {
  if (m <= 0) return "Full";
  if (m < 60) return `${Math.round(m)} min`;
  return `${Math.floor(m / 60)}h ${Math.round(m % 60)}m`;
};
const num = (s) => {
  const v = parseFloat(s?.state);
  return Number.isFinite(v) ? v : null;
};

const APPLIED_SHOW_S = 30;        // how long "Applied" stays up

class IcommWaterHeaterCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity || !config.entity.startsWith("water_heater.")) {
      throw new Error("Set entity: to a water_heater entity");
    }
    this._config = config;
    this._ids = null;
    if (this._thermo) this._thermo.setConfig(this._thermoConfig());
    if (this._root) this._render();
  }

  static getStubConfig(hass) {
    const wh = Object.keys(hass.states).find((e) => e.startsWith("water_heater."));
    return { entity: wh || "water_heater.garage" };
  }

  getCardSize() { return 10; }
  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }

  set hass(hass) {
    this._hass = hass;
    if (!this._root) this._build();
    if (!this._ids) this._ids = this._findSiblings();
    if (this._thermo) this._thermo.hass = hass;
    this._render();
  }

  _thermoConfig() {
    const wh = this._hass?.states[this._config.entity];
    const list = wh?.attributes.operation_list || MODES;
    return {
      type: "thermostat",
      entity: this._config.entity,
      ...(this._config.name ? { name: this._config.name } : {}),
      features: [{ type: "water-heater-operation-modes",
        operation_modes: MODES.filter((m) => list.includes(m)) }],
    };
  }

  // --- the device's other entities --------------------------------------
  _findSiblings() {
    const ids = {};
    const reg = this._hass.entities || {};
    const device = reg[this._config.entity]?.device_id;
    const mine = device
      ? Object.values(reg).filter((e) => e.device_id === device).map((e) => e.entity_id)
      : [];
    const base = this._config.entity.split(".")[1];
    for (const [key, suffix] of Object.entries(SUFFIXES)) {
      ids[key] = mine.find((e) => e.startsWith("sensor.") && e.endsWith(suffix))
        || Object.keys(this._hass.states).find(
          (e) => e.startsWith("sensor.") && e.includes(base) && e.endsWith(suffix));
    }
    if (this._config.power_entity) ids.meter = this._config.power_entity;
    return ids;
  }

  _s(key) {
    const id = this._ids?.[key];
    return id ? this._hass.states[id] : undefined;
  }

  // --- DOM ----------------------------------------------------------------
  _build() {
    this._root = this.attachShadow({ mode: "open" });
    this._root.innerHTML = `
      <style>${STYLE}</style>
      <div class="stack">
        <div class="thermo"></div>
        <div class="write" hidden></div>
        <div class="banner" hidden></div>
        <div class="tiles">
          ${tile("power", "mdi:flash", "Power")}
          ${tile("recover", "mdi:timer-sand", "Time to recover")}
          ${tile("water", "mdi:water-thermometer", "Hot water")}
          ${tile("away", "mdi:airplane", "Vacation")}
        </div>
      </div>`;
    const $ = (sel) => this._root.querySelector(sel);
    this.$ = $;
    $(".t-power").addEventListener("click", () => this._moreInfo(this._ids.meter || this._ids.power));
    $(".t-recover").addEventListener("click", () => this._moreInfo(this._ids.recover));
    $(".t-water").addEventListener("click", () => this._moreInfo(this._ids.top));
    $(".t-away").addEventListener("click", () => this._toggleAway());
    this._loadThermostat();
  }

  async _loadThermostat() {
    // HA's own card, so the dial looks and behaves exactly like the built-in one.
    const helpers = await window.loadCardHelpers?.();
    if (!helpers) return;
    this._thermo = helpers.createCardElement(this._thermoConfig());
    if (this._hass) this._thermo.hass = this._hass;
    this.$(".thermo").appendChild(this._thermo);
  }

  _moreInfo(entityId) {
    if (!entityId) return;
    this.dispatchEvent(new CustomEvent("hass-more-info",
      { detail: { entityId }, bubbles: true, composed: true }));
  }

  _toggleAway() {
    const wh = this._hass.states[this._config.entity];
    if (!wh) return;
    this._hass.callService("water_heater", "set_away_mode",
      { entity_id: this._config.entity, away_mode: wh.attributes.away_mode !== "on" });
  }

  // --- rendering ------------------------------------------------------------
  _render() {
    if (!this._root || !this._hass || !this._config) return;
    const $ = this.$;
    const wh = this._hass.states[this._config.entity];
    if (!wh) return;
    const a = wh.attributes;

    const watts = num(this._s("meter")) ?? num(this._s("power")) ?? a.power_w;
    const opState = this._s("state")?.state;
    const element = this._s("element")?.state;
    const stale = a.stale === true;
    const heating = !stale && (opState === "heating" || (watts ?? 0) > 50);

    // Banner: anything that makes the numbers less trustworthy.
    const health = this._s("health")?.state;
    let banner = "";
    if (stale) banner = "The heater has stopped sending fresh data, so values may be old. Opening the A.O. Smith app usually wakes it.";
    else if (health && !["healthy", "unknown"].includes(health)) banner = `Relay: ${health}`;
    $(".banner").hidden = !banner;
    $(".banner").textContent = banner;

    this._renderWrite();

    setTile($(".t-power"), watts != null && !stale ? `${Math.round(watts)} W` : "–",
      heating ? (element && element !== "off" ? "element" : "compressor") : (stale ? "may be old" : "idle"),
      heating ? "heat" : "");
    const recover = num(this._s("recover"));
    const deficit = num(this._s("deficit"));
    setTile($(".t-recover"), recover != null ? fmtMin(recover) : "–",
      deficit != null && deficit > 0 ? `${(deficit / 1000).toFixed(1)} kWh` : "", recover ? "recover" : "");
    const level = this._s("level")?.state;
    const top = num(this._s("top"));
    const bottom = num(this._s("bottom"));
    setTile($(".t-water"), level && !["unknown", "unavailable"].includes(level)
      ? level[0].toUpperCase() + level.slice(1) : "–",
    top != null && bottom != null ? `${Math.round(top)}° / ${Math.round(bottom)}°` : "", "water");
    const away = a.away_mode === "on";
    setTile($(".t-away"), away ? "On" : "Off",
      away && a.vacation_days ? `${a.vacation_days} days` : "", away ? "away" : "");
  }

  _renderWrite() {
    // One line while a change is on its way; gone 30 s after it applies.
    const el = this.$(".write");
    const pa = this._s("pending")?.attributes || {};
    const w = pa.pending || pa.last_write;
    const nowS = Date.now() / 1000;
    if (!w || !w.queued || (!pa.pending && nowS - (w.confirmed || w.delivered || w.queued) > APPLIED_SHOW_S
        && (w.outcome || w.status) === "confirmed")
        || (!pa.pending && nowS - w.queued > RECENT_WRITE_S)) {
      el.hidden = true;
      return;
    }
    const what = w.kind === "setpoint" ? `${Math.round(w.value)}°F`
      : w.kind === "mode" ? (MODE_LABEL[w.value] || w.value) : `${w.kind} ${w.value}`;
    const status = w.outcome || w.status || "queued";
    const text = {
      queued: ["mdi:send-clock", `Sending ${what}…`, "busy"],
      delivered: ["mdi:send-clock", `Sent ${what}, waiting for the heater…`, "busy"],
      acknowledged: ["mdi:progress-check", `Heater got ${what}, waiting for the tank to show it…`, "busy"],
      confirmed: ["mdi:check-circle", `${what[0].toUpperCase() + what.slice(1)} applied`, "ok"],
      not_applied: ["mdi:alert-circle", `${what} not applied (the tank kept reporting the old value)`, "bad"],
      rejected: ["mdi:alert-circle", `${what} rejected by the heater`, "bad"],
      unconfirmed: ["mdi:alert-circle", `${what} not confirmed`, "bad"],
      expired: ["mdi:alert-circle", `${what} expired before the heater took it`, "bad"],
    }[status] || ["mdi:progress-clock", `${what}: ${status}`, "busy"];
    el.hidden = false;
    el.className = `write ${text[2]}`;
    el.innerHTML = `<ha-icon icon="${text[0]}"></ha-icon><span>${text[1]}</span>`;
  }
}

function tile(key, icon, name) {
  return `<div class="tile t-${key}" role="button" tabindex="0">
    <div class="ic"><ha-icon icon="${icon}"></ha-icon></div>
    <div class="tx"><div class="nm">${name}</div><div class="vl"><span class="v"></span><span class="n"></span></div></div>
  </div>`;
}

function setTile(el, value, note, tone) {
  el.querySelector(".v").textContent = value;
  el.querySelector(".n").textContent = note ? ` · ${note}` : "";
  el.dataset.tone = tone || "";
}

const STYLE = `
  :host { display: block; }
  .stack { display: flex; flex-direction: column; gap: 8px; }
  .write { display: flex; align-items: center; gap: 10px; padding: 10px 14px; border-radius: 12px;
    font-size: 14px; background: var(--card-background-color, #fff);
    border: 1px solid var(--ha-card-border-color, var(--divider-color)); }
  .write ha-icon { --mdc-icon-size: 20px; flex-shrink: 0; }
  .write.busy ha-icon { color: var(--primary-color); animation: pulse 1.2s infinite; }
  .write.ok ha-icon { color: var(--success-color, #43a047); }
  .write.bad { border-color: var(--error-color, #f44336); }
  .write.bad ha-icon { color: var(--error-color, #f44336); }
  .write[hidden], .banner[hidden] { display: none; }
  @keyframes pulse { 50% { opacity: .35; } }
  .banner { padding: 10px 14px; border-radius: 12px; font-size: 14px;
    background: rgba(255,193,7,.15); border-left: 4px solid var(--warning-color, #ffc107); }
  .tiles { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  .tile { display: flex; align-items: center; gap: 10px; padding: 10px; min-height: 56px; box-sizing: border-box;
    border-radius: var(--ha-card-border-radius, 12px); cursor: pointer;
    background: var(--card-background-color, #fff);
    border: 1px solid var(--ha-card-border-color, var(--divider-color)); }
  .ic { width: 36px; height: 36px; border-radius: 50%; display: grid; place-items: center; flex-shrink: 0;
    --c: var(--state-icon-color, #44739e); color: var(--c); background: color-mix(in srgb, var(--c) 20%, transparent); }
  .ic ha-icon { --mdc-icon-size: 22px; }
  .tile[data-tone=heat] .ic { --c: var(--orange-color, #ff9800); }
  .tile[data-tone=recover] .ic { --c: var(--amber-color, #ffc107); }
  .tile[data-tone=water] .ic { --c: var(--blue-color, #2196f3); }
  .tile[data-tone=away] .ic { --c: var(--purple-color, #9c27b0); }
  .tx { min-width: 0; }
  .nm { font-size: 14px; font-weight: 500; line-height: 20px; color: var(--primary-text-color);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .vl { font-size: 12px; line-height: 16px; color: var(--primary-text-color);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .vl .n { color: var(--secondary-text-color); }
`;

customElements.define("icomm-water-heater-card", IcommWaterHeaterCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "icomm-water-heater-card",
  name: "iCOMM Water Heater",
  description: "HA's thermostat dial plus write status, live watts, time to recover and tank detail for icomm_local.",
  preview: true,
});
