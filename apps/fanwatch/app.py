#!/usr/bin/env python3
"""HPE iLO 4 fan-cause dashboard - pure Python stdlib, single file.

Polls each iLO 4 BMC's Redfish API and shows, at a glance, what is driving the
chassis fans - which temperature sensor or event is making fans spin up - plus a
live IML event log. The infamous bogus "08-HD Max" virtual sensor (from non-HP
SSDs) is shown but labelled "neutralized (offset)" and excluded from drivers.

Run:
    ILO_TARGETS="DL360=192.168.1.175,DL380=192.168.1.180" \\
    ILO_USER=Administrator ILO_PASS=yourpass \\
    python3 app.py

No pip installs. Designed for python:3.12-slim.

Env vars (read at startup):
    ILO_TARGETS        comma list "LABEL=host,LABEL=host" (required)
    ILO_USER           iLO username (default "Administrator")
    ILO_PASS           shared iLO password
    ILO_PASS_<LABEL>   per-host password override (e.g. ILO_PASS_DL360)
    PORT               HTTP listen port (default 8080)
    POLL_SECONDS       thermal poll cadence (default 15)
    IML_POLL_SECONDS   IML event-log poll cadence (default 180)
    HISTORY_POINTS     samples kept per target (default 480 ~= 2h at 15s)
"""

import base64
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def _parse_targets(raw):
    targets = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        label, host = chunk.split("=", 1)
        label, host = label.strip(), host.strip()
        if label and host:
            targets.append((label, host))
    return targets


ILO_USER = os.environ.get("ILO_USER", "Administrator")
ILO_PASS = os.environ.get("ILO_PASS", "")
PORT = int(os.environ.get("PORT", "8080"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "15"))
IML_POLL_SECONDS = int(os.environ.get("IML_POLL_SECONDS", "180"))
HISTORY_POINTS = int(os.environ.get("HISTORY_POINTS", "480"))
TARGETS = _parse_targets(os.environ.get("ILO_TARGETS", ""))

DRIVER_THRESHOLD = 0.6        # score above this = "likely driver"
IML_FETCH_COUNT = 15          # only fetch the last N IML members
IML_SLEEP = 0.3               # polite delay between member fetches
HTTP_TIMEOUT = 10
MAX_RETRIES = 3

_SSL_CTX = ssl._create_unverified_context()


def _pass_for(label):
    return os.environ.get("ILO_PASS_" + label, ILO_PASS)


# --------------------------------------------------------------------------- #
# Shared state
# --------------------------------------------------------------------------- #

_lock = threading.Lock()

# state[label] = {
#   host, online, maxfan, fans, drivers, temps, events,
#   history (deque), last_thermal_ok, last_iml_ok
# }
_state = {}
for _label, _host in TARGETS:
    _state[_label] = {
        "host": _host,
        "online": False,
        "maxfan": 0,
        "fans": [],
        "drivers": [],
        "temps": [],
        "events": [],
        "history": deque(maxlen=HISTORY_POINTS),
        "last_thermal_ok": 0,
        "last_iml_ok": 0,
    }


# --------------------------------------------------------------------------- #
# Redfish HTTP
# --------------------------------------------------------------------------- #

def _redfish_get(host, path, label):
    """GET a Redfish resource. Returns parsed JSON or raises."""
    url = "https://%s%s" % (host, path)
    auth = base64.b64encode(
        ("%s:%s" % (ILO_USER, _pass_for(label))).encode("utf-8")
    ).decode("ascii")
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " + auth)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8", "replace"))


def _redfish_get_retry(host, path, label, retries=MAX_RETRIES):
    last = None
    for attempt in range(retries):
        try:
            return _redfish_get(host, path, label)
        except Exception as exc:  # noqa: BLE001 - never crash the poller
            last = exc
            time.sleep(0.5 * (attempt + 1))
    raise last if last else RuntimeError("redfish get failed")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _fan_reading(fan):
    val = fan.get("Reading")
    if val is None:
        val = fan.get("CurrentReading")
    try:
        return int(round(float(val)))
    except (TypeError, ValueError):
        return None


