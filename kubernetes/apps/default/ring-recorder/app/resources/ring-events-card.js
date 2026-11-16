/*
 * ring-events-card: Ring-style live + history for Home Assistant.
 *
 * THESIS: one rail of time. The right end is now (LIVE); everything left of it
 * is the past. A fixed playhead in the middle picks what the stage plays.
 * Refuses the clip gallery plus a separate live view.
 * OWN-WORLD: the dashboard house style (#151515 cards, hairline borders, warm
 * white text). Footage edge to edge, a warm-white playhead with a time bubble,
 * 16:9 thumbnail tiles labelled in words. Red only means LIVE, amber only
 * means notable.
 * STORY: open the card, see the camera now (latest snapshot, tap for live);
 * swipe right-to-left through labelled events; swipe back to the end for LIVE.
 * FIRST VIEWPORT: camera pills, the 16:9 stage, one line of what happened,
 * then the rail with the playhead. On wide cards the details sit beside the stage.
 * FORM: Ring app camera timeline, played straight (pinned by Andrei).
 *
 * Served by ring-recorder at /card.js. Vanilla ES2020, no dependencies.
 *
 *   type: custom:ring-events-card
 *   base_url: https://hass.iacob.uk/ring-events   # optional; default picks same-origin or LAN
 *   mode: full | compact                          # compact = Home dashboard
 *   camera: front-door                            # starting camera, or "all"
 *   cameras: [front-door, driveway, garden]       # optional pill order/subset
 *   entities: { downstairs: camera.dining_room_live_view }  # live entity overrides
 *   more_path: /lovelace/security                 # compact: "All cameras" link
 */

const LAN_BASE_URL = "https://hass.iacob.uk/ring-events";

// Opened through the public HA hostname: use the same-origin route (HA login gated).
// Opened on the LAN (IP or *.local): talk to the LAN route directly.
function defaultBaseUrl() {
  const host = location.hostname;
  const lan = /^(\d+\.){3}\d+$/.test(host) || host.endsWith(".local") || host === "localhost" || host === "hass.iacob.uk";
  return location.protocol === "https:" && !lan ? `${location.origin}/ring-events` : LAN_BASE_URL;
}
const REFRESH_SECONDS = 60;
const PAGE_LIMIT = 100;
const TIME_ZONE = "Europe/London";
const NEAR_EDGE_PX = 240;
const SETTLE_MS = 140;
const LIVE_ID = "__live__";

const DEFAULT_ENTITIES = {
  "front-door": "camera.front_door_live_view",
  driveway: "camera.driveway_live_view",
  garden: "camera.garden_live_view",
  hallway: "camera.hallway_live_view",
  downstairs: "camera.dining_room_live_view",
  living: "camera.living_live_view",
  "meli-s": "camera.meli_s_live_view",
};

const FALLBACK_CAMERAS = [
  { slug: "front-door", name: "Front Door" },
  { slug: "driveway", name: "Driveway" },
  { slug: "garden", name: "Garden" },
];

const TAG_LABELS = [
  ["delivery", "Delivery"],
  ["unknown person", "Person"],
  ["visitor", "Visitor"],
  ["family", "Family"],
  ["passer-by", "Passer-by"],
  ["car", "Car"],
  ["vehicle", "Car"],
  ["cat", "Cat"],
  ["dog", "Dog"],
  ["fox", "Fox"],
  ["nothing", "No one visible"],
];

/* ---------- small helpers ---------- */

function el(tag, className, attrs) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (attrs) for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

function slugify(value) {
  return String(value || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

function titleFromSlug(value) {
  return String(value || "").split("-").filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1)).join(" ");
}

