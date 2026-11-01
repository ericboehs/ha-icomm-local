/*
 * iCOMM Water Heater card: a Lovelace card for the icomm_local integration.
 *
 *   type: custom:icomm-water-heater-card
 *   entity: water_heater.garage
 *   power_entity: sensor.vue_garage_water_heater_power   # optional: a measured meter
 *
 * What it adds over the built-in thermostat card:
 * - +/- keep your target while you tap, then send once you stop (1.5 s).
 * - Write progress: queued, sent, heater got it, applied (from the tank's own
 *   telemetry), or "not applied". Shows the time of each step.
 * - Live watts and what's heating (compressor or element), time to recover,
 *   tank top/bottom and hot water level.
 * The sibling sensors are found on the water heater's device by their entity
 * ID suffix, so a renamed device still works.
 */

const DEBOUNCE_MS = 1500;
const HOLD_MS = 20000;            // keep the local target this long after sending
const RECENT_WRITE_S = 600;       // show the last write's steps for 10 min

const MODES = [
  { key: "heat_pump", label: "Heat pump", icon: "mdi:heat-pump-outline" },
  { key: "eco", label: "Hybrid", icon: "mdi:leaf" },
  { key: "electric", label: "Electric", icon: "mdi:lightning-bolt" },
  { key: "away", label: "Vacation", icon: "mdi:airplane" },
];
const MODE_LABEL = { heat_pump: "heat pump", eco: "hybrid", hybrid: "hybrid",
  electric: "electric", vacation: "vacation" };

const SUFFIXES = {
  power: "_power", recover: "_time_to_recover", deficit: "_heat_deficit",
  top: "_water_tank_top", bottom: "_water_tank_bottom", level: "_hot_water_level",
  state: "_operating_state", element: "_heating_element", pending: "_pending_write",
  health: "_relay_health", active: "_active_setpoint",
};