def _parse_thermal(doc):
    """Return (maxfan, fans, temps) from a Thermal document."""
    fans = []
    for fan in doc.get("Fans", []) or []:
        r = _fan_reading(fan)
        if r is not None:
            fans.append(max(0, min(100, r)))
    maxfan = max(fans) if fans else 0

    temps = []
    for t in doc.get("Temperatures", []) or []:
        name = t.get("Name", "?")
        c = t.get("ReadingCelsius")
        crit = t.get("UpperThresholdCritical")
        state = (t.get("Status") or {}).get("State", "Unknown")
        try:
            c = int(round(float(c))) if c is not None else None
        except (TypeError, ValueError):
            c = None
        try:
            crit = int(round(float(crit))) if crit is not None else None
        except (TypeError, ValueError):
            crit = None
        temps.append({"name": name, "c": c, "crit": crit, "state": state})
    return maxfan, fans, temps


def _is_hd_max(name):
    return "hd max" in (name or "").lower()


def _compute_drivers(temps):
    """Rank Enabled sensors by reading/crit. Exclude HD Max. Top scorers
    above DRIVER_THRESHOLD are likely drivers."""
    scored = []
    for t in temps:
        if t["state"] != "Enabled":
            continue
        if _is_hd_max(t["name"]):
            continue
        c, crit = t["c"], t["crit"]
        if c is None or crit is None or crit <= 0:
            continue
        score = c / float(crit)
        scored.append({"name": t["name"], "c": c, "crit": crit, "score": round(score, 3)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    drivers = [s for s in scored if s["score"] >= DRIVER_THRESHOLD][:3]
    return drivers


# --------------------------------------------------------------------------- #
# Pollers
# --------------------------------------------------------------------------- #

def _thermal_poller(label, host):
    path = "/redfish/v1/Chassis/1/Thermal/"
    while True:
        try:
            doc = _redfish_get_retry(host, path, label)
            maxfan, fans, temps = _parse_thermal(doc)
            drivers = _compute_drivers(temps)
            now = time.time()
            with _lock:
                st = _state[label]
                st["online"] = True
                st["maxfan"] = maxfan
                st["fans"] = fans
                st["temps"] = temps
                st["drivers"] = drivers
                st["last_thermal_ok"] = now
                st["history"].append({"t": int(now), "maxfan": maxfan})
        except Exception:  # noqa: BLE001
            with _lock:
                _state[label]["online"] = False
        time.sleep(POLL_SECONDS)


def _parse_iml_entry(doc):
    return {
        "created": doc.get("Created", ""),
        "severity": doc.get("Severity", "OK"),
        "message": doc.get("Message", ""),
    }


def _iml_poller(label, host):
    index_path = "/redfish/v1/Systems/1/LogServices/IML/Entries/"
    while True:
        try:
            index = _redfish_get_retry(host, index_path, label)
            members = index.get("Members", []) or []
            urls = []
            for m in members:
                u = m.get("@odata.id")
                if u:
                    urls.append(u)
            # last N references only
            urls = urls[-IML_FETCH_COUNT:]
            events = []
            for u in urls:
                try:
                    entry = _redfish_get_retry(host, u, label, retries=2)
                    events.append(_parse_iml_entry(entry))
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(IML_SLEEP)
            # newest first
            events.reverse()
            with _lock:
                _state[label]["events"] = events
                _state[label]["last_iml_ok"] = time.time()
        except Exception:  # noqa: BLE001
            pass  # keep last-known events
        time.sleep(IML_POLL_SECONDS)


def _start_pollers():
    for label, host in TARGETS:
        threading.Thread(target=_thermal_poller, args=(label, host),
                         daemon=True, name="thermal-" + label).start()
        threading.Thread(target=_iml_poller, args=(label, host),
                         daemon=True, name="iml-" + label).start()


# --------------------------------------------------------------------------- #
# State snapshot for the API
# --------------------------------------------------------------------------- #

def _snapshot():
    with _lock:
        out = {"ts": int(time.time()), "targets": {}}
        for label, st in _state.items():
            out["targets"][label] = {
                "host": st["host"],
                "online": st["online"],
                "maxfan": st["maxfan"],
                "fans": list(st["fans"]),
                "drivers": list(st["drivers"]),
                "temps": list(st["temps"]),
                "history": list(st["history"]),
                "events": list(st["events"]),
            }
    return out


# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>fanwatch - iLO fan-cause dashboard</title>
<style>
  :root {
    --bg: #0c0e12;
    --panel: #14171d;
    --panel2: #1a1e26;
    --line: #262b35;
    --fg: #d7dce4;
    --muted: #8a93a3;
    --green: #41c971;
    --amber: #e3b341;
    --red: #e5534b;
    --grey: #5a6373;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
      Helvetica, Arial, sans-serif;
    font-size: 14px; line-height: 1.5;
  }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 24px 18px 60px; }
  header.top { display: flex; align-items: baseline; gap: 12px; margin-bottom: 6px; }
  header.top h1 { font-size: 18px; font-weight: 600; margin: 0; letter-spacing: .2px; }
  header.top .sub { color: var(--muted); font-size: 12px; }
  .updated { color: var(--muted); font-size: 12px; margin-bottom: 18px; }
  .grid { display: grid; grid-template-columns: 1fr; gap: 18px; }
  @media (min-width: 760px) { .grid { grid-template-columns: 1fr 1fr; } }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; padding: 16px 16px 14px; overflow: hidden;
  }
  .chead { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
  .chead .label { font-size: 15px; font-weight: 600; }
  .chead .host { color: var(--muted); font-size: 12px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--grey);
    margin-left: auto; flex: none; box-shadow: 0 0 0 3px rgba(0,0,0,.25); }
  .dot.on { background: var(--green); }
  .dot.off { background: var(--red); }
  .fanrow { display: flex; align-items: flex-end; gap: 14px; margin-bottom: 10px; }
  .bignum { font-size: 40px; font-weight: 700; line-height: 1; font-variant-numeric: tabular-nums; }
  .bignum .pct { font-size: 18px; font-weight: 500; color: var(--muted); margin-left: 2px; }
  .bignum.green { color: var(--green); } .bignum.amber { color: var(--amber); } .bignum.red { color: var(--red); }
  .spark { flex: 1; height: 42px; }
  .caption { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .5px; }
  .bumping { margin: 4px 0 14px; padding: 9px 11px; background: var(--panel2);
    border: 1px solid var(--line); border-radius: 7px; font-size: 13px; }
  .bumping b { font-weight: 600; }
  .bumping.calm { color: var(--muted); }
  .fanbars { display: flex; gap: 3px; align-items: flex-end; height: 26px; margin: 4px 0 14px; }
  .fanbars .fb { width: 8px; background: var(--grey); border-radius: 2px 2px 0 0; min-height: 2px; }
  table.sensors { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  table.sensors th { text-align: left; color: var(--muted); font-weight: 500;
    font-size: 11px; text-transform: uppercase; letter-spacing: .4px;
    padding: 0 8px 6px 0; border-bottom: 1px solid var(--line); }
  table.sensors td { padding: 5px 8px 5px 0; border-bottom: 1px solid var(--panel2);
    vertical-align: middle; }
  table.sensors td.name { white-space: nowrap; max-width: 180px; overflow: hidden;
    text-overflow: ellipsis; }
  .num { font-variant-numeric: tabular-nums; text-align: right; }
  .bar { height: 7px; width: 90px; background: var(--panel2); border-radius: 4px; overflow: hidden; }
  .bar > i { display: block; height: 100%; background: var(--green); border-radius: 4px; }
  .tag { font-size: 10px; padding: 1px 6px; border-radius: 5px; border: 1px solid var(--line);
    color: var(--muted); white-space: nowrap; }
  .tag.neutral { color: var(--amber); border-color: rgba(227,179,65,.4); }
  .tag.absent { color: var(--grey); }
  .secthead { color: var(--muted); font-size: 11px; text-transform: uppercase;
    letter-spacing: .5px; margin: 16px 0 7px; }
  .events { display: flex; flex-direction: column; gap: 5px; max-height: 220px;
    overflow-y: auto; }
  .ev { display: flex; gap: 8px; align-items: baseline; font-size: 12px;
    padding: 4px 8px; background: var(--panel2); border-radius: 6px;
    border-left: 3px solid var(--grey); }
  .ev.crit { border-left-color: var(--red); }
  .ev.warn { border-left-color: var(--amber); }
  .ev.ok { border-left-color: var(--grey); }
  .ev .when { color: var(--muted); font-size: 11px; white-space: nowrap; flex: none; }
  .ev .msg { flex: 1; }
  .ev .sev { font-size: 10px; text-transform: uppercase; letter-spacing: .4px; flex: none; }
  .ev.crit .sev { color: var(--red); } .ev.warn .sev { color: var(--amber); } .ev.ok .sev { color: var(--muted); }
  .empty { color: var(--muted); font-size: 12px; padding: 6px 0; }
  .offline-note { color: var(--amber); font-size: 11px; }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>fanwatch</h1>
    <span class="sub">iLO 4 fan-cause dashboard</span>
  </header>
  <div class="updated" id="updated">loading...</div>
  <div class="grid" id="grid"></div>
</div>
<script>
function fanClass(v){ return v > 70 ? "red" : (v >= 40 ? "amber" : "green"); }
function barColor(score){ return score > 0.85 ? "var(--red)" : (score >= 0.6 ? "var(--amber)" : "var(--green)"); }
function esc(s){ return String(s == null ? "" : s).replace(/[&<>"]/g, function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]; }); }

function sparkline(hist){
  if(!hist || hist.length < 2){ return '<svg class="spark"></svg>'; }
  var w = 100, h = 42, n = hist.length;
  var pts = hist.map(function(p, i){
    var x = (i/(n-1))*w;
    var y = h - (Math.max(0, Math.min(100, p.maxfan))/100)*h;
    return x.toFixed(1)+","+y.toFixed(1);
  });
  var last = hist[hist.length-1].maxfan;
  var col = last > 70 ? "var(--red)" : (last >= 40 ? "var(--amber)" : "var(--green)");
  var area = "0,"+h+" "+pts.join(" ")+" "+w+","+h;
  return '<svg class="spark" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none">'
    + '<polygon points="'+area+'" fill="'+col+'" opacity="0.10"/>'
    + '<polyline points="'+pts.join(" ")+'" fill="none" stroke="'+col+'" stroke-width="1.6" '
    + 'vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>';
}

function fanbars(fans){
  if(!fans || !fans.length){ return ''; }
  return '<div class="fanbars">' + fans.map(function(v){
    var c = v > 70 ? "var(--red)" : (v >= 40 ? "var(--amber)" : "var(--green)");
    var hgt = Math.max(2, (v/100)*26);
    return '<div class="fb" title="'+v+'%" style="height:'+hgt.toFixed(1)+'px;background:'+c+'"></div>';
  }).join("") + '</div>';
}

function bumpingLine(t){
  if(!t.online){
    return '<div class="bumping calm">offline - showing last-known data</div>';
  }
  if(t.drivers && t.drivers.length){
    var d = t.drivers[0];
    var pct = Math.round(d.score*100);
    return '<div class="bumping"><b>'+esc(d.name)+'</b> '+d.c+'C ('
      + pct+'% of '+d.crit+'C crit)</div>';
  }
  return '<div class="bumping calm">nothing - fans tracking baseline</div>';
}

function sensorRows(temps){
  // sort by score desc (computed reading/crit); absent + no-data sink to bottom
  var rows = temps.map(function(t){
    var score = (t.c != null && t.crit && t.crit > 0) ? t.c/t.crit : -1;
    var hd = /hd max/i.test(t.name || "");
    return {t: t, score: score, hd: hd};
  });
  rows.sort(function(a, b){ return b.score - a.score; });
  if(!rows.length){ return '<tr><td colspan="4" class="empty">no sensor data</td></tr>'; }
  return rows.map(function(r){
    var t = r.t;
    var tag = "";
    if(r.hd){ tag = '<span class="tag neutral">neutralized (offset)</span>'; }
    else if(t.state === "Absent"){ tag = '<span class="tag absent">absent</span>'; }
    var barHtml = "";
    if(r.score >= 0 && !r.hd){
      var pct = Math.max(0, Math.min(100, r.score*100));
      barHtml = '<div class="bar"><i style="width:'+pct.toFixed(0)+'%;background:'
        + barColor(r.score)+'"></i></div>';
    }
    return '<tr>'
      + '<td class="name" title="'+esc(t.name)+'">'+esc(t.name)+'</td>'
      + '<td class="num">'+(t.c == null ? "-" : t.c+"C")+'</td>'
      + '<td class="num">'+(t.crit == null ? "-" : t.crit+"C")+'</td>'
      + '<td>'+barHtml+' '+tag+'</td>'
      + '</tr>';
  }).join("");
}

function eventRows(events){
  if(!events || !events.length){ return '<div class="empty">no recent events</div>'; }
  return events.map(function(e){
    var sev = (e.severity || "OK").toLowerCase();
    var cls = sev.indexOf("crit") === 0 ? "crit" : (sev.indexOf("warn") === 0 ? "warn" : "ok");
    return '<div class="ev '+cls+'">'
      + '<span class="when">'+esc(e.created)+'</span>'
      + '<span class="msg">'+esc(e.message)+'</span>'
      + '<span class="sev">'+esc(e.severity || "OK")+'</span>'
      + '</div>';
  }).join("");
}

function card(label, t){
  var fc = fanClass(t.maxfan);
  return '<div class="card">'
    + '<div class="chead">'
    +   '<span class="label">'+esc(label)+'</span>'
    +   '<span class="host">'+esc(t.host)+'</span>'
    +   '<span class="dot '+(t.online ? "on" : "off")+'"></span>'
    + '</div>'
    + '<div class="fanrow">'
    +   '<div><div class="caption">max fan</div>'
    +     '<div class="bignum '+fc+'">'+t.maxfan+'<span class="pct">%</span></div></div>'
    +   sparkline(t.history)
    + '</div>'
    + fanbars(t.fans)
    + bumpingLine(t)
    + '<table class="sensors"><thead><tr>'
    +   '<th>sensor</th><th class="num">read</th><th class="num">crit</th><th>load</th>'
    + '</tr></thead><tbody>'+sensorRows(t.temps)+'</tbody></table>'
    + '<div class="secthead">event log (IML)</div>'
    + '<div class="events">'+eventRows(t.events)+'</div>'
    + '</div>';
}

function render(data){
  var grid = document.getElementById("grid");
  var labels = Object.keys(data.targets);
  if(!labels.length){
    grid.innerHTML = '<div class="empty">no targets configured - set ILO_TARGETS</div>';
  } else {
    grid.innerHTML = labels.map(function(l){ return card(l, data.targets[l]); }).join("");
  }
  var d = new Date(data.ts*1000);
  document.getElementById("updated").textContent = "updated " + d.toLocaleTimeString();
}

function tick(){
  fetch("/api/state", {cache: "no-store"})
    .then(function(r){ return r.json(); })
    .then(render)
    .catch(function(){
      document.getElementById("updated").textContent = "fetch failed - retrying";
    });
}
tick();
setInterval(tick, 10000);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "fanwatch/1.0"

    def log_message(self, *args):  # quiet
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path == "/" or self.path.startswith("/index"):
                self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            elif self.path.startswith("/api/state"):
                self._send(200, json.dumps(_snapshot()), "application/json")
            elif self.path == "/healthz":
                self._send(200, "ok", "text/plain")
            else:
                self._send(404, "not found", "text/plain")
        except Exception:  # noqa: BLE001 - never 500 the dashboard
            try:
                self._send(503, json.dumps({"error": "internal"}), "application/json")
            except Exception:  # noqa: BLE001
                pass

    do_HEAD = do_GET


def main():
    _start_pollers()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    labels = ", ".join("%s=%s" % (l, h) for l, h in TARGETS) or "(none)"
    print("fanwatch listening on :%d  targets: %s" % (PORT, labels), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