function parseDate(value) {
  if (typeof value !== "string" || !value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function londonKey(date) {
  if (!date) return "unknown";
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone: TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(date).map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function shiftKey(key, days) {
  const [y, m, d] = key.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
}

function formatTime(date, language) {
  return new Intl.DateTimeFormat(language, { timeZone: TIME_ZONE, hour: "2-digit", minute: "2-digit" }).format(date);
}

function dayName(date, language) {
  const key = londonKey(date);
  const today = londonKey(new Date());
  if (key === today) return "Today";
  if (key === shiftKey(today, -1)) return "Yesterday";
  return new Intl.DateTimeFormat(language, {
    timeZone: TIME_ZONE, weekday: "short", day: "numeric", month: "short",
  }).format(date);
}

function relativeWhen(date, language) {
  return `${dayName(date, language)} ${formatTime(date, language)}`;
}

function eventLabel(event) {
  if (event.analysis.label) return event.analysis.label;
  if (event.quick.label) return event.quick.label;
  const tags = event.analysis.tags.map((tag) => tag.toLowerCase());
  for (const [tag, label] of TAG_LABELS) if (tags.includes(tag)) return label;
  if (event.kind === "ding") return "Doorbell";
  if (event.kind === "on_demand") return "Live view";
  return "Motion";
}

function normaliseEvent(raw) {
  if (!raw || typeof raw !== "object" || raw.id === undefined || !raw.created_at) return null;
  const date = parseDate(String(raw.created_at));
  if (!date) return null;
  const a = raw.analysis && typeof raw.analysis === "object" ? raw.analysis : {};
  const q = raw.quick && typeof raw.quick === "object" ? raw.quick : null;
  const persons = Array.isArray(a.persons) ? a.persons.filter((p) => p && typeof p.role === "string") : [];
  const tags = Array.isArray(a.activity_tags) ? a.activity_tags : Array.isArray(a.tags) ? a.tags : [];
  const device = slugify(raw.device || raw.device_slug || raw.device_name);
  return {
    id: String(raw.id),
    device,
    deviceName: String(raw.device_name || titleFromSlug(device)),
    kind: String(raw.kind || "motion"),
    date,
    clipUrl: typeof raw.clip_url === "string" && raw.clip_url ? raw.clip_url : null,
    thumbUrl: typeof raw.thumb_url === "string" && raw.thumb_url ? raw.thumb_url : null,
    duration: Number(raw.duration) || null,
    quick: {
      label: q && typeof q.label === "string" ? q.label.trim() : "",
      headline: q && typeof q.headline === "string" ? q.headline.trim() : "",
    },
    status: String(raw.status || "pending"),
    analysis: {
      label: typeof a.label === "string" ? a.label.trim() : "",
      summary: typeof a.summary === "string" ? a.summary : "",
      detail: typeof a.detail === "string" ? a.detail : "",
      tags: tags.map(String).filter(Boolean),
      notable: a.notable === true,
      roles: persons.map((p) => p.role),
      fromQuick: a.source === "quick",
    },
  };
}

/* "2 residents", "Resident and visitor": who the full pass saw, for the meta line. */
function describeRoles(roles) {
  const counts = new Map();
  for (const role of roles) if (role && role !== "unknown") counts.set(role, (counts.get(role) || 0) + 1);
  const parts = [...counts].map(([role, n]) => {
    const word = role === "passer-by" ? "passer-by" : role;
    if (n === 1) return word;
    return `${n} ${word === "passer-by" ? "passers-by" : word === "delivery" ? "couriers" : `${word}s`}`;
  });
  if (!parts.length) return "";
  const text = parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}`;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function sortEvents(events) {
  const byId = new Map();
  for (const event of events) byId.set(event.id, event);
  return [...byId.values()].sort((left, right) => left.date - right.date);
}

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---------- styles ---------- */

const STYLE = `
:host {
  display: block;
  --rc-bg: var(--ha-card-background, var(--card-background-color, #151515));
  --rc-text: var(--primary-text-color, #f2f0ec);
  --rc-muted: rgba(242, 240, 236, 0.62);
  --rc-faint: rgba(242, 240, 236, 0.38);
  --rc-line: rgba(255, 255, 255, 0.08);
  --rc-raised: rgba(255, 255, 255, 0.05);
  --rc-hover: rgba(255, 255, 255, 0.09);
  --rc-live: #ff5147;
  --rc-notable: #f2b04a;
  --rc-playhead: #f6efe4;
  --rc-radius: var(--ha-card-border-radius, 16px);
  --rc-tile-w: 132px;
  --rc-ease: cubic-bezier(0.22, 1, 0.36, 1);
  color-scheme: dark;
}
* { box-sizing: border-box; }
ha-card, .card {
  display: block;
  overflow: hidden;
  background: var(--rc-bg);
  color: var(--rc-text);
  border: 1px solid var(--rc-line);
  border-radius: var(--rc-radius);
  box-shadow: none;
  container-type: inline-size;
  outline: none;
}
.card:focus-visible { box-shadow: 0 0 0 2px var(--rc-playhead); }

/* header: camera pills */
.top {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
}
.pills {
  display: flex;
  gap: 6px;
  flex: 1;
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: none;
  -webkit-mask-image: linear-gradient(90deg, #000 calc(100% - 24px), transparent);
          mask-image: linear-gradient(90deg, #000 calc(100% - 24px), transparent);
  padding-right: 24px;
}
.pills::-webkit-scrollbar { display: none; }
.pill, .action {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 12px;
  border: 1px solid var(--rc-line);
  border-radius: 999px;
  background: transparent;
  color: var(--rc-muted);
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  letter-spacing: 0.01em;
  cursor: pointer;
  transition: background 160ms var(--rc-ease), color 160ms var(--rc-ease), border-color 160ms var(--rc-ease);
  -webkit-tap-highlight-color: transparent;
}
.pill:hover, .action:hover { background: var(--rc-hover); color: var(--rc-text); }
.pill[aria-pressed="true"] { background: var(--rc-text); color: #111; border-color: var(--rc-text); }
.pill:focus-visible, .action:focus-visible, .tile:focus-visible, .stage-btn:focus-visible, .more:focus-visible {
  outline: 2px solid var(--rc-playhead);
  outline-offset: 2px;
}
.action { color: var(--rc-text); }
.action svg { width: 16px; height: 16px; }
.action.live-jump[hidden] { display: none; }
.live-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--rc-live);
  box-shadow: 0 0 0 0 rgba(255, 81, 71, 0.5);
}
.is-live .live-dot, .tile.live[aria-current="true"] .live-dot { animation: pulse 1.8s var(--rc-ease) infinite; }
@keyframes pulse {
  0% { box-shadow: 0 0 0 0 rgba(255, 81, 71, 0.55); }
  100% { box-shadow: 0 0 0 7px rgba(255, 81, 71, 0); }
}

/* body: stage + details */
.body { display: grid; grid-template-columns: 1fr; }
.stage {
  position: relative;
  aspect-ratio: 16 / 9;
  background: #050505;
  overflow: hidden;
}
.stage > video, .stage > img, .stage > ha-camera-stream, .stage > .live-host {
  position: absolute; inset: 0; width: 100%; height: 100%;
  object-fit: contain; display: block; background: #050505;
}
.live-host ha-camera-stream, .live-host > * { width: 100%; height: 100%; display: block; }
.stage img.snapshot { object-fit: cover; filter: saturate(0.9) brightness(0.72); transition: filter 240ms var(--rc-ease); }
.overlay {
  position: absolute; left: 0; right: 0; top: 0;
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px 28px;
  background: linear-gradient(180deg, rgba(0,0,0,0.62), transparent);
  pointer-events: none;
  font-size: 13px;
}
.overlay .cam { font-weight: 600; }
.overlay .when { color: var(--rc-muted); font-variant-numeric: tabular-nums; }
.badge {
  margin-left: auto;
  display: inline-flex; align-items: center; gap: 6px;
  height: 22px; padding: 0 8px;
  border-radius: 6px;
  background: rgba(0,0,0,0.55);
  font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
}
.badge.live { background: var(--rc-live); color: #fff; }
.badge.live .live-dot { background: #fff; }
.stage-cta {
  position: absolute; inset: 0;
  display: grid; place-items: center;
}
.stage-btn {
  display: inline-flex; align-items: center; gap: 10px;
  height: 48px; padding: 0 20px 0 16px;
  border: 0; border-radius: 999px;
  background: rgba(10,10,10,0.72);
  -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px);
  color: #fff; font: inherit; font-size: 15px; font-weight: 600;
  cursor: pointer;
  box-shadow: 0 6px 24px rgba(0,0,0,0.35);
  transition: transform 160ms var(--rc-ease), background 160ms var(--rc-ease);
}
.stage-btn:hover { background: rgba(10,10,10,0.86); transform: translateY(-1px); }
.stage-btn:active { transform: translateY(0) scale(0.98); }
.stage-btn .live-dot { width: 9px; height: 9px; }
.stage-note {
  position: absolute; left: 50%; bottom: 14px; transform: translateX(-50%);
  max-width: calc(100% - 32px);
  padding: 6px 10px; border-radius: 8px;
  background: rgba(0,0,0,0.6); color: var(--rc-muted);
  font-size: 12px; text-align: center;
}
.stage-empty {
  position: absolute; inset: 0; display: grid; place-items: center; padding: 24px;
  color: var(--rc-muted); font-size: 14px; text-align: center;
}

.details { padding: 12px 14px 10px; min-width: 0; }
.headline { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.label {
  font-size: 17px; font-weight: 600; letter-spacing: -0.01em; line-height: 1.25;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.label .flag { color: var(--rc-notable); }
.meta { color: var(--rc-muted); font-size: 13px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.summary { margin: 4px 0 0; color: var(--rc-muted); font-size: 14px; line-height: 1.45; max-width: 68ch; }
.more {
  margin-top: 6px; padding: 0; border: 0; background: none;
  color: var(--rc-text); font: inherit; font-size: 13px; font-weight: 500; cursor: pointer;
  text-decoration: underline; text-decoration-color: var(--rc-faint); text-underline-offset: 3px;
}
.more[hidden], .summary[hidden] { display: none; }
.detail { margin: 6px 0 0; color: var(--rc-muted); font-size: 13px; line-height: 1.5; max-width: 68ch; }
.detail[hidden] { display: none; }
.notable .label { color: var(--rc-notable); }

/* rail */
.rail-wrap { position: relative; border-top: 1px solid var(--rc-line); }
.rail {
  display: flex; align-items: stretch; gap: 8px;
  overflow-x: auto; overflow-y: hidden;
  padding: 26px calc(50% - var(--rc-tile-w) / 2) 14px;
  scroll-snap-type: x mandatory;
  scrollbar-width: none;
  overscroll-behavior-x: contain;
  touch-action: pan-x;
}
.rail::-webkit-scrollbar { display: none; }
.playhead {
  position: absolute; top: 0; left: 50%;
  height: calc(30px + var(--rc-tile-w) * 9 / 16);
  width: 2px; margin-left: -1px;
  background: var(--rc-playhead);
  border-radius: 2px;
  pointer-events: none;
  opacity: 0.9;
  box-shadow: 0 0 0 1px rgba(0,0,0,0.35);
}
.bubble {
  position: absolute; top: 4px; left: 50%; transform: translateX(-50%);
  height: 18px; padding: 0 8px; border-radius: 5px;
  background: var(--rc-playhead); color: #141414;
  font-size: 11px; font-weight: 700; line-height: 18px; white-space: nowrap;
  font-variant-numeric: tabular-nums; letter-spacing: 0.02em;
  pointer-events: none;
}
.bubble.live { background: var(--rc-live); color: #fff; letter-spacing: 0.08em; }
.edge {
  position: absolute; top: 0; bottom: 0; width: 44px; pointer-events: none;
}
.edge.left { left: 0; background: linear-gradient(90deg, var(--rc-bg), transparent); }
.edge.right { right: 0; background: linear-gradient(270deg, var(--rc-bg), transparent); }

.tile {
  position: relative;
  flex: none;
  width: var(--rc-tile-w);
  scroll-snap-align: center;
  padding: 0; border: 0; background: none; color: inherit; font: inherit; text-align: left;
  cursor: pointer;
  -webkit-tap-highlight-color: transparent;
}
.thumb {
  position: relative; display: block;
  aspect-ratio: 16 / 9; border-radius: 9px; overflow: hidden;
  background: #0b0b0b;
  outline: 1px solid var(--rc-line); outline-offset: -1px;
  transition: outline-color 160ms var(--rc-ease), transform 200ms var(--rc-ease);
}
.thumb video, .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; pointer-events: none; }
.tile:hover .thumb { outline-color: rgba(255,255,255,0.22); }
.tile[aria-current="true"] .thumb { outline: 2px solid var(--rc-playhead); outline-offset: -2px; }
.tile.notable .thumb::after {
  content: ""; position: absolute; top: 6px; right: 6px; width: 8px; height: 8px; border-radius: 50%;
  background: var(--rc-notable); box-shadow: 0 0 0 2px rgba(0,0,0,0.5);
}
.tile .kind-ding {
  position: absolute; left: 6px; top: 6px; height: 16px; padding: 0 5px; border-radius: 4px;
  background: rgba(0,0,0,0.6); font-size: 10px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
}
.tile-copy { display: block; padding: 6px 2px 0; }
.tile-time { display: block; color: var(--rc-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.tile-label {
  display: block; margin-top: 1px; font-size: 13px; font-weight: 600; line-height: 1.25;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.tile.notable .tile-label { color: var(--rc-notable); }
.tile .tile-cam { color: var(--rc-faint); font-weight: 500; }

.tile.live .thumb {
  display: grid; place-items: center;
  background: radial-gradient(120% 120% at 50% 40%, #2a1210, #0b0b0b 70%);
}
.tile.live .thumb img { position: absolute; inset: 0; opacity: 0.45; }
.live-word {
  position: relative; display: inline-flex; align-items: center; gap: 6px;
  font-size: 13px; font-weight: 800; letter-spacing: 0.14em; color: #fff;
}

.day {
  flex: none; align-self: stretch;
  display: flex; flex-direction: column; justify-content: flex-start;
  width: 1px; margin: 0 6px; position: relative;
  background: var(--rc-line);
}
.day span {
  position: absolute; top: -20px; left: 6px; white-space: nowrap;
  color: var(--rc-faint); font-size: 11px; font-weight: 600; letter-spacing: 0.02em;
}
.loader { flex: none; width: 40px; display: grid; place-items: center; color: var(--rc-faint); font-size: 11px; }

.foot {
  display: flex; align-items: center; gap: 10px;
  padding: 0 14px 12px; color: var(--rc-faint); font-size: 12px; min-height: 18px;
}
.foot .status.error { color: #ff8a80; }
.foot .spacer { flex: 1; }
.foot input[type="date"] {
  height: 28px; padding: 0 8px; border-radius: 8px;
  border: 1px solid var(--rc-line); background: transparent; color: var(--rc-muted);
  font: inherit; font-size: 12px; color-scheme: dark;
}
.foot a { color: var(--rc-text); text-decoration: none; font-weight: 500; font-size: 13px; }
.foot a:hover { text-decoration: underline; }

.compact { --rc-tile-w: 116px; }
.compact .details { padding: 10px 12px 8px; }
.compact .label { font-size: 15px; }

@container (min-width: 760px) {
  .full .body { grid-template-columns: minmax(0, 1fr) minmax(240px, 32%); }
  .full .details { padding: 18px 18px 16px; border-left: 1px solid var(--rc-line); }
  .full .headline { flex-direction: column; align-items: flex-start; gap: 2px; }
  .full .label { font-size: 20px; white-space: normal; }
  .full .summary { font-size: 14px; margin-top: 10px; }
  .full { --rc-tile-w: 156px; }
}

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
}
`;

const ICONS = {
  forward: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M13 6.5 18.5 12 13 17.5l-1.4-1.4 3.1-3.1H5v-2h9.7l-3.1-3.1z"/></svg>',
};

/* ---------- the card ---------- */

class RingEventsCard extends HTMLElement {
  static getStubConfig() {
    return { mode: "full", camera: "front-door" };
  }

  static getConfigElement() {
    return undefined;
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._events = [];
    this._cameras = FALLBACK_CAMERAS;
    this._selected = LIVE_ID;
    this._liveActive = false;
    this._hasMore = true;
    this._loading = false;
    this._userScrolled = false;
    this._settleTimer = null;
    this._refreshTimer = null;
    this._fetchSeq = 0;
    this._refreshSeq = 0;
    this._jumpSeq = 0;
    this._detailOpen = false;
    this._built = false;
  }

  setConfig(config) {
    if (!config || typeof config !== "object") throw new Error("ring-events-card: config must be an object");
    if (config.mode && !["full", "compact"].includes(config.mode)) {
      throw new Error("ring-events-card: mode must be 'full' or 'compact'");
    }
    if (config.cameras && !Array.isArray(config.cameras)) throw new Error("ring-events-card: cameras must be a list");
    this._config = {
      baseUrl: String(config.base_url || defaultBaseUrl()).replace(/\/+$/, ""),
      mode: config.mode || "full",
      camera: slugify(config.camera || "front-door") || "front-door",
      cameras: Array.isArray(config.cameras) ? config.cameras.map(slugify).filter(Boolean) : null,
      entities: { ...DEFAULT_ENTITIES, ...(config.entities || {}) },
      morePath: config.more_path || "/lovelace/security",
    };
    this._camera = this._config.camera;
    this._cameras = FALLBACK_CAMERAS;
    this._camerasLoaded = false;
    if (this._built) {
      this._stopLive();
      this._build();
      if (this.isConnected) this._reload();
    } else if (this.isConnected) {
      this.connectedCallback();
    }
  }

  set hass(hass) {
    this._hass = hass;
    if (this._liveStream) {
      this._liveStream.hass = hass;
      const stateObj = hass.states[this._liveEntity()];
      if (stateObj) this._liveStream.stateObj = stateObj;
    }
    const snap = this._snapshotUrl();
    if (snap === this._lastSnap || !this._built) return;
    this._lastSnap = snap;
    // Snapshot URLs carry a rotating token; swap sources in place instead of re-rendering.
    const liveThumb = this._rail.querySelector(".tile.live .thumb");
    if (liveThumb && snap) {
      let liveImg = liveThumb.querySelector("img");
      if (!liveImg) {
        liveImg = el("img", "", { alt: "" });
        liveThumb.prepend(liveImg);
      }
      liveImg.src = snap;
    }
    const stageImg = this._stage.querySelector("img.snapshot");
    if (stageImg && snap) stageImg.src = snap;
    else if (this._selected === LIVE_ID && !this._liveActive) this._renderStage();
  }

  getCardSize() {
    return this._config && this._config.mode === "compact" ? 6 : 9;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  connectedCallback() {
    if (!this._config) return;
    if (!this._built) this._build();
    this._onVisibility = () => {
      if (document.hidden) {
        this._stopTimer();
        this._suspendMedia();
      } else {
        this._startTimer();
        this._renderStage();
        this._refresh();
      }
    };
    document.addEventListener("visibilitychange", this._onVisibility);
    this._startTimer();
    if (!this._events.length) {
      this._reload();
    } else {
      this._observeThumbs([...this._rail.querySelectorAll(".thumb video:not([src])")]);
      this._renderStage(true);
    }
  }

  disconnectedCallback() {
    this._stopTimer();
    if (this._onVisibility) document.removeEventListener("visibilitychange", this._onVisibility);
    this._fetchSeq += 1;
    this._loading = false;
    this._suspendMedia();
    if (this._observer) this._observer.disconnect();
  }

  _suspendMedia() {
    this._stopLive();
    if (this._stage) this._clearStage();
    if (this._selected === LIVE_ID && this._stage && this.isConnected) this._renderStage(true);
  }

  /* ----- structure ----- */

  _build() {
    this._built = true;
    const root = this.shadowRoot;
    root.innerHTML = "";
    const style = el("style");
    style.textContent = STYLE;
    root.append(style);

    this._card = el("div", `card ${this._config.mode}`, { tabindex: "0", role: "region", "aria-label": "Ring cameras" });

    const top = el("div", "top");
    this._pills = el("div", "pills", { role: "toolbar", "aria-label": "Camera" });
    this._liveJump = el("button", "action live-jump", { type: "button" });
    this._liveJump.innerHTML = '<span class="live-dot"></span>Live';
    this._liveJump.addEventListener("click", () => this._goLive(true));
    top.append(this._pills, this._liveJump);

    const body = el("div", "body");
    this._stage = el("div", "stage");
    const details = el("div", "details");
    const headline = el("div", "headline");
    this._label = el("div", "label");
    this._meta = el("div", "meta");
    headline.append(this._label, this._meta);
    this._summary = el("p", "summary");
    this._more = el("button", "more", { type: "button", "aria-expanded": "false" });
    this._more.textContent = "More detail";
    this._more.addEventListener("click", () => {
      this._detailOpen = !this._detailOpen;
      this._renderDetails();
    });
    this._detail = el("p", "detail");
    details.append(headline, this._summary, this._more, this._detail);
    body.append(this._stage, details);

    const railWrap = el("div", "rail-wrap");
    this._rail = el("div", "rail", { role: "listbox", "aria-label": "Timeline, newest on the right" });
    this._bubble = el("div", "bubble");
    railWrap.append(this._rail, el("div", "edge left"), el("div", "edge right"), el("div", "playhead"), this._bubble);

    const foot = el("div", "foot");
    this._status = el("span", "status", { role: "status", "aria-live": "polite" });
    foot.append(this._status, el("span", "spacer"));
    if (this._config.mode === "full") {
      this._date = el("input", "", { type: "date", "aria-label": "Jump to date" });
      this._date.addEventListener("change", () => this._jumpToDate(this._date.value));
      foot.append(this._date);
    } else {
      const link = el("a", "", { href: this._config.morePath });
      link.textContent = "All cameras";
      link.addEventListener("click", (event) => {
        event.preventDefault();
        history.pushState(null, "", this._config.morePath);
        window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
      });
      foot.append(link);
    }

    this._card.append(top, body, railWrap, foot);
    root.append(this._card);

    this._rail.addEventListener("scroll", () => this._onRailScroll(), { passive: true });
    for (const type of ["wheel", "pointerdown", "touchstart", "keydown"]) {
      this._rail.addEventListener(type, () => { this._userScrolled = true; }, { passive: true });
    }
    this._card.addEventListener("keydown", (event) => this._onKey(event));
    this._renderPills();
  }

  /* ----- data ----- */

  _url(path, params) {
    const url = new URL(`${this._config.baseUrl}${path}`);
    for (const [key, value] of Object.entries(params || {})) {
      if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
    }
    return url.toString();
  }

  _sameOrigin() {
    return new URL(this._config.baseUrl).origin === location.origin;
  }

  // Same-origin route sits behind a session cookie; trade the HA login token for one.
  async _login() {
    const auth = this._hass && this._hass.auth;
    if (!auth) throw new Error("login needs Home Assistant");
    if (auth.expired && auth.refreshAccessToken) await auth.refreshAccessToken();
    const response = await fetch(`${this._config.baseUrl}/api/session`, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { Authorization: `Bearer ${auth.data.access_token}` },
    });
    if (!response.ok) throw new Error(`session ${response.status}`);
    // Thumbnails that failed while the session was gone (recorder restart) load again now.
    for (const img of this.shadowRoot.querySelectorAll(".thumb img[data-retry]")) {
      img.src = img.dataset.retry;
      img.removeAttribute("data-retry");
      img.hidden = false;
    }
  }

  async _get(path, params) {
    const same = this._sameOrigin();
    const go = () => fetch(this._url(path, params), {
      mode: "cors", cache: "no-store", credentials: same ? "same-origin" : "omit",
    });
    let response = await go();
    if (response.status === 401 && same) {
      if (!this._loginPromise) this._loginPromise = this._login().finally(() => { this._loginPromise = null; });
      await this._loginPromise;
      response = await go();
    }
    if (!response.ok) throw new Error(`${path} ${response.status}`);
    return response.json();
  }

  // Returns events plus whether the server page was full, judged before any client-side camera filter.
  async _fetchEvents(extra) {
    const params = { limit: PAGE_LIMIT, ...extra };
    if (this._camera !== "all") params.device = this._camera;
    const payload = await this._get("/api/events", params);
    const rows = Array.isArray(payload) ? payload : (payload && payload.events) || [];
    let events = rows.map(normaliseEvent).filter(Boolean);
    // Server builds clip URLs for the LAN host; serve them from whichever route we use.
    for (const event of events) this._localise(event);
    if (this._camera === "all" && this._config.cameras) {
      events = events.filter((event) => this._config.cameras.includes(event.device));
    }
    const oldestRaw = rows.length ? parseDate(String(rows[rows.length - 1].created_at)) : null;
    return { events, full: rows.length >= PAGE_LIMIT, oldestRaw };
  }

  async _loadCameras() {
    try {
      const payload = await this._get("/api/devices");
      const rows = Array.isArray(payload) ? payload : (payload && payload.devices) || [];
      let cameras = rows.map((row) => ({ slug: slugify(row.slug || row.name), name: String(row.name || row.slug) }))
        .filter((cam) => cam.slug);
      const order = this._config.cameras || ["front-door", "driveway", "garden", "hallway", "downstairs", "living", "meli-s"];
      cameras = cameras.filter((cam) => !this._config.cameras || this._config.cameras.includes(cam.slug))
        .sort((a, b) => (order.indexOf(a.slug) + 99) % 99 - (order.indexOf(b.slug) + 99) % 99);
      if (cameras.length) this._cameras = cameras;
    } catch (_error) {
      /* keep fallback list */
    }
    this._renderPills();
  }

  async _reload() {
    const seq = ++this._fetchSeq;
    this._loading = false;
    this._setStatus("Loading events...");
    this._stopLive();
    this._events = [];
    this._selected = LIVE_ID;
    this._userScrolled = false;
    this._renderRail();
    this._renderStage();
    if (!this._camerasLoaded) {
      this._camerasLoaded = true;
      this._loadCameras();
    }
    try {
      const page = await this._fetchEvents({});
      if (seq !== this._fetchSeq || !this.isConnected) return;
      this._events = sortEvents(page.events);
      this._hasMore = page.full;
      this._cursor = page.oldestRaw;
      this._renderRail();
      this._parkAtLive();
      this._setStatus(this._events.length ? "" : "No recordings yet for this camera.");
    } catch (_error) {
      if (seq !== this._fetchSeq) return;
      this._setStatus("Can't reach the Ring archive. Retrying in a minute.", true);
    }
  }

  // Where the selected tile sits in the viewport, so a re-render can put it back.
  _anchor() {
    const tile = this._tileFor(this._selected);
    if (!tile) return null;
    return { id: this._selected, offset: tile.getBoundingClientRect().left - this._rail.getBoundingClientRect().left };
  }

  _restoreAnchor(anchor) {
    const tile = anchor && this._tileFor(anchor.id);
    if (!tile) return;
    this._quietUntil = Date.now() + 400;
    const now = tile.getBoundingClientRect().left - this._rail.getBoundingClientRect().left;
    this._rail.scrollTo({ left: this._rail.scrollLeft + now - anchor.offset, behavior: "instant" });
  }

  async _loadOlder() {
    if (this._loading || !this._hasMore || !this._events.length) return false;
    this._loading = true;
    const seq = this._fetchSeq;
    this._setStatus("Loading older events...");
    try {
      const before = this._events.length;
      let merged = this._events;
      // Pages are cut on raw rows (camera filter applies after), so walk raw pages until one adds something.
      for (let hop = 0; hop < 5 && this._hasMore && merged.length === before; hop += 1) {
        // Ring timestamps are per second; overlap by one second and dedupe so ties on a page edge are kept.
        const cursor = this._cursor || this._events[0].date;
        const page = await this._fetchEvents({ until: new Date(cursor.getTime() + 1000).toISOString() });
        if (seq !== this._fetchSeq || !this.isConnected) return false;
        merged = sortEvents([...page.events, ...merged]);
        const moved = page.oldestRaw && page.oldestRaw < cursor;
        this._cursor = page.oldestRaw || cursor;
        this._hasMore = page.full && Boolean(moved);
      }
      const anchor = this._anchor();
      this._events = merged;
      this._renderRail();
      this._restoreAnchor(anchor);
      this._setStatus(this._hasMore ? "" : "Start of the archive");
      return true;
    } catch (_error) {
      if (seq === this._fetchSeq) this._setStatus("Older events didn't load. Scroll left to retry.", true);
      return false;
    } finally {
      if (seq === this._fetchSeq) this._loading = false;
    }
  }

  async _refresh() {
    if (!this.isConnected || this._loading) return;
    if (!this._events.length && this._status.classList.contains("error")) return this._reload();
    const seq = this._fetchSeq;
    const refreshSeq = ++this._refreshSeq;
    try {
      // Latest page, not just newer rows: descriptions land on recent clips after they appear.
      const page = await this._fetchEvents({});
      const current = this._selectedEvent();
      if (current && !current.clipUrl && !page.events.some((event) => event.id === current.id)) {
        const one = normaliseEvent(await this._get(`/api/events/${encodeURIComponent(current.id)}`));
        if (one) this._localise(one);
        if (one) page.events.push(one);
      }
      if (seq !== this._fetchSeq || refreshSeq !== this._refreshSeq || this._loading || !this.isConnected) return;
      const byId = new Map(this._events.map((event) => [event.id, event]));
      const stamp = (event) => JSON.stringify([event.clipUrl, event.status, event.analysis, event.quick]);
      const added = page.events.some((event) => !byId.has(event.id));
      const updated = page.events.filter((event) => byId.has(event.id) && stamp(byId.get(event.id)) !== stamp(event));
      if (!added && !updated.length) return;
      const selected = updated.find((event) => event.id === this._selected);
      const clipArrived = selected && selected.clipUrl !== byId.get(selected.id).clipUrl;
      const atLive = this._selected === LIVE_ID;
      const anchor = this._anchor();
      this._events = sortEvents([...this._events, ...page.events]);
      this._renderRail();
      if (atLive) this._parkAtLive();
      else this._restoreAnchor(anchor);
      if (clipArrived) this._renderStage(true);
      else this._renderDetails();
      this._setStatus("");
    } catch (_error) {
      /* transient; next tick retries */
    }
  }

  async _jumpToDate(value) {
    const token = ++this._jumpSeq;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return;
    const seq = this._fetchSeq;
    this._userScrolled = true;
    while (this._loading) {
      await new Promise((resolve) => window.setTimeout(resolve, 100));
      if (token !== this._jumpSeq) return;
    }
    while (this._events.length && londonKey(this._events[0].date) > value && this._hasMore) {
      if (seq !== this._fetchSeq || token !== this._jumpSeq) return;
      if (!(await this._loadOlder())) return;
    }
    if (seq !== this._fetchSeq || token !== this._jumpSeq) return;
    const hit = this._events.find((event) => londonKey(event.date) >= value);
    if (!hit) {
      this._setStatus("Nothing recorded on or after that day.");
      return;
    }
    if (londonKey(hit.date) !== value) this._setStatus(`Nothing on that day. Showing ${dayName(hit.date, this._language())}.`);
    this._centerTile(hit.id, false);
  }

  /* ----- timers ----- */

  _startTimer() {
    this._stopTimer();
    this._refreshTimer = window.setInterval(() => this._refresh(), REFRESH_SECONDS * 1000);
  }

  _stopTimer() {
    if (this._refreshTimer) window.clearInterval(this._refreshTimer);
    this._refreshTimer = null;
  }

  /* ----- rendering ----- */

  _language() {
    const hass = this._hass;
    const language = (hass && ((hass.locale && hass.locale.language) || hass.language)) || "en-GB";
    return language === "en" ? "en-GB" : language;
  }

  _cameraName(slug) {
    const cam = this._cameras.find((item) => item.slug === slug);
    return cam ? cam.name : titleFromSlug(slug);
  }

  _liveCamera() {
    return this._camera === "all" ? this._config.camera === "all" ? "front-door" : this._config.camera : this._camera;
  }

  _liveEntity() {
    return this._config.entities[this._liveCamera()] || `camera.${this._liveCamera().replace(/-/g, "_")}_live_view`;
  }

  _renderPills() {
    if (!this._pills) return;
    this._pills.innerHTML = "";
    const entries = [{ slug: "all", name: "All" }, ...this._cameras];
    for (const cam of entries) {
      const pill = el("button", "pill", { type: "button", "aria-pressed": String(cam.slug === this._camera) });
      pill.textContent = cam.name;
      pill.addEventListener("click", () => {
        if (this._camera === cam.slug) return;
        this._camera = cam.slug;
        this._renderPills();
        this._reload();
      });
      this._pills.append(pill);
    }
  }

  _renderRail() {
    const active = this.shadowRoot.activeElement;
    const hadFocus = Boolean(active && active.classList && active.classList.contains("tile"));
    if (this._observer) this._observer.disconnect();
    this._rail.innerHTML = "";
    const language = this._language();
    const lazy = [];
    if (this._hasMore && this._events.length) {
      const loader = el("div", "loader", { "aria-hidden": "true" });
      loader.textContent = "...";
      this._rail.append(loader);
    }
    let lastDay = null;
    for (const event of this._events) {
      const key = londonKey(event.date);
      if (key !== lastDay) {
        const day = el("div", "day", { "aria-hidden": "true" });
        const span = el("span");
        span.textContent = dayName(event.date, language);
        day.append(span);
        this._rail.append(day);
        lastDay = key;
      }
      const label = eventLabel(event);
      const tile = el("button", `tile${event.analysis.notable ? " notable" : ""}`, {
        type: "button",
        role: "option",
        "data-id": event.id,
        "aria-current": String(this._selected === event.id),
        "aria-selected": String(this._selected === event.id),
        tabindex: this._selected === event.id ? "0" : "-1",
        "aria-label": `${event.deviceName}, ${label}, ${relativeWhen(event.date, language)}`,
      });
      const thumb = el("span", "thumb");
      if (event.thumbUrl) {
        // Hero frame at the moment Ring saw someone: a small JPEG instead of a video per tile.
        const img = el("img", "", { alt: "", loading: "lazy", decoding: "async", src: event.thumbUrl });
        img.addEventListener("load", () => { delete img.dataset.retried; });
        img.addEventListener("error", () => {
          if (img.dataset.retried) { img.remove(); return; }
          img.dataset.retried = "1";
          img.dataset.retry = img.src;
          img.hidden = true;
          // A 401 here means the session lapsed; one refresh logs in and retries them all.
          if (!this._thumbRetry) {
            this._thumbRetry = setTimeout(() => { this._thumbRetry = null; if (this.isConnected) this._refresh(); }, 800);
          }
        });
        thumb.append(img);
      } else if (event.clipUrl) {
        const video = el("video", "", { muted: "", playsinline: "", preload: "none", "aria-hidden": "true" });
        video.muted = true;
        video.dataset.src = `${event.clipUrl}#t=0.6`;
        thumb.append(video);
        lazy.push(video);
      }
      if (event.kind === "ding") {
        const ding = el("span", "kind-ding");
        ding.textContent = "Ding";
        thumb.append(ding);
      }
      const copy = el("span", "tile-copy");
      const time = el("span", "tile-time");
      time.textContent = formatTime(event.date, language);
      if (this._camera === "all") {
        const cam = el("span", "tile-cam");
        cam.textContent = ` · ${event.deviceName}`;
        time.append(cam);
      }
      const text = el("span", "tile-label");
      text.textContent = label;
      copy.append(time, text);
      tile.append(thumb, copy);
      tile.addEventListener("click", () => {
        this._userScrolled = true;
        this._centerTile(event.id, true);
      });
      this._rail.append(tile);
    }

    const live = el("button", "tile live", {
      type: "button", role: "option", "data-id": LIVE_ID,
      "aria-current": String(this._selected === LIVE_ID),
      "aria-selected": String(this._selected === LIVE_ID),
      tabindex: this._selected === LIVE_ID ? "0" : "-1",
      "aria-label": `Live view, ${this._cameraName(this._liveCamera())}`,
    });
    const liveThumb = el("span", "thumb");
    const snap = this._snapshotUrl();
    if (snap) {
      const img = el("img", "", { alt: "", loading: "lazy" });
      img.src = snap;
      liveThumb.append(img);
    }
    const word = el("span", "live-word");
    word.innerHTML = '<span class="live-dot"></span>LIVE';
    liveThumb.append(word);
    const liveCopy = el("span", "tile-copy");
    const liveTime = el("span", "tile-time");
    liveTime.textContent = "Now";
    const liveLabel = el("span", "tile-label");
    liveLabel.textContent = this._cameraName(this._liveCamera());
    liveCopy.append(liveTime, liveLabel);
    live.append(liveThumb, liveCopy);
    live.addEventListener("click", () => {
      this._userScrolled = true;
      if (this._selected === LIVE_ID) this._startLive();
      else this._goLive(true);
    });
    this._rail.append(live);
    this._observeThumbs(lazy);
    if (hadFocus) {
      const tile = this._tileFor(this._selected);
      if (tile) tile.focus({ preventScroll: true });
    }
  }

  _observeThumbs(videos) {
    const load = (video) => {
      if (video.getAttribute("src") || !video.dataset.src) return;
      video.preload = "metadata";
      video.src = video.dataset.src;
    };
    if (typeof IntersectionObserver !== "function") return videos.forEach(load);
    this._observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        load(entry.target);
        this._observer.unobserve(entry.target);
      }
    }, { root: this._rail, rootMargin: "0px 200px" });
    videos.forEach((video) => this._observer.observe(video));
  }

  _selectedEvent() {
    return this._events.find((event) => event.id === this._selected) || null;
  }

  _snapshotUrl() {
    const hass = this._hass;
    const state = hass && hass.states[this._liveEntity()];
    const picture = state && state.attributes && state.attributes.entity_picture;
    if (!picture) return null;
    return hass.hassUrl ? hass.hassUrl(picture) : picture;
  }

  _renderStage() {
    if (!this._stage) return;
    const language = this._language();
    const event = this._selectedEvent();
    const isLive = this._selected === LIVE_ID;
    this._card.classList.toggle("is-live", isLive && this._liveActive);
    this._card.classList.toggle("notable", Boolean(event && event.analysis.notable));
    this._liveJump.hidden = isLive;

    for (const tile of this._rail.querySelectorAll(".tile")) {
      const on = tile.dataset.id === this._selected;
      tile.setAttribute("aria-current", String(on));
      tile.setAttribute("aria-selected", String(on));
      tile.tabIndex = on ? 0 : -1;
    }

    if (isLive) {
      this._bubble.className = "bubble live";
      this._bubble.textContent = "LIVE";
      if (!this._liveActive) {
        this._clearStage();
        const snap = this._snapshotUrl();
        if (snap) {
          const img = el("img", "snapshot", { alt: `Latest snapshot from ${this._cameraName(this._liveCamera())}` });
          img.src = snap;
          this._stage.append(img);
        }
        const cta = el("div", "stage-cta");
        const btn = el("button", "stage-btn", { type: "button" });
        btn.innerHTML = '<span class="live-dot"></span>';
        btn.append(document.createTextNode(`Watch ${this._cameraName(this._liveCamera())} live`));
        btn.addEventListener("click", () => this._startLive());
        cta.append(btn);
        this._stage.append(cta);
        if (!this._hass) {
          const note = el("div", "stage-note");
          note.textContent = "Live view opens inside Home Assistant.";
          this._stage.append(note);
        }
        this._stage.append(this._overlay(this._cameraName(this._liveCamera()), "Latest snapshot", null));
      }
    } else if (event) {
      this._bubble.className = "bubble";
      this._bubble.textContent = relativeWhen(event.date, language);
      this._stopLive();
      this._clearStage();
      if (event.clipUrl) {
        const video = el("video", "", { controls: "", playsinline: "", preload: "auto" });
        video.muted = true;
        video.autoplay = !reducedMotion();
        if (event.thumbUrl) video.poster = event.thumbUrl;
        video.src = event.clipUrl;
        video.setAttribute("aria-label", `${event.deviceName} recording, ${relativeWhen(event.date, language)}`);
        this._stage.append(video);
      } else {
        const empty = el("div", "stage-empty");
        empty.textContent = "This clip is still downloading from Ring.";
        this._stage.append(empty);
      }
      this._stage.append(this._overlay(event.deviceName, relativeWhen(event.date, language), event.kind === "ding" ? "Ding" : null));
    } else {
      this._bubble.className = "bubble";
      this._bubble.textContent = "";
    }
    this._renderDetails();
  }

  _overlay(camera, when, badgeText, live) {
    const overlay = el("div", "overlay");
    const cam = el("span", "cam");
    cam.textContent = camera;
    const time = el("span", "when");
    time.textContent = when;
    overlay.append(cam, time);
    if (badgeText || live) {
      const badge = el("span", `badge${live ? " live" : ""}`);
      if (live) badge.innerHTML = '<span class="live-dot"></span>';
      badge.append(document.createTextNode(badgeText || "Live"));
      overlay.append(badge);
    }
    return overlay;
  }

  _renderDetails() {
    if (!this._label) return;
    const language = this._language();
    const event = this._selectedEvent();
    this._label.textContent = "";
    if (this._selected === LIVE_ID || !event) {
      const name = this._cameraName(this._liveCamera());
      this._label.textContent = this._liveActive ? `${name} · Live` : name;
      const last = this._events[this._events.length - 1];
      this._meta.textContent = last ? `Last event ${relativeWhen(last.date, language)}` : "";
      this._summary.textContent = last && last.analysis.summary ? `Last: ${last.analysis.summary}` : "Swipe the timeline left to go back in time.";
      this._summary.hidden = false;
      this._more.hidden = true;
      this._detail.hidden = true;
      return;
    }
    const label = eventLabel(event);
    if (event.analysis.notable) {
      const flag = el("span", "flag");
      flag.textContent = "Notable · ";
      this._label.append(flag);
    }
    this._label.append(document.createTextNode(label));
    const who = describeRoles(event.analysis.roles);
    this._meta.textContent = [event.deviceName, relativeWhen(event.date, language), who].filter(Boolean).join(" · ");
    let summary = event.analysis.summary;
    if (!summary && event.quick.headline) {
      // The quick look lands within seconds of the clip; the full pass refines it.
      summary = event.status === "pending" ? `${event.quick.headline}. Watching the full clip...` : event.quick.headline;
    }
    if (!summary) {
      summary = event.status === "excluded" ? "Indoor camera: not sent for AI description."
        : event.status === "pending"
          ? (Date.now() - event.date.getTime() < 15 * 60 * 1000 ? "Describing this clip..." : "Queued for a description.")
          : event.status === "failed" ? "No description for this clip." : "";
    }
    this._summary.textContent = summary;
    this._summary.hidden = !summary;
    const hasDetail = Boolean(event.analysis.detail && event.analysis.detail !== event.analysis.summary);
    this._more.hidden = !hasDetail;
    this._more.textContent = this._detailOpen ? "Less detail" : "More detail";
    this._more.setAttribute("aria-expanded", String(this._detailOpen));
    this._detail.textContent = event.analysis.detail;
    this._detail.hidden = !(hasDetail && this._detailOpen);
  }

  _localise(event) {
    // The server builds URLs for the LAN host; serve them from whichever route this card uses.
    const id = encodeURIComponent(event.id);
    if (event.clipUrl) event.clipUrl = `${this._config.baseUrl}/clips/${id}.mp4`;
    if (event.thumbUrl) event.thumbUrl = `${this._config.baseUrl}/thumbs/${id}.jpg`;
  }

  _clearStage() {
    this._stage.querySelectorAll("video").forEach((video) => { video.pause(); video.removeAttribute("src"); video.load(); });
    this._stage.innerHTML = "";
  }

  /* ----- live ----- */

  async _streamElement() {
    if (customElements.get("ha-camera-stream")) return true;
    try {
      if (window.loadCardHelpers) {
        const helpers = await window.loadCardHelpers();
        const probe = await helpers.createCardElement({ type: "picture-entity", entity: this._liveEntity(), camera_view: "live" });
        if (this._hass) probe.hass = this._hass;
      }
    } catch (_error) {
      /* fall through to the timeout */
    }
    await Promise.race([
      customElements.whenDefined("ha-camera-stream"),
      new Promise((resolve) => window.setTimeout(resolve, 5000)),
    ]);
    return Boolean(customElements.get("ha-camera-stream"));
  }

  async _startLive() {
    if (!this._hass || this._liveActive) return;
    const entity = this._liveEntity();
    const stateObj = this._hass.states[entity];
    if (!stateObj) {
      this._setStatus(`No live entity ${entity} in Home Assistant.`, true);
      return;
    }
    this._liveActive = true;
    this._clearStage();
    const waiting = el("div", "stage-empty");
    waiting.textContent = `Connecting to ${this._cameraName(this._liveCamera())}...`;
    this._stage.append(waiting);
    this._renderDetails();
    const ok = await this._streamElement();
    if (!this._liveActive || this._selected !== LIVE_ID) return;
    this._clearStage();
    if (!ok) {
      this._liveActive = false;
      this._renderStage();
      this._setStatus("Live player didn't load. Open the camera from its entity instead.", true);
      return;
    }
    const host = el("div", "live-host");
    const stream = document.createElement("ha-camera-stream");
    stream.hass = this._hass;
    stream.stateObj = stateObj;
    stream.controls = true;
    stream.muted = true;
    stream.allowExoPlayer = true;
    host.append(stream);
    this._liveStream = stream;
    this._stage.append(host, this._overlay(this._cameraName(this._liveCamera()), "Now", "Live", true));
    this._card.classList.add("is-live");
    this._renderDetails();
  }

  _stopLive() {
    if (this._liveStream) {
      this._liveStream.remove();
      this._liveStream = null;
    }
    this._liveActive = false;
    if (this._card) this._card.classList.remove("is-live");
  }

  /* ----- rail behaviour ----- */

  _tileFor(id) {
    return this._rail.querySelector(`.tile[data-id="${CSS.escape(id)}"]`);
  }

  _centerTile(id, smooth) {
    const tile = this._tileFor(id);
    if (!tile) return;
    const rail = this._rail.getBoundingClientRect();
    const rect = tile.getBoundingClientRect();
    const left = this._rail.scrollLeft + (rect.left - rail.left) - (this._rail.clientWidth - rect.width) / 2;
    const far = Math.abs(left - this._rail.scrollLeft) > this._rail.clientWidth * 2;
    this._rail.scrollTo({ left, behavior: smooth && !far && !reducedMotion() ? "smooth" : "instant" });
    this._select(id);
  }

  _parkAtLive() {
    this._quietUntil = Date.now() + 400;
    this._rail.scrollTo({ left: this._rail.scrollWidth, behavior: "instant" });
    this._select(LIVE_ID, true);
  }

  _goLive(start) {
    this._centerTile(LIVE_ID, true);
    if (start) this._startLive();
  }

  _select(id, force) {
    if (!force && id === this._selected) return;
    const leavingLive = this._selected === LIVE_ID && id !== LIVE_ID;
    this._selected = id;
    this._detailOpen = false;
    if (leavingLive) this._stopLive();
    this._renderStage();
  }

  _onRailScroll() {
    window.clearTimeout(this._settleTimer);
    this._settleTimer = window.setTimeout(() => this._settle(), SETTLE_MS);
    if (this._userScrolled && this._rail.scrollLeft <= NEAR_EDGE_PX) this._loadOlder();
  }

  _settle() {
    const center = this._rail.getBoundingClientRect().left + this._rail.clientWidth / 2;
    let best = null;
    let bestDistance = Infinity;
    for (const tile of this._rail.querySelectorAll(".tile")) {
      const rect = tile.getBoundingClientRect();
      const distance = Math.abs(rect.left + rect.width / 2 - center);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = tile;
      }
    }
    if (!best || !this._userScrolled || Date.now() < (this._quietUntil || 0)) return;
    this._select(best.dataset.id);
    // Like Ring: landing on the right end means "show me now".
    if (best.dataset.id === LIVE_ID && !this._liveActive) this._startLive();
  }

  _onKey(event) {
    const origin = event.composedPath()[0];
    const fromTimeline = origin === this._card || origin === this._rail
      || (origin instanceof Element && origin.classList.contains("tile"));
    if (!fromTimeline) return;
    const ids = [...this._rail.querySelectorAll(".tile")].map((tile) => tile.dataset.id);
    const index = ids.indexOf(this._selected);
    let next = null;
    if (event.key === "ArrowLeft") next = ids[Math.max(0, index - 1)];
    else if (event.key === "ArrowRight") next = ids[Math.min(ids.length - 1, index + 1)];
    else if (event.key === "End") next = LIVE_ID;
    else if (event.key === "Home") next = ids[0];
    if (!next) return;
    event.preventDefault();
    this._userScrolled = true;
    this._centerTile(next, true);
    const tile = this._tileFor(next);
    if (tile) tile.focus({ preventScroll: true });
    if (next === LIVE_ID && !this._liveActive) this._startLive();
  }

  _setStatus(message, isError) {
    if (!this._status) return;
    this._status.textContent = message || "";
    this._status.classList.toggle("error", Boolean(isError));
  }
}

if (!customElements.get("ring-events-card")) customElements.define("ring-events-card", RingEventsCard);

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "ring-events-card")) {
  window.customCards.push({
    type: "ring-events-card",
    name: "Ring timeline",
    description: "Ring-style live view and recorded history on one timeline, labelled by Gemini.",
    preview: false,
    documentationURL: "https://hass.iacob.uk/ring-events/",
  });
}
