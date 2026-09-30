#!/usr/bin/env python3
"""Prometheus exporter for the CLIProxyAPI v8 usage queue.

Record contract: CLIProxyAPI v8.0.5
  internal/api/handlers/management/usage.go   GET /v8/management/observability/usage/queue?count=N
  internal/redisqueue/plugin.go               queuedUsageDetail JSON (one object per request)
  internal/redisqueue/queue.go                in-memory queue, 60s default retention

The endpoint pops records destructively and has no acknowledgement. A crash
between the HTTP response and the SQLite commit loses that batch; that window
is kept to one batch by committing after every pop. Run exactly one replica.

Only provider, model, fail.status_code, latency_ms and tokens.* are read.
api_key, auth_index, source, access_token_sha256, client IPs, user_agent,
session/request ids, fail.body and response_headers are never persisted,
labelled or logged.
"""

import json
import logging
import os
import re
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

QUEUE_PATH = "/v8/management/observability/usage/queue"

TOKEN_TYPES = {
    "input_tokens": "input",
    "output_tokens": "output",
    "reasoning_tokens": "reasoning",
    "cached_tokens": "cached",
    "cache_read_tokens": "cache_read",
    "cache_creation_tokens": "cache_creation",
    "total_tokens": "total",
}
# Record fields whose values must never surface as a label value.
SECRET_FIELDS = ("api_key", "auth_index", "source", "access_token_sha256")
INVALID_REASONS = ("not_object", "bad_provider", "bad_model", "bad_status", "bad_tokens", "bad_latency", "secret_label")
OVERFLOW = "_overflow_"
MAX_LABEL_LEN = 96
MAX_TOKENS_PER_RECORD = 10**12
MAX_LATENCY_MS = 24 * 3600 * 1000
LABEL_RE = re.compile(r"[^A-Za-z0-9._:/@+\-]")

log = logging.getLogger("cliproxy-exporter")


def env_int(name, default, lo, hi):
    raw = os.environ.get(name, "").strip()
    value = int(raw) if raw else default
    return min(max(value, lo), hi)