const fmtAgo = (ts) => {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)} h ago`;
};
const fmtClock = (ts) => ts
  ? new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })
  : "";
const fmtMin = (m) => {
  if (m <= 0) return "Full";
  if (m < 60) return `${Math.round(m)} min`;
  return `${Math.floor(m / 60)}h ${Math.round(m % 60)}m`;
};
const num = (s) => {
  const v = parseFloat(s?.state);
  return Number.isFinite(v) ? v : null;
};

class IcommWaterHeaterCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity || !config.entity.startsWith("water_heater.")) {
      throw new Error("Set entity: to a water_heater entity");
    }
    this._config = config;
    this._target = null;        // the target while you tap, or until the heater shows it
    this._sendTimer = null;
    this._sentAt = 0;
    this._modeSent = null;      // {key, at} until the entity shows it
    this._ids = null;
    if (this._root) this._render();
  }

  static getStubConfig(hass) {
    const wh = Object.keys(hass.states).find((e) => e.startsWith("water_heater."));
    return { entity: wh || "water_heater.garage" };
  }

  getCardSize() { return 7; }
  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }

  set hass(hass) {
    this._hass = hass;
    if (!this._root) this._build();
    if (!this._ids) this._ids = this._findSiblings();
    this._render();
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
      <ha-card>
        <div class="head">
          <div class="title"></div>
          <div class="pill"><span class="dot"></span><span class="pill-text"></span></div>
        </div>
        <div class="banner" hidden></div>
        <div class="main">
          <div class="tank" title="Tank top and bottom">
            <div class="tank-fill"></div>
            <div class="tank-top"></div>
            <div class="tank-bottom"></div>
          </div>
          <div class="center">
            <div class="label">Target</div>
            <div class="target"><span class="target-num"></span><span class="unit">°F</span></div>
            <div class="sub"></div>
            <div class="buttons">
              <button class="minus" aria-label="Lower target"><ha-icon icon="mdi:minus"></ha-icon></button>
              <button class="plus" aria-label="Raise target"><ha-icon icon="mdi:plus"></ha-icon></button>
            </div>
          </div>
        </div>
        <div class="write"></div>
        <div class="stats">
          <div class="stat power"><div class="k">Power</div><div class="v"></div><div class="n"></div></div>
          <div class="stat recover"><div class="k">Time to recover</div><div class="v"></div><div class="n"></div></div>
          <div class="stat water"><div class="k">Hot water</div><div class="v"></div><div class="n"></div></div>
        </div>
        <div class="modes"></div>
      </ha-card>`;
    const $ = (sel) => this._root.querySelector(sel);
    this.$ = $;
    $(".minus").addEventListener("click", () => this._step(-1));
    $(".plus").addEventListener("click", () => this._step(1));
    $(".title").addEventListener("click", () => this._moreInfo(this._config.entity));
    $(".power").addEventListener("click", () => this._moreInfo(this._ids.meter || this._ids.power));
    $(".recover").addEventListener("click", () => this._moreInfo(this._ids.recover));
    $(".water").addEventListener("click", () => this._moreInfo(this._ids.top));
    const modes = $(".modes");
    for (const m of MODES) {
      const b = document.createElement("button");
      b.className = "mode";
      b.dataset.mode = m.key;
      b.innerHTML = `<ha-icon icon="${m.icon}"></ha-icon><span>${m.label}</span>`;
      b.addEventListener("click", () => this._setMode(m.key));
      modes.appendChild(b);
    }
  }

  _moreInfo(entityId) {
    if (!entityId) return;
    this.dispatchEvent(new CustomEvent("hass-more-info",
      { detail: { entityId }, bubbles: true, composed: true }));
  }

  // --- actions --------------------------------------------------------------
  _step(dir) {
    const wh = this._hass.states[this._config.entity];
    if (!wh) return;
    const a = wh.attributes;
    const stepSize = a.target_temp_step || 1;
    const from = this._target ?? a.temperature;
    this._target = Math.min(a.max_temp, Math.max(a.min_temp, from + dir * stepSize));
    this._sentAt = 0;
    clearTimeout(this._sendTimer);
    this._sendTimer = setTimeout(() => this._send(), DEBOUNCE_MS);
    this._render();
  }

  _send() {
    this._sendTimer = null;
    const wh = this._hass.states[this._config.entity];
    if (!wh || this._target == null) return;
    if (this._target === wh.attributes.temperature) {
      this._target = null;      // back where it was: nothing to send
    } else {
      this._sentAt = Date.now();
      this._hass.callService("water_heater", "set_temperature",
        { entity_id: this._config.entity, temperature: this._target });
    }
    this._render();
  }

  _setMode(key) {
    const wh = this._hass.states[this._config.entity];
    if (!wh) return;
    const away = wh.attributes.away_mode === "on";
    if (key === "away") {
      this._hass.callService("water_heater", "set_away_mode",
        { entity_id: this._config.entity, away_mode: !away });
    } else {
      if (away) {
        this._hass.callService("water_heater", "set_away_mode",
          { entity_id: this._config.entity, away_mode: false });
      }
      if (wh.attributes.operation_mode === key && !away) return;
      this._hass.callService("water_heater", "set_operation_mode",
        { entity_id: this._config.entity, operation_mode: key });
    }
    this._modeSent = { key, at: Date.now() };
    this._render();
  }

  // --- rendering ------------------------------------------------------------
  _render() {
    if (!this._root || !this._hass || !this._config) return;
    const $ = this.$;
    const wh = this._hass.states[this._config.entity];
    if (!wh) {
      $(".title").textContent = `${this._config.entity} not found`;
      return;
    }
    const a = wh.attributes;
    $(".title").textContent = this._config.name || a.friendly_name || "Water heater";

    // The local target lasts while you tap, then until the heater shows it.
    if (this._target != null && !this._sendTimer && this._sentAt
        && (a.temperature === this._target || Date.now() - this._sentAt > HOLD_MS)) {
      this._target = null;
      this._sentAt = 0;
    }
    const target = this._target ?? a.temperature;
    $(".target-num").textContent = target ?? "–";
    $(".target").classList.toggle("editing", this._target != null);
    $(".minus").disabled = target <= a.min_temp;
    $(".plus").disabled = target >= a.max_temp;

    // State pill: what the tank is doing right now.
    const watts = num(this._s("meter")) ?? num(this._s("power")) ?? a.power_w;
    const opState = this._s("state")?.state;
    const element = this._s("element")?.state;
    const stale = a.stale === true;
    const unavailable = wh.state === "unavailable";
    let pill = "Idle", tone = "idle";
    if (unavailable) { pill = "Unavailable"; tone = "bad"; }
    else if (stale) { pill = "Data frozen"; tone = "warn"; }
    else if (opState === "fault") { pill = "Fault"; tone = "bad"; }
    else if (opState === "heating" || (watts ?? 0) > 50) {
      const by = element && element !== "off" ? `${element} element` : "heat pump";
      pill = `Heating · ${by}`; tone = "heat";
    }
    $(".pill-text").textContent = pill;
    $(".pill").className = `pill ${tone}`;

    // Banner: anything that makes the numbers less trustworthy.
    const health = this._s("health")?.state;
    let banner = "";
    if (stale) banner = "The heater has stopped sending fresh data. Values may be old; opening the A.O. Smith app usually wakes it.";
    else if (health && !["healthy", "unknown"].includes(health)) banner = `Relay: ${health}`;
    $(".banner").hidden = !banner;
    $(".banner").textContent = banner;

    // Tank: top/bottom and a fill that follows the average.
    const top = num(this._s("top"));
    const bottom = num(this._s("bottom"));
    $(".tank-top").textContent = top != null ? `${Math.round(top)}°` : "";
    $(".tank-bottom").textContent = bottom != null ? `${Math.round(bottom)}°` : "";
    const avg = a.water_average ?? a.current_temperature;
    const fill = avg != null ? Math.max(8, Math.min(100, ((avg - 70) / (a.max_temp - 70)) * 100)) : 0;
    $(".tank-fill").style.height = `${fill}%`;
    $(".tank").classList.toggle("heating", tone === "heat");

    // Under the target: the tank's average and the controller's active setpoint.
    const active = a.active_setpoint ?? num(this._s("active"));
    let sub = avg != null ? `Tank ${Math.round(avg)}°F` : "";
    if (active != null && active !== a.temperature) sub += ` · heating to ${Math.round(active)}°`;
    $(".sub").textContent = sub;

    this._renderWrite(a);

    // Stats.
    $(".power .v").textContent = watts != null ? `${Math.round(watts)} W` : "–";
    $(".power .n").textContent = tone === "heat"
      ? (element && element !== "off" ? `${element} element on` : "compressor running")
      : (stale ? "may be old" : "not heating");
    const recover = num(this._s("recover"));
    const deficit = num(this._s("deficit"));
    $(".recover .v").textContent = recover != null ? fmtMin(recover) : "–";
    $(".recover .n").textContent = deficit != null && deficit > 0
      ? `${(deficit / 1000).toFixed(1)} kWh to target` : (recover === 0 ? "at target" : "");
    const level = this._s("level")?.state;
    $(".water .v").textContent = level && !["unknown", "unavailable"].includes(level)
      ? level[0].toUpperCase() + level.slice(1) : "–";
    $(".water .n").textContent = top != null && bottom != null
      ? `top ${Math.round(top)}° · bottom ${Math.round(bottom)}°` : "";

    // Modes.
    const away = a.away_mode === "on";
    if (this._modeSent) {
      const k = this._modeSent.key;
      const landed = k === "away" ? away : (a.operation_mode === k && !away);
      if (landed || Date.now() - this._modeSent.at > HOLD_MS) this._modeSent = null;
    }
    for (const b of this._root.querySelectorAll(".mode")) {
      const k = b.dataset.mode;
      const on = k === "away" ? away : (!away && a.operation_mode === k);
      b.classList.toggle("on", on);
      b.classList.toggle("sending", this._modeSent?.key === k && !on);
      b.hidden = k !== "away" && !(a.operation_list || []).includes(k);
    }
  }

  _renderWrite(a) {
    const el = this.$(".write");
    const ps = this._s("pending");
    const pa = ps?.attributes || {};
    const w = pa.pending || pa.last_write;

    // Before the integration has it: your taps, then the service call.
    if (this._sendTimer) {
      el.className = "write local";
      el.innerHTML = `<ha-icon icon="mdi:gesture-tap"></ha-icon>
        <span>Will send <b>${this._target}°F</b> when you stop tapping…</span>`;
      return;
    }
    const fresh = w && w.queued && (pa.pending || Date.now() / 1000 - w.queued < RECENT_WRITE_S);
    if (this._sentAt && (!w || !w.queued || w.queued * 1000 < this._sentAt - 2000)) {
      el.className = "write local";
      el.innerHTML = `<ha-icon icon="mdi:send-clock"></ha-icon><span>Sending <b>${this._target}°F</b>…</span>`;
      return;
    }
    if (!w || !w.queued) {
      el.className = "write none";
      el.innerHTML = "";
      return;
    }
    const what = w.kind === "setpoint" ? `${Math.round(w.value)}°F`
      : w.kind === "mode" ? (MODE_LABEL[w.value] || w.value) : `${w.kind} ${w.value}`;
    const status = w.outcome || w.status || "queued";
    // The relay's outcomes: confirmed only from the tank's telemetry; the rest
    // retire a write that never showed up there (icomm_relay._expire_pending).
    const failed = ["not_applied", "rejected", "unconfirmed", "expired"].includes(status);
    const failLabel = { not_applied: "Not applied", rejected: "Rejected",
      unconfirmed: "Not confirmed", expired: "Expired" }[status];
    if (!fresh) {
      el.className = "write quiet";
      el.innerHTML = `<span>Last change: <b>${what}</b> · ${failed ? failLabel.toLowerCase() : "applied"} ${fmtAgo(w.confirmed || w.delivered || w.queued)}</span>`;
      return;
    }
    const steps = [
      { label: "Queued", at: w.queued },
      { label: "Sent", at: w.delivered, note: w.delivery_path === "lan" ? "local" : w.delivery_path },
      { label: "Heater got it", at: w.acked },
      { label: failed ? failLabel : "Applied", at: failed ? 0 : w.confirmed, failed },
    ];
    const current = steps.findIndex((s) => !s.at);
    el.className = `write steps ${failed ? "failed" : status === "confirmed" ? "done" : "busy"}`;
    el.innerHTML = `<div class="write-head">${failed ? failLabel : status === "confirmed" ? "Applied" : "Changing"}
        <b>${what}</b>${w.attempts > 1 ? ` · try ${w.attempts}` : ""}</div>
      <ol>${steps.map((s, i) => {
        const cls = s.failed ? "failed"
          : s.at ? "done" : i === current ? "now" : "";
        return `<li class="${cls}"><span class="mark"></span><span class="sl">${s.label}</span>
          <span class="st">${s.at ? fmtClock(s.at) : i === current && !failed ? "waiting…" : ""}</span></li>`;
      }).join("")}</ol>
      ${failed ? `<div class="hint">The tank's telemetry never showed it. If the data is frozen it may have applied anyway; it re-checks when fresh data arrives.</div>` : ""}`;
  }
}

