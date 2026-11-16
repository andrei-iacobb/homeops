/*
 * ring-events-card
 *
 * A Lovelace custom card for the Ring archive. The footage is the visual
 * anchor, with quiet utility controls and amber reserved for notable events.
 * Served by ring-recorder at /card.js. Vanilla ES2020, no dependencies.
 *
 * Example:
 *   type: custom:ring-events-card
 *   base_url: https://hass.iacob.uk/ring-events
 *   device: front-door
 *   height: 420
 */

const DEFAULT_BASE_URL = "https://hass.iacob.uk/ring-events";
const DEFAULT_REFRESH_SECONDS = 60;
const PAGE_LIMIT = 100;
const TIME_ZONE = "Europe/London";
const NEAR_EDGE_PX = 72;

const TEXT = "#f2f0ec";
const TEXT_SECONDARY = "rgba(242,240,236,0.64)";

const FALLBACK_DEVICES = [
  { slug: "front-door", name: "Front Door" },
  { slug: "hallway", name: "Hallway" },
];

function el(tag, className, attrs) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  }
  return node;
}

function slugify(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function titleFromSlug(value) {
  return String(value || "")
    .split("-")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function cleanBaseUrl(value) {
  const base = typeof value === "string" && value.trim() ? value.trim() : DEFAULT_BASE_URL;
  return base.replace(/\/+$/, "");
}

function parseDate(value) {
  if (typeof value !== "string" || !value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function londonParts(date) {
  if (!date) return null;
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return { year: values.year, month: values.month, day: values.day };
}

function londonDateKey(value) {
  const date = value instanceof Date ? value : parseDate(value);
  const parts = londonParts(date);
  return parts ? `${parts.year}-${parts.month}-${parts.day}` : "unknown";
}

function displayTime(value, language) {
  const date = parseDate(value);
  if (!date) return "Unknown time";
  return new Intl.DateTimeFormat(language || undefined, {
    timeZone: TIME_ZONE,
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function displayDateTime(value, language) {
  const date = parseDate(value);
  if (!date) return "Unknown time";
  return new Intl.DateTimeFormat(language || undefined, {
    timeZone: TIME_ZONE,
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function dayLabel(value, language) {
  const date = parseDate(value);
  if (!date) return "Undated";
  const key = londonDateKey(date);
  const todayParts = londonParts(new Date());
  const today = `${todayParts.year}-${todayParts.month}-${todayParts.day}`;
  const yesterday = new Date(Date.UTC(
    Number(todayParts.year),
    Number(todayParts.month) - 1,
    Number(todayParts.day) - 1,
  )).toISOString().slice(0, 10);
  if (key === today) return "Today";
  if (key === yesterday) return "Yesterday";
  const parts = new Intl.DateTimeFormat(language || undefined, {
    timeZone: TIME_ZONE,
    weekday: "short",
    day: "numeric",
    month: "short",
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.weekday} ${values.day} ${values.month}`;
}

function kindLabel(kind) {
  const labels = { ding: "Ding", motion: "Motion", on_demand: "On demand" };
  return labels[kind] || titleFromSlug(String(kind || "event").replace(/_/g, "-"));
}

function normaliseAnalysis(raw) {
  const analysis = raw && typeof raw.analysis === "object" && raw.analysis ? raw.analysis : raw || {};
  const tags = Array.isArray(analysis.activity_tags)
    ? analysis.activity_tags
    : Array.isArray(analysis.tags)
      ? analysis.tags
      : [];
  return {
    summary: typeof analysis.summary === "string" ? analysis.summary : "",
    detail: typeof analysis.detail === "string" ? analysis.detail : "",
    people: Number.isFinite(Number(analysis.people)) ? Number(analysis.people) : 0,
    vehicles: Number.isFinite(Number(analysis.vehicles)) ? Number(analysis.vehicles) : 0,
    animals: Array.isArray(analysis.animals) ? analysis.animals.map(String) : [],
    packages: analysis.packages === true,
    tags: tags.map(String).filter(Boolean),
    notable: analysis.notable === true || (raw && (raw.notable === true || raw.notable === 1)),
    confidence: Number.isFinite(Number(analysis.confidence)) ? Number(analysis.confidence) : null,
  };
}

function normaliseEvent(raw) {
  if (!raw || typeof raw !== "object" || raw.id === undefined || !raw.created_at) return null;
  const deviceObject = raw.device && typeof raw.device === "object" ? raw.device : null;
  const deviceValue = deviceObject ? deviceObject.slug || deviceObject.id : raw.device_slug || raw.device;
  const deviceName = raw.device_name || raw.device_label || (deviceObject && deviceObject.name) || titleFromSlug(deviceValue);
  const device = slugify(deviceValue || deviceName) || "camera";
  const id = String(raw.id);
  const analysis = normaliseAnalysis(raw);
  const clipUrl = typeof raw.clip_url === "string" && raw.clip_url ? raw.clip_url : null;
  return {
    id,
    device,
    deviceName: String(deviceName || titleFromSlug(device)),
    kind: String(raw.kind || "event"),
    createdAt: String(raw.created_at),
    clipUrl,
    analysis,
    status: String(raw.status || raw.analysis_status || "pending"),
    recordingStatus: String(raw.recording_status || (clipUrl ? "ready" : "pending")),
  };
}

function normaliseDevice(raw) {
  if (typeof raw === "string") return { slug: slugify(raw), name: titleFromSlug(slugify(raw)) };
  if (!raw || typeof raw !== "object") return null;
  const value = raw.slug || raw.device_slug || raw.device || raw.name || raw.id;
  const slug = slugify(value);
  if (!slug) return null;
  return { slug, name: String(raw.name || raw.label || raw.device_name || titleFromSlug(slug)) };
}

function nextDateKey(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return "";
  const next = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]) + 1));
  return next.toISOString().slice(0, 10);
}

const CARD_STYLE = `
  :host {
    display: block;
    color-scheme: dark;
  }
  * { box-sizing: border-box; }
  .card {
    overflow: hidden;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 16px;
    background: #151515;
    color: ${TEXT};
    font: inherit;
    outline: none;
  }
  .card:focus-visible {
    box-shadow: 0 0 0 2px #151515, 0 0 0 4px ${TEXT};
  }
  .card.notable {
    border-color: rgba(214,163,74,0.58);
  }
  .player-shell {
    position: relative;
    width: 100%;
    max-width: var(--ring-player-max-width, none);
    margin: 0 auto;
    aspect-ratio: 16 / 9;
    background: #090909;
  }
  .player {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: #090909;
  }
  .player-empty {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    padding: 24px;
    color: ${TEXT_SECONDARY};
    font-size: 0.9rem;
    text-align: center;
  }
  .event-copy {
    padding: 13px 16px 14px;
    border-top: 1px solid rgba(255,255,255,0.06);
  }
  .headline {
    display: flex;
    align-items: center;
    gap: 9px;
    min-width: 0;
  }
  .camera {
    overflow: hidden;
    min-width: 0;
    color: ${TEXT};
    font-size: 0.96rem;
    font-weight: 680;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .event-time {
    color: ${TEXT_SECONDARY};
    font-size: 0.84rem;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .kind {
    flex: 0 0 auto;
    margin-left: auto;
    padding: 3px 8px;
    border-radius: 999px;
    background: rgba(255,255,255,0.07);
    color: ${TEXT_SECONDARY};
    font-size: 0.68rem;
    font-weight: 720;
    letter-spacing: 0.035em;
    text-transform: uppercase;
  }
  .notable .kind {
    background: rgba(214,163,74,0.14);
    color: #edc779;
  }
  .summary {
    margin: 8px 0 0;
    color: ${TEXT};
    font-size: 0.9rem;
    line-height: 1.45;
  }
  .analysis-state {
    margin: 8px 0 0;
    color: ${TEXT_SECONDARY};
    font-size: 0.82rem;
  }
  details {
    margin-top: 7px;
    color: ${TEXT_SECONDARY};
    font-size: 0.84rem;
    line-height: 1.45;
  }
  details[hidden] { display: none; }
  details summary {
    width: max-content;
    color: ${TEXT_SECONDARY};
    cursor: pointer;
    user-select: none;
  }
  details p { max-width: 72ch; margin: 7px 0 0; }
  .toolbar {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 10px 12px;
    border-top: 1px solid rgba(255,255,255,0.06);
    border-bottom: 1px solid rgba(255,255,255,0.06);
    overflow-x: auto;
    scrollbar-width: thin;
  }
  .device-filters {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  button, input { font: inherit; }
  .filter, .notable-filter {
    flex: 0 0 auto;
    min-height: 34px;
    padding: 0 11px;
    border: 1px solid rgba(255,255,255,0.09);
    border-radius: 999px;
    background: #1d1d1d;
    color: ${TEXT_SECONDARY};
    cursor: pointer;
  }
  .filter[aria-pressed="true"] {
    border-color: rgba(242,240,236,0.32);
    background: #292929;
    color: ${TEXT};
  }
  .notable-filter[aria-pressed="true"] {
    border-color: rgba(214,163,74,0.52);
    background: rgba(214,163,74,0.13);
    color: #edc779;
  }
  .date-label {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 7px;
    margin-left: auto;
    color: ${TEXT_SECONDARY};
    font-size: 0.78rem;
    white-space: nowrap;
  }
  .date-jump {
    min-height: 34px;
    padding: 0 8px;
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 9px;
    background: #1d1d1d;
    color: ${TEXT};
    color-scheme: dark;
  }
  @media (hover: hover) {
    button:hover, .date-jump:hover {
      border-color: rgba(242,240,236,0.3);
      background: #292929;
    }
  }
  button:active { transform: scale(0.97); }
  button:focus-visible, input:focus-visible, summary:focus-visible {
    outline: 2px solid ${TEXT};
    outline-offset: 2px;
  }
  .load-state {
    min-height: 28px;
    padding: 8px 13px 0;
    color: ${TEXT_SECONDARY};
    font-size: 0.76rem;
  }
  .load-state.error { color: #ef9991; }
  .timeline {
    display: flex;
    align-items: stretch;
    gap: 8px;
    min-height: 132px;
    padding: 8px 12px 12px;
    overflow-x: auto;
    overscroll-behavior-x: contain;
    scroll-behavior: smooth;
    scroll-snap-type: x proximity;
    scrollbar-gutter: stable;
    -webkit-overflow-scrolling: touch;
  }
  .day-marker {
    display: flex;
    flex: 0 0 54px;
    align-items: flex-end;
    justify-content: center;
    padding: 0 4px 7px;
    border-right: 1px solid rgba(255,255,255,0.09);
    color: rgba(242,240,236,0.46);
    font-size: 0.68rem;
    line-height: 1.25;
    text-align: center;
  }
  .event-tile {
    position: relative;
    flex: 0 0 154px;
    width: 154px;
    min-height: 112px;
    padding: 0;
    overflow: hidden;
    border: 1px solid rgba(255,255,255,0.09);
    border-radius: 12px;
    background: #1c1c1c;
    color: ${TEXT};
    cursor: pointer;
    scroll-snap-align: start;
    text-align: left;
  }
  .event-tile[aria-current="true"] {
    border-color: rgba(242,240,236,0.62);
    box-shadow: inset 0 0 0 1px rgba(242,240,236,0.22);
  }
  .event-tile.notable-tile {
    border-color: rgba(214,163,74,0.46);
  }
  .tile-video {
    display: block;
    width: 100%;
    aspect-ratio: 16 / 9;
    border: 0;
    background: #0b0b0b;
    object-fit: cover;
    pointer-events: none;
  }
  .tile-copy { display: block; padding: 7px 8px 8px; }
  .tile-top {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 6px;
  }
  .tile-time {
    font-size: 0.78rem;
    font-weight: 680;
    font-variant-numeric: tabular-nums;
  }
  .tile-camera {
    overflow: hidden;
    color: ${TEXT_SECONDARY};
    font-size: 0.68rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .tags {
    display: flex;
    gap: 4px;
    min-height: 18px;
    margin-top: 5px;
    overflow: hidden;
  }
  .tag {
    overflow: hidden;
    max-width: 78px;
    padding: 2px 5px;
    border-radius: 999px;
    background: rgba(255,255,255,0.07);
    color: ${TEXT_SECONDARY};
    font-size: 0.58rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .empty-strip {
    display: grid;
    flex: 1 0 100%;
    min-height: 110px;
    place-items: center;
    color: ${TEXT_SECONDARY};
    font-size: 0.84rem;
    text-align: center;
  }
  @media (max-width: 560px) {
    .event-copy { padding: 11px 12px 12px; }
    .toolbar { align-items: flex-start; flex-wrap: wrap; }
    .date-label { width: 100%; margin-left: 0; }
    .date-jump { flex: 1 1 auto; }
    .event-tile { flex-basis: 136px; width: 136px; }
    .day-marker { flex-basis: 46px; }
  }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      scroll-behavior: auto !important;
      transition: none !important;
    }
  }
`;

class RingEventsCard extends HTMLElement {
  static getStubConfig() {
    return { base_url: DEFAULT_BASE_URL };
  }

  constructor() {
    super();
    this._config = null;
    this._hass = null;
    this._events = [];
    this._devices = FALLBACK_DEVICES.slice();
    this._selectedId = null;
    this._device = "all";
    this._notableOnly = false;
    this._loading = false;
    this._hasMore = true;
    this._loaded = false;
    this._refreshTimer = null;
    this._abort = null;
    this._fetchSeq = 0;
    this._observer = null;

    const root = this.attachShadow({ mode: "open" });
    const style = el("style");
    style.textContent = CARD_STYLE;

    this._card = el("section", "card", { tabindex: "0", "aria-label": "Ring event timeline" });
    const playerShell = el("div", "player-shell");
    this._player = el("video", "player", { controls: "", playsinline: "", preload: "metadata" });
    this._playerEmpty = el("div", "player-empty");
    this._playerEmpty.textContent = "Loading Ring events...";
    playerShell.append(this._player, this._playerEmpty);

    const headline = el("div", "headline");
    this._camera = el("span", "camera");
    this._eventTime = el("time", "event-time");
    this._kind = el("span", "kind");
    headline.append(this._camera, this._eventTime, this._kind);
    this._summary = el("p", "summary");
    this._analysisState = el("p", "analysis-state");
    this._details = el("details");
    const detailToggle = el("summary");
    detailToggle.textContent = "Show detail";
    this._detail = el("p");
    this._details.append(detailToggle, this._detail);
    const copy = el("div", "event-copy");
    copy.append(headline, this._summary, this._analysisState, this._details);

    this._deviceFilters = el("div", "device-filters", { role: "group", "aria-label": "Camera filter" });
    this._notableButton = el("button", "notable-filter", {
      type: "button",
      "aria-pressed": "false",
    });
    this._notableButton.textContent = "Notable only";
    const dateLabel = el("label", "date-label");
    const dateText = el("span");
    dateText.textContent = "Jump to date";
    this._dateInput = el("input", "date-jump", { type: "date", "aria-label": "Jump to date" });
    dateLabel.append(dateText, this._dateInput);
    const toolbar = el("nav", "toolbar", { "aria-label": "Ring event filters" });
    toolbar.append(this._deviceFilters, this._notableButton, dateLabel);

    this._loadState = el("div", "load-state", { "aria-live": "polite" });
    this._timeline = el("div", "timeline", {
      tabindex: "0",
      role: "region",
      "aria-label": "Ring events, oldest to newest",
    });
    this._card.append(playerShell, copy, toolbar, this._loadState, this._timeline);
    root.append(style, this._card);

    this._onVisibility = this._onVisibility.bind(this);
    this._onKeyDown = this._onKeyDown.bind(this);
    this._onScroll = this._onScroll.bind(this);
    this._onWheel = this._onWheel.bind(this);
    this._notableButton.addEventListener("click", () => this._setNotable(!this._notableOnly));
    this._dateInput.addEventListener("change", () => this._jumpToDate(this._dateInput.value));
    this._card.addEventListener("keydown", this._onKeyDown);
    this._timeline.addEventListener("scroll", this._onScroll, { passive: true });
    this._timeline.addEventListener("wheel", this._onWheel, { passive: false });
    this._renderAll();
  }

  setConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("ring-events-card: config must be an object");
    }
    const refresh = Number(config.refresh);
    const height = Number(config.height);
    if (this._abort) this._abort.abort();
    this._fetchSeq += 1;
    this._loading = false;
    this._config = {
      baseUrl: cleanBaseUrl(config.base_url),
      device: typeof config.device === "string" && config.device.trim() ? slugify(config.device) : "all",
      refresh: Number.isFinite(refresh) && refresh >= 10 ? refresh : DEFAULT_REFRESH_SECONDS,
      height: Number.isFinite(height) && height >= 180 ? Math.min(height, 900) : null,
    };
    this._device = this._config.device;
    this._events = [];
    this._selectedId = null;
    this._loaded = false;
    this._hasMore = true;
    if (this._config.height) this.style.setProperty("--ring-player-max-width", `${this._config.height * 16 / 9}px`);
    else this.style.removeProperty("--ring-player-max-width");
    this._renderAll();
    if (this.isConnected) {
      this._startTimer();
      this._loadDevices();
      this._loadInitial();
    }
  }

  set hass(hass) {
    this._hass = hass;
  }

  get hass() {
    return this._hass;
  }

  getCardSize() {
    return 8;
  }

  connectedCallback() {
    document.addEventListener("visibilitychange", this._onVisibility);
    if (this._config) {
      this._startTimer();
      this._loadDevices();
      this._loadInitial();
    }
  }

  disconnectedCallback() {
    document.removeEventListener("visibilitychange", this._onVisibility);
    this._stopTimer();
    if (this._abort) this._abort.abort();
    if (this._observer) this._observer.disconnect();
  }

  _language() {
    const hass = this._hass;
    return (hass && ((hass.locale && hass.locale.language) || hass.language)) || undefined;
  }

  _startTimer() {
    this._stopTimer();
    this._refreshTimer = window.setInterval(() => this._refresh(), this._config.refresh * 1000);
  }

  _stopTimer() {
    if (this._refreshTimer !== null) window.clearInterval(this._refreshTimer);
    this._refreshTimer = null;
  }

  _onVisibility() {
    if (document.visibilityState === "visible") this._refresh();
  }

  _url(path, params) {
    const url = new URL(`${this._config.baseUrl}${path}`);
    for (const [key, value] of Object.entries(params || {})) {
      if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, String(value));
    }
    return url.toString();
  }

  _filterParams(extra) {
    const params = { limit: PAGE_LIMIT, ...(extra || {}) };
    if (this._device !== "all") params.device = this._device;
    if (this._notableOnly) params.notable = 1;
    return params;
  }

  async _fetchJson(path, params, signal) {
    const response = await fetch(this._url(path, params), {
      mode: "cors",
      cache: "no-store",
      credentials: "omit",
      signal,
    });
    if (!response.ok) throw new Error(`${path} returned ${response.status}`);
    return response.json();
  }

  async _fetchEvents(extra, signal) {
    const payload = await this._fetchJson("/api/events", this._filterParams(extra), signal);
    const rows = Array.isArray(payload) ? payload : payload && Array.isArray(payload.events) ? payload.events : [];
    return rows.map(normaliseEvent).filter(Boolean);
  }

  async _loadDevices() {
    try {
      const payload = await this._fetchJson("/api/devices");
      const rows = Array.isArray(payload) ? payload : payload && Array.isArray(payload.devices) ? payload.devices : [];
      const found = rows.map(normaliseDevice).filter(Boolean);
      const merged = new Map(FALLBACK_DEVICES.map((device) => [device.slug, device]));
      for (const device of found) merged.set(device.slug, device);
      this._devices = Array.from(merged.values());
      this._renderControls();
    } catch (_error) {
      this._devices = FALLBACK_DEVICES.slice();
      this._renderControls();
    }
  }

  async _loadInitial() {
    if (!this._config || this._loading) return;
    this._loading = true;
    this._loaded = false;
    this._setStatus("Loading events...");
    if (this._abort) this._abort.abort();
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    this._abort = controller;
    const seq = ++this._fetchSeq;
    try {
      const events = await this._fetchEvents({}, controller ? controller.signal : undefined);
      if (seq !== this._fetchSeq || !this.isConnected) return;
      this._events = this._sortAndDedupe(events);
      this._hasMore = events.length >= PAGE_LIMIT;
      this._selectedId = this._events.length ? this._events[this._events.length - 1].id : null;
      this._loaded = true;
      this._renderAll();
      this._setStatus(this._events.length ? `${this._events.length} events loaded` : "No events match these filters yet.");
      window.requestAnimationFrame(() => this._scrollToEnd(false));
    } catch (error) {
      if (error && error.name === "AbortError") return;
      this._events = [];
      this._selectedId = null;
      this._loaded = false;
      this._renderAll();
      this._setStatus("The Ring archive is unavailable. It will retry automatically.", true);
    } finally {
      if (seq === this._fetchSeq) this._loading = false;
      if (this._abort === controller) this._abort = null;
    }
  }

  async _loadOlder() {
    if (this._loading || !this._hasMore || !this._events.length) return;
    this._loading = true;
    const seq = this._fetchSeq;
    const beforeWidth = this._timeline.scrollWidth;
    const beforeLeft = this._timeline.scrollLeft;
    this._setStatus("Loading older events...");
    try {
      const events = await this._fetchEvents({ until: this._events[0].createdAt });
      if (seq !== this._fetchSeq) return;
      this._hasMore = events.length >= PAGE_LIMIT;
      this._events = this._sortAndDedupe([...events, ...this._events]);
      this._renderTimeline();
      this._renderMain();
      window.requestAnimationFrame(() => {
        this._timeline.scrollLeft = beforeLeft + (this._timeline.scrollWidth - beforeWidth);
      });
      this._setStatus(events.length ? `${events.length} older events loaded` : "Start of retained archive");
    } catch (_error) {
      if (seq !== this._fetchSeq) return;
      this._setStatus("Older events could not be loaded. Scroll left to retry.", true);
    } finally {
      if (seq === this._fetchSeq) this._loading = false;
    }
  }

  async _refresh() {
    if (!this._config || this._loading || document.visibilityState === "hidden") return;
    if (!this._events.length) {
      this._loadInitial();
      return;
    }
    const atRight = this._atRightEdge();
    const knownIds = new Set(this._events.map((event) => event.id));
    const seq = this._fetchSeq;
    this._loading = true;
    try {
      const events = await this._fetchEvents({ since: this._events[this._events.length - 1].createdAt });
      if (seq !== this._fetchSeq) return;
      const newCount = events.filter((event) => !knownIds.has(event.id)).length;
      const before = JSON.stringify(this._events);
      const merged = this._sortAndDedupe([...this._events, ...events]);
      const changed = before !== JSON.stringify(merged);
      this._events = merged;
      if (newCount && atRight) this._selectedId = this._events[this._events.length - 1].id;
      if (changed) this._renderAll();
      if (newCount) this._setStatus(`${newCount} new ${newCount === 1 ? "event" : "events"}`);
      if (atRight) window.requestAnimationFrame(() => this._scrollToEnd(true));
    } catch (_error) {
      if (seq !== this._fetchSeq) return;
      this._setStatus("Refresh failed. The current timeline is still available.", true);
    } finally {
      if (seq === this._fetchSeq) this._loading = false;
    }
  }

  async _jumpToDate(value) {
    if (!value) return;
    const loaded = this._events.find((event) => londonDateKey(event.createdAt) === value);
    if (loaded) {
      this._scrollToEvent(loaded.id);
      return;
    }
    const next = nextDateKey(value);
    if (!next) return;
    this._setStatus(`Loading ${value}...`);
    const seq = this._fetchSeq;
    this._loading = true;
    try {
      const events = await this._fetchEvents({
        since: `${value}T00:00:00`,
        until: `${next}T00:00:00`,
        limit: 500,
      });
      if (seq !== this._fetchSeq) return;
      this._events = this._sortAndDedupe([...events, ...this._events]);
      this._renderAll();
      const target = this._events.find((event) => londonDateKey(event.createdAt) === value);
      if (target) {
        this._setStatus(`${events.length} events loaded for ${value}`);
        window.requestAnimationFrame(() => this._scrollToEvent(target.id));
      } else {
        this._setStatus(`No events match ${value}.`);
      }
    } catch (_error) {
      if (seq !== this._fetchSeq) return;
      this._setStatus(`Events for ${value} could not be loaded.`, true);
    } finally {
      if (seq === this._fetchSeq) this._loading = false;
    }
  }

  _sortAndDedupe(events) {
    const byId = new Map();
    for (const event of events) byId.set(event.id, event);
    return Array.from(byId.values()).sort((left, right) => {
      const leftTime = parseDate(left.createdAt);
      const rightTime = parseDate(right.createdAt);
      return (leftTime ? leftTime.getTime() : 0) - (rightTime ? rightTime.getTime() : 0);
    });
  }

  _setDevice(slug) {
    if (slug === this._device) return;
    if (this._abort) this._abort.abort();
    this._fetchSeq += 1;
    this._loading = false;
    this._device = slug;
    this._dateInput.value = "";
    this._events = [];
    this._selectedId = null;
    this._hasMore = true;
    this._renderAll();
    this._loadInitial();
  }

  _setNotable(value) {
    if (Boolean(value) === this._notableOnly) return;
    if (this._abort) this._abort.abort();
    this._fetchSeq += 1;
    this._loading = false;
    this._notableOnly = Boolean(value);
    this._dateInput.value = "";
    this._events = [];
    this._selectedId = null;
    this._hasMore = true;
    this._renderAll();
    this._loadInitial();
  }

  _select(id, options) {
    if (!this._events.some((event) => event.id === id)) return;
    this._selectedId = id;
    this._renderMain();
    for (const tile of this._timeline.querySelectorAll(".event-tile")) {
      tile.setAttribute("aria-current", String(tile.dataset.eventId === id));
    }
    if (!options || options.scroll !== false) this._scrollToEvent(id);
    if (options && options.play) {
      const playResult = this._player.play();
      if (playResult && typeof playResult.catch === "function") playResult.catch(() => {});
    }
  }

  _moveSelection(offset) {
    if (!this._events.length) return;
    const current = this._events.findIndex((event) => event.id === this._selectedId);
    const next = Math.max(0, Math.min(this._events.length - 1, (current < 0 ? this._events.length - 1 : current) + offset));
    this._select(this._events[next].id);
    const button = Array.from(this._timeline.querySelectorAll(".event-tile"))
      .find((tile) => tile.dataset.eventId === this._events[next].id);
    if (button) button.focus({ preventScroll: true });
  }

  _selectedEvent() {
    return this._events.find((event) => event.id === this._selectedId) || null;
  }

  _renderAll() {
    this._renderControls();
    this._renderMain();
    this._renderTimeline();
  }

  _renderControls() {
    this._deviceFilters.replaceChildren();
    const options = [{ slug: "all", name: "All" }, ...this._devices];
    for (const device of options) {
      const button = el("button", "filter", {
        type: "button",
        "aria-pressed": String(device.slug === this._device),
      });
      button.textContent = device.name;
      button.addEventListener("click", () => this._setDevice(device.slug));
      this._deviceFilters.append(button);
    }
    this._notableButton.setAttribute("aria-pressed", String(this._notableOnly));
  }

  _renderMain() {
    const event = this._selectedEvent();
    this._card.classList.toggle("notable", Boolean(event && event.analysis.notable));
    if (!event) {
      this._player.pause();
      this._player.removeAttribute("src");
      this._player.load();
      this._player.hidden = true;
      this._playerEmpty.hidden = false;
      this._playerEmpty.textContent = this._loaded ? "No events match these filters." : "Loading Ring events...";
      this._camera.textContent = "Ring events";
      this._eventTime.textContent = "";
      this._eventTime.removeAttribute("datetime");
      this._kind.textContent = "";
      this._kind.hidden = true;
      this._summary.textContent = "";
      this._summary.hidden = true;
      this._analysisState.textContent = "";
      this._analysisState.hidden = true;
      this._details.hidden = true;
      return;
    }
    if (event.clipUrl) {
      this._player.hidden = false;
      this._playerEmpty.hidden = true;
      if (this._player.getAttribute("src") !== event.clipUrl) {
        this._player.src = event.clipUrl;
        this._player.load();
      }
    } else {
      this._player.pause();
      this._player.removeAttribute("src");
      this._player.load();
      this._player.hidden = true;
      this._playerEmpty.hidden = false;
      this._playerEmpty.textContent = event.recordingStatus === "expired"
        ? "No recording became available for this event."
        : "Ring is still preparing this recording.";
    }
    this._player.setAttribute("aria-label", `${event.deviceName}, ${kindLabel(event.kind)}, ${displayDateTime(event.createdAt, this._language())}`);
    this._camera.textContent = event.deviceName;
    this._eventTime.textContent = displayDateTime(event.createdAt, this._language());
    this._eventTime.dateTime = event.createdAt;
    this._kind.textContent = kindLabel(event.kind);
    this._kind.hidden = false;
    this._summary.textContent = event.analysis.summary;
    this._summary.hidden = !event.analysis.summary;
    const stateCopy = {
      pending: "Analysis queued",
      processing: "Analysis in progress",
      failed: "Analysis failed",
      disabled: "Analysis disabled",
    }[event.status] || "";
    this._analysisState.textContent = stateCopy;
    this._analysisState.hidden = Boolean(event.analysis.summary) || !stateCopy;
    this._detail.textContent = event.analysis.detail;
    this._details.hidden = !event.analysis.detail;
  }

  _renderTimeline() {
    if (this._observer) this._observer.disconnect();
    this._timeline.replaceChildren();
    if (!this._events.length) {
      const empty = el("div", "empty-strip");
      empty.textContent = this._loaded ? "No events match these filters." : "Loading timeline...";
      this._timeline.append(empty);
      return;
    }
    let currentDay = null;
    const lazyVideos = [];
    for (const event of this._events) {
      const dateKey = londonDateKey(event.createdAt);
      if (dateKey !== currentDay) {
        currentDay = dateKey;
        const marker = el("div", "day-marker", { role: "presentation" });
        marker.textContent = dayLabel(event.createdAt, this._language());
        this._timeline.append(marker);
      }
      const selected = event.id === this._selectedId;
      const tile = el("button", `event-tile${event.analysis.notable ? " notable-tile" : ""}`, {
        type: "button",
        "aria-current": String(selected),
        "aria-label": `${event.deviceName}, ${kindLabel(event.kind)}, ${displayDateTime(event.createdAt, this._language())}`,
      });
      tile.dataset.eventId = event.id;
      tile.dataset.date = dateKey;
      const video = event.clipUrl
        ? el("video", "tile-video", { muted: "", playsinline: "", preload: "none", tabindex: "-1" })
        : el("span", "tile-video", { "aria-hidden": "true" });
      if (event.clipUrl) video.dataset.src = `${event.clipUrl}#t=0.5`;
      const tileCopy = el("span", "tile-copy");
      const top = el("span", "tile-top");
      const time = el("span", "tile-time");
      time.textContent = displayTime(event.createdAt, this._language());
      const camera = el("span", "tile-camera");
      camera.textContent = event.deviceName;
      top.append(time, camera);
      const tags = el("span", "tags");
      const tagValues = event.analysis.tags.length ? event.analysis.tags.slice(0, 3) : [kindLabel(event.kind)];
      for (const value of tagValues) {
        const tag = el("span", "tag");
        tag.textContent = value;
        tags.append(tag);
      }
      tileCopy.append(top, tags);
      tile.append(video, tileCopy);
      tile.addEventListener("click", () => this._select(event.id, { play: true, scroll: false }));
      this._timeline.append(tile);
      if (event.clipUrl) lazyVideos.push(video);
    }
    this._observeVideos(lazyVideos);
  }

  _observeVideos(videos) {
    const load = (video) => {
      if (!video.dataset.src || video.src) return;
      video.preload = "metadata";
      video.src = video.dataset.src;
      video.load();
    };
    if (typeof IntersectionObserver !== "function") {
      videos.forEach(load);
      return;
    }
    this._observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        load(entry.target);
        this._observer.unobserve(entry.target);
      }
    }, { root: this._timeline, rootMargin: "160px" });
    videos.forEach((video) => this._observer.observe(video));
  }

  _setStatus(message, isError) {
    this._loadState.textContent = message;
    this._loadState.classList.toggle("error", Boolean(isError));
  }

  _atRightEdge() {
    return this._timeline.scrollWidth - this._timeline.clientWidth - this._timeline.scrollLeft <= NEAR_EDGE_PX;
  }

  _scrollToEnd(smooth) {
    this._timeline.scrollTo({
      left: this._timeline.scrollWidth,
      behavior: smooth && !window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "smooth" : "auto",
    });
  }

  _scrollToEvent(id) {
    const tile = Array.from(this._timeline.querySelectorAll(".event-tile"))
      .find((candidate) => candidate.dataset.eventId === id);
    if (!tile) return;
    tile.scrollIntoView({
      block: "nearest",
      inline: "center",
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
    });
  }

  _onScroll() {
    if (this._timeline.scrollLeft <= NEAR_EDGE_PX) this._loadOlder();
  }

  _onWheel(event) {
    if (Math.abs(event.deltaY) <= Math.abs(event.deltaX) || this._timeline.scrollWidth <= this._timeline.clientWidth) return;
    event.preventDefault();
    this._timeline.scrollLeft += event.deltaY;
  }

  _onKeyDown(event) {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
    const tile = event.target && event.target.closest
      ? event.target.closest(".event-tile")
      : null;
    if (event.target !== this._card && event.target !== this._timeline && !tile) return;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      this._moveSelection(-1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      this._moveSelection(1);
    }
  }
}

if (!customElements.get("ring-events-card")) {
  customElements.define("ring-events-card", RingEventsCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "ring-events-card")) {
  window.customCards.push({
    type: "ring-events-card",
    name: "Ring Events Card",
    description: "Ring recordings with a camera timeline and Gemini summaries.",
    preview: false,
    documentationURL: `${DEFAULT_BASE_URL}/`,
  });
}