def validate_base_url(raw):
    parsed = urllib.parse.urlsplit(raw.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("CLIPROXY_URL must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("CLIPROXY_URL must not carry credentials, query or fragment")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def validate_key(raw):
    key = (raw or "").strip()
    if not key or any(c in key for c in "\r\n\0"):
        raise ValueError("CLIPROXY_MANAGEMENT_KEY is empty or contains control characters")
    return key


class FetchError(Exception):
    """Carries only a short, secret-free description of the failure."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Following a redirect would resend Authorization to wherever it points.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FetchError(f"redirect {code} refused", status=code)


class QueueClient:
    def __init__(self, base_url, key, timeout, max_bytes):
        self.base_url = validate_base_url(base_url)
        self._key = validate_key(key)
        self.timeout = timeout
        self.max_bytes = max_bytes
        # Empty ProxyHandler: never route the management key through env proxies.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def __repr__(self):
        return f"QueueClient({self.base_url!r})"

    def pop(self, count):
        req = urllib.request.Request(
            f"{self.base_url}{QUEUE_PATH}?count={int(count)}",
            headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read(self.max_bytes + 1)
        except FetchError:
            raise
        except urllib.error.HTTPError as exc:
            exc.close()
            raise FetchError(f"http {exc.code}", status=exc.code) from None
        except (urllib.error.URLError, OSError) as exc:
            raise FetchError(f"transport {type(exc).__name__}") from None
        if len(body) > self.max_bytes:
            raise FetchError("response exceeds size limit")
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            raise FetchError("response is not JSON") from None
        if not isinstance(data, list):
            raise FetchError("response is not a JSON array")
        return data


def clean_label(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return LABEL_RE.sub("_", value[:MAX_LABEL_LEN])


def is_count(value, upper):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= upper


def parse_record(rec):
    """Return (sample, None) for a valid record or (None, reason)."""
    if not isinstance(rec, dict):
        return None, "not_object"
    provider = clean_label(rec.get("provider"))
    if provider is None:
        return None, "bad_provider"
    model = clean_label(rec.get("model"))
    if model is None:
        return None, "bad_model"
    secrets = {v.strip() for f in SECRET_FIELDS if isinstance(v := rec.get(f), str) and v.strip()}
    if rec.get("provider", "").strip() in secrets or rec.get("model", "").strip() in secrets:
        return None, "secret_label"

    fail = rec.get("fail")
    failed = rec.get("failed")
    if not isinstance(failed, bool) or not isinstance(fail, dict):
        return None, "bad_status"
    code = fail.get("status_code")
    if not is_count(code, 599) or code < 100:
        return None, "bad_status"

    tokens = rec.get("tokens")
    if not isinstance(tokens, dict):
        return None, "bad_tokens"
    counts = {}
    for field, kind in TOKEN_TYPES.items():
        value = tokens.get(field, 0)
        if not is_count(value, MAX_TOKENS_PER_RECORD):
            return None, "bad_tokens"
        counts[kind] = value

    latency = rec.get("latency_ms")
    if not is_count(latency, MAX_LATENCY_MS):
        return None, "bad_latency"

    return {
        "provider": provider,
        "model": model,
        "status": str(code),
        "tokens": counts,
        "latency_s": latency / 1000.0,
    }, None


SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (provider TEXT, model TEXT, status TEXT, value INTEGER NOT NULL,
  PRIMARY KEY (provider, model, status));
CREATE TABLE IF NOT EXISTS tokens (provider TEXT, model TEXT, type TEXT, value INTEGER NOT NULL,
  PRIMARY KEY (provider, model, type));
CREATE TABLE IF NOT EXISTS duration (provider TEXT, model TEXT, sum REAL NOT NULL, count INTEGER NOT NULL,
  PRIMARY KEY (provider, model));
CREATE TABLE IF NOT EXISTS invalid (reason TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS overflow (id INTEGER PRIMARY KEY CHECK (id = 1), value INTEGER NOT NULL);
"""


class Store:
    def __init__(self, path, max_pairs):
        self.max_pairs = max_pairs
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.pairs = {tuple(r) for r in self.db.execute("SELECT DISTINCT provider, model FROM requests")}

    def close(self):
        self.db.close()

    def _pair(self, provider, model, pending):
        pair = (provider, model)
        if pair in self.pairs or pair in pending:
            return pair, False
        if len(self.pairs) + len(pending) >= self.max_pairs:
            return (OVERFLOW, OVERFLOW), True
        pending.add(pair)
        return pair, False

    def apply(self, records):
        """Persist one popped batch in a single transaction. Returns (valid, invalid)."""
        valid = invalid = overflowed = 0
        pending = set()
        cur = self.db.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            for rec in records:
                sample, reason = parse_record(rec)
                if sample is None:
                    invalid += 1
                    cur.execute(
                        "INSERT INTO invalid VALUES (?, 1) ON CONFLICT(reason) DO UPDATE SET value = value + 1",
                        (reason,),
                    )
                    continue
                (p, m), over = self._pair(sample["provider"], sample["model"], pending)
                overflowed += over
                valid += 1
                cur.execute(
                    "INSERT INTO requests VALUES (?, ?, ?, 1) ON CONFLICT DO UPDATE SET value = value + 1",
                    (p, m, sample["status"]),
                )
                for kind, n in sample["tokens"].items():
                    if n:
                        cur.execute(
                            "INSERT INTO tokens VALUES (?, ?, ?, ?) ON CONFLICT DO UPDATE SET value = value + excluded.value",
                            (p, m, kind, n),
                        )
                cur.execute(
                    "INSERT INTO duration VALUES (?, ?, ?, 1) ON CONFLICT DO UPDATE "
                    "SET sum = sum + excluded.sum, count = count + 1",
                    (p, m, sample["latency_s"]),
                )
            if overflowed:
                cur.execute(
                    "INSERT INTO overflow VALUES (1, ?) ON CONFLICT DO UPDATE SET value = value + excluded.value",
                    (overflowed,),
                )
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        self.pairs |= pending
        return valid, invalid

    def snapshot(self):
        q = self.db.execute
        return {
            "requests": q("SELECT provider, model, status, value FROM requests ORDER BY 1, 2, 3").fetchall(),
            "tokens": q("SELECT provider, model, type, value FROM tokens ORDER BY 1, 2, 3").fetchall(),
            "duration": q("SELECT provider, model, sum, count FROM duration ORDER BY 1, 2").fetchall(),
            "invalid": dict(q("SELECT reason, value FROM invalid").fetchall()),
            "overflow": (q("SELECT value FROM overflow").fetchone() or (0,))[0],
        }


def esc(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def labels(**kv):
    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in kv.items()) + "}"


def render_counters(snap):
    out = [
        "# HELP cliproxy_requests_total Requests seen in the CLIProxy usage queue.",
        "# TYPE cliproxy_requests_total counter",
    ]
    out += [f"cliproxy_requests_total{labels(provider=p, model=m, status=s)} {v}" for p, m, s, v in snap["requests"]]
    out += ["# HELP cliproxy_tokens_total Tokens reported by CLIProxy usage records.", "# TYPE cliproxy_tokens_total counter"]
    out += [f"cliproxy_tokens_total{labels(provider=p, model=m, type=t)} {v}" for p, m, t, v in snap["tokens"]]
    out += [
        "# HELP cliproxy_request_duration_seconds Upstream request latency (latency_ms) from CLIProxy usage records.",
        "# TYPE cliproxy_request_duration_seconds summary",
    ]
    for p, m, total, count in snap["duration"]:
        lb = labels(provider=p, model=m)
        out.append(f"cliproxy_request_duration_seconds_sum{lb} {total!r}")
        out.append(f"cliproxy_request_duration_seconds_count{lb} {count}")
    out += [
        "# HELP cliproxy_exporter_invalid_records_total Queue records dropped as malformed.",
        "# TYPE cliproxy_exporter_invalid_records_total counter",
    ]
    out += [
        f"cliproxy_exporter_invalid_records_total{labels(reason=r)} {snap['invalid'].get(r, 0)}" for r in INVALID_REASONS
    ]
    out += [
        "# HELP cliproxy_exporter_label_overflow_total Records folded into the _overflow_ series by the cardinality cap.",
        "# TYPE cliproxy_exporter_label_overflow_total counter",
        f"cliproxy_exporter_label_overflow_total {snap['overflow']}",
    ]
    return "\n".join(out) + "\n"


class State:
    """Everything /metrics and /healthz need, readable without touching upstream."""

    def __init__(self):
        self.lock = threading.Lock()
        self.start_time = time.time()
        self.counters_text = ""
        self.up = 0
        self.last_success = 0.0
        self.heartbeat = time.time()
        self.polls = {"success": 0, "failure": 0}
        self.records = 0

    def render(self):
        with self.lock:
            lines = [
                "# HELP cliproxy_exporter_up Whether the last queue poll succeeded.",
                "# TYPE cliproxy_exporter_up gauge",
                f"cliproxy_exporter_up {self.up}",
                "# HELP cliproxy_exporter_last_success_timestamp_seconds Unix time of the last successful poll.",
                "# TYPE cliproxy_exporter_last_success_timestamp_seconds gauge",
                f"cliproxy_exporter_last_success_timestamp_seconds {self.last_success!r}",
                "# HELP cliproxy_exporter_start_time_seconds Unix time the exporter process started.",
                "# TYPE cliproxy_exporter_start_time_seconds gauge",
                f"cliproxy_exporter_start_time_seconds {self.start_time!r}",
                "# HELP cliproxy_exporter_polls_total Queue polls since process start.",
                "# TYPE cliproxy_exporter_polls_total counter",
                *(f"cliproxy_exporter_polls_total{labels(result=k)} {v}" for k, v in self.polls.items()),
                "# HELP cliproxy_exporter_records_total Valid records persisted since process start.",
                "# TYPE cliproxy_exporter_records_total counter",
                f"cliproxy_exporter_records_total {self.records}",
            ]
            return (self.counters_text + "\n".join(lines) + "\n").encode()


class Poller:
    def __init__(self, client, store, state, batch, max_batches):
        self.client = client
        self.store = store
        self.state = state
        self.batch = batch
        self.max_batches = max_batches
        self.refresh()

    def refresh(self):
        text = render_counters(self.store.snapshot())
        with self.state.lock:
            self.state.counters_text = text

    def poll_once(self, stop=None):
        """Drain up to max_batches pops. Returns (ok, http_status_on_failure)."""
        committed = False
        try:
            for _ in range(self.max_batches):
                if stop is not None and stop.is_set():
                    break
                records = self.client.pop(self.batch)
                valid, invalid = self.store.apply(records)
                committed = True
                with self.state.lock:
                    self.state.records += valid
                if invalid:
                    log.warning("dropped %d malformed usage records", invalid)
                if len(records) < self.batch:
                    break
        except FetchError as exc:
            log.warning("queue poll failed: %s", exc)
            return self._finish(False, committed), exc.status
        except Exception as exc:
            # Type only: exception text could echo record content.
            log.error("poll failed: %s", type(exc).__name__)
            return self._finish(False, committed), None
        return self._finish(True, committed), None

    def _finish(self, ok, committed):
        if committed:
            self.refresh()
        with self.state.lock:
            now = time.time()
            self.state.heartbeat = now
            self.state.up = int(ok)
            self.state.polls["success" if ok else "failure"] += 1
            if ok:
                self.state.last_success = now
        return ok

    def run(self, stop, interval):
        failures = 0
        while not stop.is_set():
            ok, status = self.poll_once(stop)
            failures = 0 if ok else failures + 1
            delay = interval
            if status in (401, 403):
                # CLIProxy bans an IP for 30 minutes after 5 bad keys; do not burn attempts.
                delay = 300
            elif failures:
                delay = min(interval * 2 ** min(failures, 5), 120)
            # Keep the heartbeat fresh during long backoff so liveness stays green.
            deadline = time.monotonic() + delay
            while not stop.wait(min(5, max(0, deadline - time.monotonic()))):
                with self.state.lock:
                    self.state.heartbeat = time.time()
                if time.monotonic() >= deadline:
                    break


def make_handler(state, stale_after):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cliproxy-exporter"
        sys_version = ""

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/metrics":
                self._send(200, state.render(), "text/plain; version=0.0.4; charset=utf-8")
            elif path == "/healthz":
                with state.lock:
                    body = {
                        "poller_alive": time.time() - state.heartbeat < stale_after,
                        "up": state.up,
                        "last_success": state.last_success,
                        "start_time": state.start_time,
                        "polls": dict(state.polls),
                    }
                self._send(200 if body["poller_alive"] else 503, json.dumps(body).encode(), "application/json")
            else:
                self._send(404, b"not found\n", "text/plain")

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    try:
        client = QueueClient(
            os.environ.get("CLIPROXY_URL", "http://cliproxy.code.svc.cluster.local:8317"),
            os.environ.get("CLIPROXY_MANAGEMENT_KEY"),
            timeout=env_int("HTTP_TIMEOUT_SECONDS", 5, 1, 10),
            max_bytes=env_int("MAX_RESPONSE_BYTES", 16 * 1024 * 1024, 64 * 1024, 64 * 1024 * 1024),
        )
    except ValueError as exc:
        log.error("config error: %s", exc)
        return 2
    os.environ.pop("CLIPROXY_MANAGEMENT_KEY", None)

    interval = env_int("POLL_SECONDS", 10, 1, 15)
    store = Store(os.environ.get("STATE_DB", "/data/cliproxy-metrics.db"), env_int("MAX_SERIES_PAIRS", 200, 1, 2000))
    state = State()
    poller = Poller(client, store, state, env_int("BATCH_COUNT", 200, 1, 1000), env_int("MAX_BATCHES_PER_POLL", 20, 1, 100))

    port = env_int("LISTEN_PORT", 9100, 1, 65535)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(state, stale_after=120))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("polling %s every %ds, serving :%d", client.base_url, interval, port)
    poller.run(stop, interval)
    server.shutdown()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