const STYLE = `
  :host { display: block; }
  ha-card { padding: 16px; display: flex; flex-direction: column; gap: 14px; box-sizing: border-box; overflow: hidden; }
  button { font: inherit; color: inherit; cursor: pointer; }
  .head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .title { font-size: 1.15rem; font-weight: 500; cursor: pointer; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px;
    font-size: .85rem; background: var(--secondary-background-color); white-space: nowrap;
    flex-shrink: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  .pill .dot { flex-shrink: 0; width: 8px; height: 8px; border-radius: 50%; background: var(--disabled-text-color); }
  .pill.heat { background: rgba(255,152,0,.15); color: var(--orange-color, #ff9800); }
  .pill.heat .dot { background: var(--orange-color, #ff9800); animation: pulse 1.6s infinite; }
  .pill.warn { background: rgba(255,193,7,.18); } .pill.warn .dot { background: var(--warning-color, #ffc107); }
  .pill.bad { background: rgba(244,67,54,.15); color: var(--error-color, #f44336); }
  .pill.bad .dot { background: var(--error-color, #f44336); }
  @keyframes pulse { 50% { opacity: .35; } }
  .banner { padding: 8px 12px; border-radius: 8px; font-size: .85rem;
    background: rgba(255,193,7,.15); border-left: 3px solid var(--warning-color, #ffc107); }
  .main { display: flex; align-items: center; gap: 20px; justify-content: center; }
  .tank { flex-shrink: 0; position: relative; width: 56px; height: 150px; border-radius: 28px; overflow: hidden;
    border: 2px solid var(--divider-color); background: var(--secondary-background-color); cursor: default; }
  .tank-fill { position: absolute; bottom: 0; left: 0; right: 0; transition: height .6s;
    background: linear-gradient(to top, #4fa3e0, #f6a04d 60%, #e8603c); opacity: .85; }
  .tank.heating .tank-fill { animation: pulse 2.4s infinite; }
  .tank-top, .tank-bottom { position: absolute; left: 0; right: 0; text-align: center;
    font-size: .8rem; font-weight: 600; color: #fff; text-shadow: 0 1px 2px rgba(0,0,0,.5); }
  .tank-top { top: 12px; } .tank-bottom { bottom: 10px; }
  .center { display: flex; flex-direction: column; align-items: center; min-width: 0; flex: 1; max-width: 240px; }
  .label { color: var(--secondary-text-color); font-size: .9rem; }
  .target { font-size: 3.6rem; font-weight: 400; line-height: 1.1; transition: color .2s; }
  .target .unit { font-size: 1.2rem; vertical-align: top; margin-left: 2px; }
  .target.editing { color: var(--primary-color); }
  .sub { color: var(--secondary-text-color); font-size: .85rem; min-height: 1.2em; text-align: center; }
  .buttons { display: flex; gap: 20px; margin-top: 10px; }
  .buttons button { width: 52px; height: 52px; border-radius: 50%; display: grid; place-items: center;
    border: 1px solid var(--divider-color); background: none; }
  .buttons button:active { background: var(--secondary-background-color); }
  .buttons button:disabled { opacity: .35; cursor: default; }
  .write { border-radius: 10px; font-size: .9rem; }
  .write.none { display: none; }
  .write.local, .write.quiet { display: flex; align-items: center; gap: 8px; padding: 8px 12px;
    background: var(--secondary-background-color); color: var(--secondary-text-color); }
  .write.local { color: var(--primary-color); }
  .write.quiet { background: none; padding: 0 2px; font-size: .82rem; }
  .write.steps { padding: 10px 12px; background: var(--secondary-background-color); }
  .write.failed { background: rgba(244,67,54,.10); }
  .write-head { margin-bottom: 8px; }
  .write ol { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 4px; }
  .write li { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 3px;
    color: var(--secondary-text-color); font-size: .78rem; position: relative; }
  .write li:not(:last-child)::after { content: ""; position: absolute; top: 6px; left: calc(50% + 9px);
    right: calc(-50% + 9px); height: 2px; background: var(--divider-color); }
  .write li.done:not(:last-child)::after { background: var(--success-color, #43a047); }
  .mark { width: 12px; height: 12px; border-radius: 50%; border: 2px solid var(--divider-color); box-sizing: border-box; }
  .write li.done .mark { background: var(--success-color, #43a047); border-color: var(--success-color, #43a047); }
  .write li.now .mark { border-color: var(--primary-color); animation: pulse 1s infinite; }
  .write li.failed .mark { background: var(--error-color, #f44336); border-color: var(--error-color, #f44336); }
  .write li.done, .write li.now { color: var(--primary-text-color); }
  .st { font-size: .72rem; color: var(--secondary-text-color); min-height: 1em; }
  .hint { margin-top: 8px; font-size: .78rem; color: var(--secondary-text-color); }
  .stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
  .stat { padding: 10px; border-radius: 10px; background: var(--secondary-background-color); cursor: pointer; min-width: 0; }
  .stat .k { font-size: .75rem; color: var(--secondary-text-color); }
  .stat .v { font-size: 1.25rem; font-weight: 500; margin: 2px 0; }
  .stat .n { font-size: .72rem; color: var(--secondary-text-color); overflow: hidden; text-overflow: ellipsis; }
  .modes { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
  .mode { display: flex; flex-direction: column; align-items: center; gap: 4px; padding: 10px 4px;
    border-radius: 12px; border: 1px solid var(--divider-color); background: none; font-size: .8rem; }
  .mode.on { background: rgba(255,152,0,.15); border-color: var(--orange-color, #ff9800); color: var(--orange-color, #ff9800); }
  .mode.sending { border-style: dashed; border-color: var(--primary-color); animation: pulse 1s infinite; }
  .mode[hidden] { display: none; }
  @media (max-width: 420px) {
    .target { font-size: 3rem; } .main { gap: 14px; }
    .stat .v { font-size: 1.1rem; }
  }
`;

customElements.define("icomm-water-heater-card", IcommWaterHeaterCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "icomm-water-heater-card",
  name: "iCOMM Water Heater",
  description: "Target, write progress, live watts, time to recover and tank detail for icomm_local.",
  preview: true,
});
