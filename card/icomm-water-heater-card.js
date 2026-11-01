/*
 * iCOMM Water Heater card: one Lovelace card for the icomm_local integration.
 *
 *   type: custom:icomm-water-heater-card
 *   entity: water_heater.garage
 *   name: Water Heater                                   # optional
 *   power_entity: sensor.vue_garage_water_heater_power   # optional: a measured meter
 *
 * HA's own dial (ha-control-circular-slider), with:
 * - a spinner beside "Target" from your first tap until the tank's own
 *   telemetry shows the new value (or a red line if it never does);
 * - power, time to recover and hot water available underneath;
 * - the mode bar, with the same spinner on a mode that's on its way.
 * The sibling sensors are found on the water heater's device by entity ID suffix.
 */

const VERSION = "0.5.1";
const DEBOUNCE_MS = 1000;          // the built-in dial's debounce
const FAIL_SHOW_S = 600;           // a failed write stays visible this long
const MODES = [
  { key: "heat_pump", icon: "mdi:heat-pump", label: "Heat pump" },
  { key: "eco", icon: "mdi:leaf", label: "Hybrid" },
  { key: "electric", icon: "mdi:flash", label: "Electric" },
  { key: "vacation", icon: "mdi:palm-tree", label: "Vacation" },
];
const MODE_LABEL = { heat_pump: "Heat pump", eco: "Hybrid", hybrid: "Hybrid",
  electric: "Electric", vacation: "Vacation" };
const SUFFIXES = {
  power: "_power", recover: "_time_to_recover", deficit: "_heat_deficit",
  avail: "_hot_water_availability", state: "_operating_state",
  element: "_heating_element", pending: "_pending_write", health: "_relay_health",
};
const FAILED = { not_applied: "not applied", rejected: "rejected by the heater",
  unconfirmed: "not confirmed", expired: "expired" };

const num = (s) => {
  const v = parseFloat(s?.state);
  return Number.isFinite(v) ? v : null;
};
const fmtMin = (m) => (m <= 0 ? "Full" : m < 60 ? `${Math.round(m)} min`
  : `${Math.floor(m / 60)}h ${Math.round(m % 60)}m`);

class IcommWaterHeaterCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity || !config.entity.startsWith("water_heater.")) {
      throw new Error("Set entity: to a water_heater entity");
    }
    this._config = config;
    this._ids = null;
    this._target = null;       // your value while tapping/dragging, before it's sent
    this._timer = null;
    this._sending = false;     // the service call is out, the integration hasn't queued it yet
    this._sentAt = 0;          // until the pending write shows up (or 5 s)
    this._mode = null;         // a mode tap, until the pending write shows it
    this._vacOpen = false;     // the vacation days row
    this._days = 7;
    if (this._root) this._render();
  }

  static getStubConfig(hass) {
    return { entity: Object.keys(hass.states).find((e) => e.startsWith("water_heater."))
      || "water_heater.garage" };
  }

  getCardSize() { return 8; }
  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }

  set hass(hass) {
    this._hass = hass;
    if (!this._root) this._build();
    if (!this._ids) this._ids = this._findSiblings();
    this._render();
  }

  _findSiblings() {
    const ids = {};
    const reg = this._hass.entities || {};
    const device = reg[this._config.entity]?.device_id;
    const mine = device
      ? Object.values(reg).filter((e) => e.device_id === device).map((e) => e.entity_id) : [];
    for (const [key, suffix] of Object.entries(SUFFIXES)) {
      ids[key] = mine.find((e) => e.startsWith("sensor.") && e.endsWith(suffix));
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
          <div class="name"></div>
          <button class="more" aria-label="More info"><ha-icon icon="mdi:dots-vertical"></ha-icon></button>
        </div>
        <div class="banner" hidden></div>
        <div class="dial">
          <div class="slider"></div>
          <div class="info">
            <div class="label"><span>Target</span><span class="spin" hidden></span></div>
            <div class="big"><span class="num"></span><span class="unit">°F</span></div>
            <div class="current"></div>
            <div class="buttons">
              <button class="round minus" aria-label="Lower target"><ha-icon icon="mdi:minus"></ha-icon></button>
              <button class="round plus" aria-label="Raise target"><ha-icon icon="mdi:plus"></ha-icon></button>
            </div>
          </div>
        </div>
        <div class="fail" hidden></div>
        <div class="stats">
          <div class="stat s-power"><ha-icon icon="mdi:flash"></ha-icon>
            <div><div class="v"></div><div class="k">Power</div></div></div>
          <div class="stat s-recover"><ha-icon icon="mdi:timer-sand"></ha-icon>
            <div><div class="v"></div><div class="k">To recover</div></div></div>
          <div class="stat s-avail"><ha-icon icon="mdi:water-percent"></ha-icon>
            <div><div class="v"></div><div class="k">Hot water</div></div></div>
        </div>
        <div class="modes">${MODES.map((m) => `
          <button class="mode" data-mode="${m.key}" aria-label="${m.label}" title="${m.label}">
            <ha-icon icon="${m.icon}"></ha-icon><span class="spin" hidden></span></button>`).join("")}
        </div>
        <div class="vac" hidden>
          <span class="vlabel">Vacation</span>
          <button class="round sm vminus" aria-label="Fewer days"><ha-icon icon="mdi:minus"></ha-icon></button>
          <span class="vdays"></span>
          <button class="round sm vplus" aria-label="More days"><ha-icon icon="mdi:plus"></ha-icon></button>
          <button class="go">Start</button>
          <button class="x" aria-label="Cancel"><ha-icon icon="mdi:close"></ha-icon></button>
        </div>
      </ha-card>`;
    const $ = (s) => this._root.querySelector(s);
    this.$ = $;
    $(".more").addEventListener("click", () => this._moreInfo(this._config.entity));
    $(".name").addEventListener("click", () => this._moreInfo(this._config.entity));
    $(".minus").addEventListener("click", () => this._step(-1));
    $(".plus").addEventListener("click", () => this._step(1));
    $(".s-power").addEventListener("click", () => this._moreInfo(this._ids.meter || this._ids.power));
    $(".s-recover").addEventListener("click", () => this._moreInfo(this._ids.recover));
    $(".s-avail").addEventListener("click", () => this._moreInfo(this._ids.avail));
    for (const b of this._root.querySelectorAll(".mode")) {
      b.addEventListener("click", () => this._setMode(b.dataset.mode));
    }
    $(".vminus").addEventListener("click", () => { this._days = Math.max(1, this._days - 1); this._render(); });
    $(".vplus").addEventListener("click", () => { this._days = Math.min(99, this._days + 1); this._render(); });
    $(".x").addEventListener("click", () => { this._vacOpen = false; this._render(); });
    $(".go").addEventListener("click", () => this._startVacation());
    this._loadSlider();
  }

  async _loadSlider() {
    // HA lazy-loads its dial; building a thermostat card once pulls it in.
    if (!customElements.get("ha-control-circular-slider")) {
      const helpers = await window.loadCardHelpers?.();
      helpers?.createCardElement({ type: "thermostat", entity: this._config.entity });
      await Promise.race([customElements.whenDefined("ha-control-circular-slider"),
        new Promise((r) => setTimeout(r, 5000))]);
    }
    if (!customElements.get("ha-control-circular-slider")) return;
    const sl = document.createElement("ha-control-circular-slider");
    sl.preventInteractionOnScroll = true;
    sl.addEventListener("value-changing", (ev) => {
      if (ev.detail?.value != null) { this._target = ev.detail.value; this._renderTarget(); }
    });
    sl.addEventListener("value-changed", (ev) => {
      if (ev.detail?.value != null) this._change(ev.detail.value);
    });
    this._slider = sl;
    this.$(".slider").appendChild(sl);
    this._render();
  }

  _moreInfo(entityId) {
    if (!entityId) return;
    this.dispatchEvent(new CustomEvent("hass-more-info",
      { detail: { entityId }, bubbles: true, composed: true }));
  }

  // What the dial should show: your unsent value, else the one on its way to the
  // tank, else what the tank reports. Without the middle step the number snaps
  // back to the old setpoint for the ~10 s until the tank confirms, and the next
  // tap counts from there.
  _shown() {
    const a = this._hass.states[this._config.entity]?.attributes || {};
    if (this._target != null) return this._target;
    if (this._sentAt && this._sentValue != null) return this._sentValue;
    const sp = this._write("setpoint");
    if (sp.busy && sp.w.value != null) return Number(sp.w.value);
    return a.temperature;
  }

  // --- actions --------------------------------------------------------------
  _step(dir) {
    const a = this._hass.states[this._config.entity]?.attributes;
    if (!a) return;
    const from = this._shown();
    this._change(Math.min(a.max_temp, Math.max(a.min_temp, from + dir * (a.target_temp_step || 1))));
  }

  _change(value) {
    this._target = value;
    clearTimeout(this._timer);
    this._timer = setTimeout(() => this._send(), DEBOUNCE_MS);
    this._render();
  }

  async _send() {
    this._timer = null;
    const a = this._hass.states[this._config.entity]?.attributes;
    const value = this._target;
    if (!a || value == null) return;
    const sp = this._write("setpoint");
    const onItsWay = this._sentAt ? this._sentValue : sp.busy ? Number(sp.w.value) : a.temperature;
    if (value === onItsWay) { this._target = null; this._render(); return; }
    this._sending = true;
    this._render();
    try {
      await this._hass.callService("water_heater", "set_temperature",
        { entity_id: this._config.entity, temperature: value });
    } finally {
      this._sending = false;
      this._sentAt = Date.now();
      this._sentValue = value;
      if (this._target === value) this._target = null;
      this._render();
    }
  }

  _setMode(key) {
    const a = this._hass.states[this._config.entity]?.attributes;
    if (!a) return;
    if (key === "vacation") {
      if (a.away_mode === "on") {       // back to the mode before vacation
        this._vacOpen = false;
        this._mode = { key: "off", at: Date.now() };
        this._hass.callService("water_heater", "set_away_mode",
          { entity_id: this._config.entity, away_mode: false });
      } else {
        this._vacOpen = !this._vacOpen;
      }
      this._render();
      return;
    }
    this._vacOpen = false;
    if (a.operation_mode === key && a.away_mode !== "on") { this._render(); return; }
    this._mode = { key, at: Date.now() };
    this._hass.callService("water_heater", "set_operation_mode",
      { entity_id: this._config.entity, operation_mode: key });
    this._render();
  }

  _startVacation() {
    this._vacOpen = false;
    this._mode = { key: "vacation", at: Date.now() };
    this._hass.callService("icomm_local", "set_mode",
      { mode: "vacation", days: this._days }, { entity_id: this._config.entity });
    this._render();
  }

  // --- what's on its way ---------------------------------------------------
  _write(kind) {
    const pa = this._s("pending")?.attributes || {};
    const p = pa.pending?.[kind];
    if (p) return { busy: true, w: { kind, ...p } };
    const last = pa.last_write;
    if (last && last.kind === kind && FAILED[last.outcome]
        && Date.now() / 1000 - (last.delivered || last.queued) < FAIL_SHOW_S) {
      return { failed: true, w: last };
    }
    return {};
  }

  // --- rendering ------------------------------------------------------------
  _renderTarget() {
    const a = this._hass.states[this._config.entity]?.attributes;
    if (!a) return;
    const t = this._shown();
    this.$(".num").textContent = t ?? "–";
    this.$(".minus").disabled = t <= a.min_temp;
    this.$(".plus").disabled = t >= a.max_temp;
    if (this._slider) this._slider.value = t;
  }

  _render() {
    if (!this._root || !this._hass || !this._config) return;
    const $ = this.$;
    const wh = this._hass.states[this._config.entity];
    if (!wh) { $(".name").textContent = `${this._config.entity} not found`; return; }
    const a = wh.attributes;
    $(".name").textContent = this._config.name || a.friendly_name || "Water heater";
    $(".name").title = `card ${VERSION}`;

    const stale = a.stale === true;
    const watts = num(this._s("meter")) ?? num(this._s("power")) ?? a.power_w;
    const opState = this._s("state")?.state;
    const element = this._s("element")?.state;
    const heating = !stale && (opState === "heating" || (watts ?? 0) > 50);
    const color = wh.state === "unavailable" ? "var(--disabled-color, #bdbdbd)"
      : "var(--state-water_heater-active-color, var(--orange-color, #ff9800))";
    this.style.setProperty("--wh-color", color);

    // The dial.
    if (this._slider) {
      Object.assign(this._slider, { min: a.min_temp, max: a.max_temp,
        step: a.target_temp_step || 1, current: a.current_temperature,
        disabled: wh.state === "unavailable" });
    }
    this._renderTarget();
    $(".current").textContent = a.current_temperature != null
      ? `Currently ${Math.round(a.current_temperature)} °F` : "";

    // The spinner: from the first tap until the tank shows it.
    const sp = this._write("setpoint");
    if (this._sentAt && ((sp.busy && Number(sp.w.value) === this._sentValue)
        || Date.now() - this._sentAt > 5000)) this._sentAt = 0;
    if (this._sentAt && !this._tick) {
      this._tick = setTimeout(() => { this._tick = null; this._render(); }, 1000);
    }
    const busy = this._target != null || this._sending || !!this._sentAt || sp.busy;
    $(".label .spin").hidden = !busy;
    $(".label").title = busy ? (sp.w ? `${sp.w.status || "queued"}` : "waiting to send") : "";
    const md = this._write("mode");
    const fail = sp.failed ? sp.w : md.failed ? md.w : null;
    $(".fail").hidden = !fail;
    if (fail) {
      const what = fail.kind === "setpoint" ? `${Math.round(fail.value)} °F` : MODE_LABEL[fail.value] || fail.value;
      $(".fail").innerHTML = `<ha-icon icon="mdi:alert-circle"></ha-icon><span>${what} ${FAILED[fail.outcome]}.
        ${stale ? "The data is frozen, so it may have applied anyway." : "The tank kept reporting the old value."}</span>`;
    }

    // Anything that makes the numbers less trustworthy.
    const health = this._s("health")?.state;
    const banner = stale ? "The heater stopped sending fresh data, so these values may be old. Opening the A.O. Smith app usually wakes it."
      : health && !["healthy", "unknown"].includes(health) ? `Relay: ${health}` : "";
    $(".banner").hidden = !banner;
    $(".banner").textContent = banner;

    // Stats.
    const pw = $(".s-power");
    pw.querySelector(".v").textContent = watts != null && !stale ? `${Math.round(watts)} W` : "–";
    pw.querySelector(".k").textContent = heating
      ? (element && element !== "off" ? "Element" : "Compressor") : "Power";
    pw.classList.toggle("hot", heating);
    const rec = num(this._s("recover"));
    $(".s-recover .v").textContent = rec != null ? fmtMin(rec) : "–";
    const avail = num(this._s("avail"));
    $(".s-avail .v").textContent = avail != null ? `${Math.round(avail)}%` : "–";

    // Modes.
    if (this._mode && (md.busy || Date.now() - this._mode.at > 5000)) this._mode = null;
    const want = md.busy ? (md.w.value === "hybrid" ? "eco" : md.w.value) : this._mode?.key;
    const away = a.away_mode === "on";
    for (const b of this._root.querySelectorAll(".mode")) {
      const k = b.dataset.mode;
      b.hidden = k === "vacation" ? a.away_mode == null : !(a.operation_list || []).includes(k);
      b.classList.toggle("on", !want && (k === "vacation" ? away : !away && a.operation_mode === k));
      b.classList.toggle("open", k === "vacation" && this._vacOpen);
      b.title = k === "vacation" && away
        ? `Vacation${a.vacation_days ? `, ${a.vacation_days} days` : ""}: tap to end` : MODES.find((m) => m.key === k).label;
      b.classList.toggle("want", want === k);
      b.querySelector(".spin").hidden = want !== k;
    }
    $(".vac").hidden = !this._vacOpen;
    $(".vdays").textContent = `${this._days} day${this._days === 1 ? "" : "s"}`;
    $(".vminus").disabled = this._days <= 1;
  }
}

const STYLE = `
  :host { display: block; --wh-color: var(--orange-color, #ff9800); }
  ha-card { padding: 12px 16px 16px; display: flex; flex-direction: column; gap: 12px; }
  button { font: inherit; color: inherit; cursor: pointer; background: none; border: none; padding: 0; }
  .head { display: flex; align-items: center; min-height: 40px; }
  .name { flex: 1; text-align: center; font-size: 16px; font-weight: 500; cursor: pointer;
    padding-left: 40px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .more { width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center;
    color: var(--secondary-text-color); }
  .banner { padding: 10px 12px; border-radius: 10px; font-size: 13px;
    background: rgba(255,193,7,.15); border-left: 4px solid var(--warning-color, #ffc107); }
  .banner[hidden], .fail[hidden], .spin[hidden], .mode[hidden] { display: none; }
  .dial { position: relative; width: 100%; max-width: 320px; margin: 0 auto; aspect-ratio: 1; }
  .slider, ha-control-circular-slider { width: 100%; height: 100%; display: block; }
  ha-control-circular-slider {
    --control-circular-slider-color: var(--wh-color);
    --control-circular-slider-background: var(--wh-color);
    --control-circular-slider-background-opacity: 0.3; }
  .info { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center;
    justify-content: center; pointer-events: none; }
  .label { display: flex; align-items: center; gap: 6px; font-size: 16px; font-weight: 500; height: 22px; }
  .big { font-size: 57px; line-height: 64px; letter-spacing: -0.25px; display: flex; align-items: flex-start; }
  .big .unit { font-size: 22px; line-height: 32px; margin-left: 2px; }
  .current { font-size: 16px; font-weight: 500; min-height: 22px; }
  .buttons { position: absolute; bottom: 4%; left: 0; right: 0; display: flex; justify-content: center;
    gap: 24px; pointer-events: auto; }
  .round { width: 48px; height: 48px; border-radius: 50%; display: grid; place-items: center;
    border: 1px solid var(--outline-color, var(--secondary-text-color)); }
  .round:active { background: var(--secondary-background-color); }
  .round:disabled { opacity: .38; cursor: default; }
  .spin { width: 14px; height: 14px; border-radius: 50%; box-sizing: border-box;
    border: 2px solid var(--wh-color); border-right-color: transparent; animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .fail { display: flex; gap: 8px; align-items: flex-start; font-size: 13px; padding: 8px 12px;
    border-radius: 10px; background: rgba(244,67,54,.10); }
  .fail ha-icon { color: var(--error-color, #f44336); --mdc-icon-size: 18px; flex-shrink: 0; }
  .stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
  .stat { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 12px;
    background: var(--secondary-background-color); cursor: pointer; min-width: 0; }
  .stat ha-icon { --mdc-icon-size: 20px; color: var(--secondary-text-color); flex-shrink: 0; }
  .stat.hot ha-icon { color: var(--wh-color); }
  .stat .v { font-size: 15px; font-weight: 500; white-space: nowrap; }
  .stat .k { font-size: 11px; color: var(--secondary-text-color); white-space: nowrap; }
  .modes { display: flex; gap: 0; background: var(--secondary-background-color); border-radius: 12px;
    padding: 0; height: 56px; overflow: hidden; }
  .mode { flex: 1; display: flex; align-items: center; justify-content: center; gap: 6px; border-radius: 12px;
    color: var(--primary-text-color); position: relative; }
  .mode.on { background: var(--wh-color); color: #fff; }
  .mode.want { outline: 2px dashed var(--wh-color); outline-offset: -4px; }
  .mode.open { outline: 2px solid var(--wh-color); outline-offset: -4px; }
  .vac { display: flex; align-items: center; gap: 8px; font-size: 14px; padding: 4px 4px 0; }
  .vac[hidden] { display: none; }
  .vac .vlabel { white-space: nowrap; }
  .vac .vdays { min-width: 56px; white-space: nowrap; text-align: center; font-weight: 500; }
  .round.sm { width: 32px; height: 32px; --mdc-icon-size: 18px; }
  .vac .go { margin-left: auto; padding: 8px 16px; border-radius: 18px; background: var(--wh-color);
    color: #fff; font-weight: 500; }
  .vac .x { width: 32px; height: 32px; display: grid; place-items: center; color: var(--secondary-text-color); }
  .mode .spin { position: absolute; top: 8px; right: 10px; width: 12px; height: 12px; }
  @media (max-width: 380px) { .stat { padding: 8px; gap: 6px; } .stat ha-icon { display: none; } }
`;

customElements.define("icomm-water-heater-card", IcommWaterHeaterCard);
console.info(`%c ICOMM-WATER-HEATER-CARD %c ${VERSION} `, "background:#ff9800;color:#fff", "background:#555;color:#fff");
window.customCards = window.customCards || [];
window.customCards.push({
  type: "icomm-water-heater-card",
  name: "iCOMM Water Heater",
  description: "HA's dial with a spinner until the tank confirms, plus power, time to recover and hot water for icomm_local.",
  preview: true,
});
