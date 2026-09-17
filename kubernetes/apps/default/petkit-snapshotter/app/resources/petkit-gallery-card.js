/*
 * petkit-gallery-card
 *
 * A Lovelace custom card that shows the newest PetKit feeder snapshot for one
 * device and opens a fullscreen, swipeable viewer over the archived history.
 * Served by petkit-snapshotter at /card.js. Vanilla ES2020, no dependencies.
 *
 * Example:
 *   type: custom:petkit-gallery-card
 *   device: tommy
 *   title: Tommy
 */

const DEFAULT_MANIFEST = "https://hass.iacob.uk/petkit-snapshots/manifest.json";
const DEFAULT_BASE = "https://hass.iacob.uk/petkit-snapshots/snapshots/";
const DEFAULT_EVENTS = ["eat", "visit", "feed"];
const DEFAULT_REFRESH = 60;
const MIN_REFRESH = 10;
const CLOCK_TICK_MS = 30 * 1000;
const SWIPE_PX = 40;
const TAP_PX = 10;

const TEXT = "#f2f0ec";
const TEXT_SECONDARY = "rgba(242,240,236,0.64)";

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function parseDate(value) {
  if (typeof value !== "string" || !value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function relativeTime(date, now) {
  if (!date) return "";
  const seconds = Math.round(((now || Date.now()) - date.getTime()) / 1000);
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days} d ago`;
  return absoluteDate(date);
}

function absoluteDate(date, language) {
  if (!date) return "";
  try {
    return date.toLocaleDateString(language || undefined, { day: "numeric", month: "short" });
  } catch (_error) {
    return date.toDateString();
  }
}

function absoluteTime(date, language) {
  if (!date) return "";
  try {
    return date.toLocaleString(language || undefined, {
      weekday: "short",
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch (_error) {
    return date.toString();
  }
}

function imageUrl(base, filename) {
  const path = String(filename)
    .split("/")
    .filter((segment) => segment.length > 0)
    .map(encodeURIComponent)
    .join("/");
  return base + path;
}

function preload(url) {
  if (!url) return;
  const image = new Image();
  image.decoding = "async";
  image.src = url;
}

function el(tag, className, attrs) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (attrs) {
    for (const key of Object.keys(attrs)) node.setAttribute(key, attrs[key]);
  }
  return node;
}

// ---------------------------------------------------------------------------
// fullscreen viewer (appended to document.body so ancestor transforms in the
// Lovelace layout cannot break position: fixed)
// ---------------------------------------------------------------------------

const VIEWER_STYLE = `
  :host {
    position: fixed;
    inset: 0;
    z-index: 9999;
    display: block;
    background: #000;
    color: ${TEXT};
    touch-action: none;
    user-select: none;
    -webkit-user-select: none;
    -webkit-tap-highlight-color: transparent;
    overscroll-behavior: contain;
  }
  .backdrop {
    position: absolute;
    inset: 0;
  }
  .photo {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
    -webkit-user-drag: none;
  }
  .topbar {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: calc(12px + env(safe-area-inset-top, 0px)) calc(12px + env(safe-area-inset-right, 0px)) 24px calc(16px + env(safe-area-inset-left, 0px));
    background: linear-gradient(rgba(0,0,0,0.72), rgba(0,0,0,0));
    pointer-events: none;
  }
  .text {
    flex: 1 1 auto;
    min-width: 0;
  }
  .cap {
    font-weight: 600;
    font-size: 1rem;
    line-height: 1.3;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .sub {
    color: ${TEXT_SECONDARY};
    font-size: 0.85rem;
    line-height: 1.3;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .counter {
    color: ${TEXT_SECONDARY};
    font-size: 0.85rem;
    line-height: 1.3;
    padding-top: 2px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  button {
    appearance: none;
    -webkit-appearance: none;
    border: 0;
    margin: 0;
    font: inherit;
    color: ${TEXT};
    background: rgba(255,255,255,0.08);
    border-radius: 999px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    pointer-events: auto;
    touch-action: manipulation;
  }
  button:focus-visible {
    outline: 2px solid ${TEXT};
    outline-offset: 2px;
  }
  button:disabled {
    opacity: 0.25;
    cursor: default;
  }
  .close {
    width: 40px;
    height: 40px;
    font-size: 26px;
    line-height: 1;
    flex: 0 0 auto;
  }
  .nav {
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    width: 44px;
    height: 44px;
    font-size: 30px;
    line-height: 1;
    padding-bottom: 4px;
  }
  .nav.newer { left: calc(10px + env(safe-area-inset-left, 0px)); }
  .nav.older { right: calc(10px + env(safe-area-inset-right, 0px)); }
  @media (hover: none) and (pointer: coarse) {
    .nav { opacity: 0.55; }
  }
`;

class PetkitGalleryViewer extends HTMLElement {
  constructor() {
    super();
    this._items = [];
    this._index = 0;
    this._base = DEFAULT_BASE;
    this._title = "";
    this._language = undefined;
    this._onClose = null;
    this._pointer = null;

    const root = this.attachShadow({ mode: "open" });
    const style = el("style");
    style.textContent = VIEWER_STYLE;

    this._backdrop = el("div", "backdrop", { role: "dialog", "aria-modal": "true" });
    this._photo = el("img", "photo", { alt: "", draggable: "false" });
    this._cap = el("div", "cap");
    this._sub = el("div", "sub");
    this._counter = el("div", "counter");
    this._closeButton = el("button", "close", { type: "button", "aria-label": "Close", title: "Close" });
    this._closeButton.textContent = "×";
    this._newerButton = el("button", "nav newer", { type: "button", "aria-label": "Newer", title: "Newer" });
    this._newerButton.textContent = "‹";
    this._olderButton = el("button", "nav older", { type: "button", "aria-label": "Older", title: "Older" });
    this._olderButton.textContent = "›";

    const text = el("div", "text");
    text.append(this._cap, this._sub);
    const topbar = el("div", "topbar");
    topbar.append(text, this._counter, this._closeButton);
    this._backdrop.append(this._photo, topbar, this._newerButton, this._olderButton);
    root.append(style, this._backdrop);

    this._onKeyDown = this._onKeyDown.bind(this);
    this._onPointerDown = this._onPointerDown.bind(this);
    this._onPointerUp = this._onPointerUp.bind(this);
    this._onPointerCancel = this._onPointerCancel.bind(this);

    this._closeButton.addEventListener("click", () => this.close());
    this._newerButton.addEventListener("click", () => this.newer());
    this._olderButton.addEventListener("click", () => this.older());
    this._backdrop.addEventListener("pointerdown", this._onPointerDown);
    this._backdrop.addEventListener("pointerup", this._onPointerUp);
    this._backdrop.addEventListener("pointercancel", this._onPointerCancel);
    this._backdrop.addEventListener("contextmenu", (event) => event.preventDefault());
  }

  connectedCallback() {
    window.addEventListener("keydown", this._onKeyDown, true);
    this._closeButton.focus({ preventScroll: true });
  }

  disconnectedCallback() {
    window.removeEventListener("keydown", this._onKeyDown, true);
    this._pointer = null;
  }

  get isOpen() {
    return this.isConnected;
  }

  open(options) {
    this._items = Array.isArray(options.items) ? options.items : [];
    this._base = options.base || DEFAULT_BASE;
    this._title = options.title || "";
    this._language = options.language;
    this._onClose = typeof options.onClose === "function" ? options.onClose : null;
    this._index = Math.min(Math.max(options.index || 0, 0), Math.max(this._items.length - 1, 0));
    if (!this.isConnected) document.body.appendChild(this);
    this._render();
  }

  close() {
    if (!this.isConnected) return;
    this.remove();
    const callback = this._onClose;
    this._onClose = null;
    if (callback) callback();
  }

  // Newest first: "older" moves down the list, "newer" moves up.
  older() {
    this.goTo(this._index + 1);
  }

  newer() {
    this.goTo(this._index - 1);
  }

  goTo(index) {
    if (index < 0 || index >= this._items.length || index === this._index) return;
    this._index = index;
    this._render();
  }

  // Called by the card when the manifest refreshes while the viewer is open.
  update(items) {
    const current = this._items[this._index];
    this._items = Array.isArray(items) ? items : [];
    let index = current ? this._items.findIndex((item) => item.id === current.id) : -1;
    if (index < 0) index = Math.min(this._index, Math.max(this._items.length - 1, 0));
    this._index = index;
    this._render();
  }

  _url(item) {
    return item && item.filename ? imageUrl(this._base, item.filename) : "";
  }

  _render() {
    const total = this._items.length;
    const item = this._items[this._index];
    if (!item) {
      this._photo.removeAttribute("src");
      this._cap.textContent = this._title || "";
      this._sub.textContent = "No snapshots";
      this._counter.textContent = "";
      this._newerButton.disabled = true;
      this._olderButton.disabled = true;
      return;
    }
    const url = this._url(item);
    if (this._photo.getAttribute("src") !== url) this._photo.src = url;
    const eventAt = parseDate(item.event_at);
    this._cap.textContent = [this._title || item.device_label || item.device || "", item.event_label || item.event || ""]
      .filter(Boolean)
      .join(" · ");
    this._sub.textContent = [absoluteTime(eventAt, this._language), relativeTime(eventAt)].filter(Boolean).join(" · ");
    this._counter.textContent = `${this._index + 1} / ${total}`;
    this._newerButton.disabled = this._index <= 0;
    this._olderButton.disabled = this._index >= total - 1;
    preload(this._url(this._items[this._index + 1]));
    preload(this._url(this._items[this._index - 1]));
  }

  _onKeyDown(event) {
    if (event.defaultPrevented) return;
    switch (event.key) {
      case "Escape":
        event.preventDefault();
        event.stopPropagation();
        this.close();
        break;
      case "ArrowLeft":
        event.preventDefault();
        this.newer();
        break;
      case "ArrowRight":
        event.preventDefault();
        this.older();
        break;
      default:
        break;
    }
  }

  _onPointerDown(event) {
    if (event.button !== 0 && event.pointerType === "mouse") return;
    this._pointer = { id: event.pointerId, x: event.clientX, y: event.clientY, target: event.target };
    try {
      this._backdrop.setPointerCapture(event.pointerId);
    } catch (_error) {
      // Pointer capture is best effort; swipe still works without it.
    }
  }

  _onPointerUp(event) {
    const start = this._pointer;
    this._pointer = null;
    if (!start || start.id !== event.pointerId) return;
    const dx = event.clientX - start.x;
    const dy = event.clientY - start.y;
    if (Math.abs(dx) > SWIPE_PX && Math.abs(dx) > Math.abs(dy)) {
      // Swipe left drags the newest-first strip leftwards, revealing older items.
      if (dx < 0) this.older();
      else this.newer();
      return;
    }
    if (Math.abs(dx) <= TAP_PX && Math.abs(dy) <= TAP_PX && start.target === this._backdrop) {
      this.close();
    }
  }

  _onPointerCancel() {
    this._pointer = null;
  }
}

// ---------------------------------------------------------------------------
// the card
// ---------------------------------------------------------------------------

const CARD_STYLE = `
  :host {
    display: block;
  }
  .card {
    background: #151515;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 16px;
    box-shadow: none;
    overflow: hidden;
    color: ${TEXT};
    font: inherit;
  }
  .media {
    position: relative;
    aspect-ratio: 4 / 3;
    background: #0d0d0d;
    cursor: pointer;
    outline: none;
    -webkit-tap-highlight-color: transparent;
  }
  .media:focus-visible {
    box-shadow: inset 0 0 0 2px ${TEXT};
  }
  .media img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .media .empty {
    position: absolute;
    inset: 0;
    display: none;
    align-items: center;
    justify-content: center;
    color: ${TEXT_SECONDARY};
    font-size: 0.9em;
  }
  .card.empty .media {
    cursor: default;
  }
  .card.empty .media img {
    display: none;
  }
  .card.empty .media .empty {
    display: flex;
  }
  .caption {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 14px 12px;
    line-height: 1.3;
  }
  .title {
    font-weight: 600;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .meta {
    color: ${TEXT_SECONDARY};
    font-size: 0.9em;
    white-space: nowrap;
    flex: 0 0 auto;
  }
  .meta .dot {
    margin: 0 6px;
  }
  .card.empty .meta {
    display: none;
  }
`;

class PetkitGalleryCard extends HTMLElement {
  static getStubConfig() {
    return { device: "tommy", events: DEFAULT_EVENTS.slice() };
  }

  constructor() {
    super();
    this._config = null;
    this._hass = null;
    this._items = [];
    this._loaded = false;
    this._refreshTimer = null;
    this._clockTimer = null;
    this._fetchSeq = 0;
    this._abort = null;
    this._viewer = null;

    const root = this.attachShadow({ mode: "open" });
    const style = el("style");
    style.textContent = CARD_STYLE;

    this._card = el("div", "card empty");
    this._media = el("div", "media", { role: "button", tabindex: "0", "aria-label": "Open snapshot history" });
    this._image = el("img", "", { alt: "", loading: "lazy", decoding: "async" });
    this._emptyLabel = el("div", "empty");
    this._emptyLabel.textContent = "No snapshots";
    this._media.append(this._image, this._emptyLabel);

    this._titleEl = el("span", "title");
    this._eventEl = el("span", "event");
    this._dotEl = el("span", "dot");
    this._dotEl.textContent = "·";
    this._timeEl = el("span", "time");
    const meta = el("span", "meta");
    meta.append(this._eventEl, this._dotEl, this._timeEl);
    const caption = el("div", "caption");
    caption.append(this._titleEl, meta);

    this._card.append(this._media, caption);
    root.append(style, this._card);

    this._onVisibility = this._onVisibility.bind(this);
    this._media.addEventListener("click", () => this._openViewer());
    this._media.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        this._openViewer();
      }
    });
    this._image.addEventListener("error", () => {
      // Broken/pruned image: fall back to the empty state rather than a broken icon.
      this._card.classList.add("empty");
    });
  }

  // --- Lovelace API --------------------------------------------------------

  setConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("petkit-gallery-card: config must be an object");
    }
    if (typeof config.device !== "string" || !config.device.trim()) {
      throw new Error("petkit-gallery-card: 'device' is required (e.g. 'tommy' or 'downstairs')");
    }
    let events = DEFAULT_EVENTS;
    if (Array.isArray(config.events)) {
      events = config.events.map((value) => String(value).trim().toLowerCase()).filter(Boolean);
    } else if (typeof config.events === "string") {
      events = config.events.split(",").map((value) => value.trim().toLowerCase()).filter(Boolean);
    }
    let base = typeof config.base === "string" && config.base ? config.base : DEFAULT_BASE;
    if (!base.endsWith("/")) base += "/";
    const refresh = Number(config.refresh);
    this._config = {
      manifest: typeof config.manifest === "string" && config.manifest ? config.manifest : DEFAULT_MANIFEST,
      base,
      device: config.device.trim().toLowerCase(),
      events,
      title: typeof config.title === "string" ? config.title : "",
      refresh: Number.isFinite(refresh) && refresh > 0 ? Math.max(MIN_REFRESH, refresh) : DEFAULT_REFRESH,
    };
    this._items = [];
    this._loaded = false;
    this._render();
    if (this.isConnected) {
      this._startTimers();
      this._fetchManifest();
    }
  }

  set hass(hass) {
    this._hass = hass;
  }

  get hass() {
    return this._hass;
  }

  getCardSize() {
    return 4;
  }

  // --- lifecycle -----------------------------------------------------------

  connectedCallback() {
    document.addEventListener("visibilitychange", this._onVisibility);
    if (this._config) {
      this._startTimers();
      this._fetchManifest();
    }
  }

  disconnectedCallback() {
    document.removeEventListener("visibilitychange", this._onVisibility);
    this._stopTimers();
    if (this._abort) {
      this._abort.abort();
      this._abort = null;
    }
    if (this._viewer) this._viewer.close();
  }

  _onVisibility() {
    if (document.visibilityState === "visible" && this._config) this._fetchManifest();
  }

  _startTimers() {
    this._stopTimers();
    this._refreshTimer = window.setInterval(() => this._fetchManifest(), this._config.refresh * 1000);
    this._clockTimer = window.setInterval(() => this._renderTime(), CLOCK_TICK_MS);
  }

  _stopTimers() {
    if (this._refreshTimer !== null) {
      window.clearInterval(this._refreshTimer);
      this._refreshTimer = null;
    }
    if (this._clockTimer !== null) {
      window.clearInterval(this._clockTimer);
      this._clockTimer = null;
    }
  }

  // --- data ----------------------------------------------------------------

  _language() {
    const hass = this._hass;
    return (hass && ((hass.locale && hass.locale.language) || hass.language)) || undefined;
  }

  async _fetchManifest() {
    if (!this._config || typeof fetch !== "function") return;
    if (this._abort) this._abort.abort();
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    this._abort = controller;
    const seq = ++this._fetchSeq;
    let snapshots = null;
    try {
      const response = await fetch(this._config.manifest, {
        mode: "cors",
        cache: "no-store",
        credentials: "omit",
        signal: controller ? controller.signal : undefined,
      });
      if (!response.ok) throw new Error(`manifest ${response.status}`);
      const manifest = await response.json();
      snapshots = manifest && Array.isArray(manifest.snapshots) ? manifest.snapshots : [];
    } catch (_error) {
      snapshots = null;
    } finally {
      if (this._abort === controller) this._abort = null;
    }
    if (seq !== this._fetchSeq || !this.isConnected) return;
    if (snapshots === null) {
      // Unreachable: keep the last good list if we have one, otherwise show
      // the quiet empty state. Never throw into Lovelace.
      if (!this._loaded) this._render();
      return;
    }
    this._items = this._filter(snapshots);
    this._loaded = true;
    this._render();
    if (this._viewer && this._viewer.isOpen) this._viewer.update(this._items);
  }

  _filter(snapshots) {
    const { device, events } = this._config;
    const wanted = new Set(events);
    const items = snapshots.filter(
      (item) =>
        item &&
        typeof item === "object" &&
        typeof item.filename === "string" &&
        String(item.device || "").toLowerCase() === device &&
        (wanted.size === 0 || wanted.has(String(item.event || "").toLowerCase()))
    );
    // The manifest is newest-first already; sort defensively by event_at.
    return items
      .map((item, order) => ({ item, order, at: (parseDate(item.event_at) || parseDate(item.captured_at) || new Date(0)).getTime() }))
      .sort((a, b) => b.at - a.at || a.order - b.order)
      .map((entry) => entry.item);
  }

  // --- rendering -----------------------------------------------------------

  _render() {
    if (!this._config) return;
    const item = this._items[0];
    if (!item) {
      this._card.classList.add("empty");
      this._image.removeAttribute("src");
      this._titleEl.textContent = this._config.title || "";
      this._eventEl.textContent = "";
      this._timeEl.textContent = "";
      return;
    }
    const url = imageUrl(this._config.base, item.filename);
    this._card.classList.remove("empty");
    if (this._image.getAttribute("src") !== url) this._image.src = url;
    this._titleEl.textContent = this._config.title || item.device_label || item.device || "";
    this._eventEl.textContent = item.event_label || item.event || "";
    this._renderTime();
  }

  _renderTime() {
    const item = this._items[0];
    if (!item) return;
    const text = relativeTime(parseDate(item.event_at));
    this._timeEl.textContent = text;
    this._dotEl.style.display = text && this._eventEl.textContent ? "" : "none";
  }

  _openViewer() {
    if (!this._config || this._items.length === 0) return;
    if (!this._viewer) this._viewer = document.createElement("petkit-gallery-viewer");
    const viewer = this._viewer;
    const restoreFocus = this._media;
    viewer.open({
      items: this._items,
      index: 0,
      base: this._config.base,
      title: this._config.title,
      language: this._language(),
      onClose: () => {
        try {
          restoreFocus.focus({ preventScroll: true });
        } catch (_error) {
          // ignore
        }
      },
    });
  }
}

// ---------------------------------------------------------------------------
// registration
// ---------------------------------------------------------------------------

if (!customElements.get("petkit-gallery-viewer")) {
  customElements.define("petkit-gallery-viewer", PetkitGalleryViewer);
}
if (!customElements.get("petkit-gallery-card")) {
  customElements.define("petkit-gallery-card", PetkitGalleryCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "petkit-gallery-card")) {
  window.customCards.push({
    type: "petkit-gallery-card",
    name: "PetKit Gallery Card",
    description: "Latest PetKit feeder snapshot with a swipeable fullscreen history.",
    preview: false,
    documentationURL: "https://hass.iacob.uk/petkit-snapshots/",
  });
}
