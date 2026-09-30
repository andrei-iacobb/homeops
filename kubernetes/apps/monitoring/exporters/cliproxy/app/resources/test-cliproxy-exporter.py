#!/usr/bin/env python3
"""Stdlib tests for cliproxy-exporter.py. Run: python3 test-cliproxy-exporter.py"""

import importlib.util
import io
import json
import logging
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("exporter", os.path.join(HERE, "cliproxy-exporter.py"))
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)

KEY = "mgmt-key-SHOULD-NOT-LEAK-7f3a"
API_KEY = "sk-client-SHOULD-NOT-LEAK-91bc"
AUTH_INDEX = "auth-idx-SHOULD-NOT-LEAK-55"
SOURCE = "someone@example.invalid"
SECRETS = (KEY, API_KEY, AUTH_INDEX, SOURCE, "10.9.8.7", "tok-hash-SHOULD-NOT-LEAK", "upstream error body")


def official_record(provider="codex", model="gpt-5-codex", failed=False, status=200, latency_ms=1500, **tokens):
    """Shape of redisqueue.queuedUsageDetail as marshalled by CLIProxyAPI v8.0.5."""
    t = {
        "input_tokens": 100,
        "output_tokens": 40,
        "reasoning_tokens": 10,
        "cached_tokens": 20,
        "cache_read_tokens": 20,
        "cache_read_tokens_present": True,
        "cache_creation_tokens": 0,
        "total_tokens": 140,
    }
    t.update(tokens)
    return {
        "timestamp": "2026-09-30T12:00:00Z",
        "latency_ms": latency_ms,
        "ttft_ms": 300,
        "source": SOURCE,
        "auth_index": AUTH_INDEX,
        "access_token_sha256": "tok-hash-SHOULD-NOT-LEAK",
        "client_ip": "10.9.8.7",
        "resolved_client_ip": "10.9.8.7",
        "x_forwarded_for": "10.9.8.7",
        "user_agent": "codex-cli",
        "tokens": t,
        "failed": failed,
        "generate": True,
        "stream": True,
        "fail": {"status_code": status, "body": "upstream error body" if failed else ""},
        "response_headers": {"X-Request-Id": ["abc"]},
        "accounting_version": 2,
        "token_breakdown": {"schema_version": 2, "quality": "exact", "total_tokens": 140},
        "provider": provider,
        "executor_type": "codex",
        "model": model,
        "alias": model,
        "endpoint": "/v1/responses",
        "auth_type": "oauth",
        "api_key": API_KEY,
        "request_id": "req-1",
        "execution_id": "exec-1",
        "reasoning_effort": "high",
        "service_tier": "",
    }


class FakeCLIProxy:
    """Implements GET /v8/management/observability/usage/queue with pop semantics."""

    def __init__(self):
        self.queue = []
        self.status = 200
        self.raw = None
        self.redirect_to = None
        self.delay = 0
        self.seen_auth = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.seen_auth.append(self.headers.get("Authorization"))
                time.sleep(fake.delay)
                if fake.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", fake.redirect_to)
                    self.end_headers()
                    return
                if not self.path.startswith(exporter.QUEUE_PATH + "?count="):
                    return self._reply(404, b"{}")
                if self.headers.get("Authorization") != f"Bearer {KEY}":
                    return self._reply(401, b'{"error":"invalid management key"}')
                if fake.status != 200:
                    return self._reply(fake.status, b'{"error":"boom"}')
                if fake.raw is not None:
                    return self._reply(200, fake.raw)
                n = int(self.path.split("count=")[1])
                out, fake.queue = fake.queue[:n], fake.queue[n:]
                self._reply(200, json.dumps(out).encode())

            def _reply(self, code, body):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "state.db")
        self.fake = FakeCLIProxy()
        self.logbuf = io.StringIO()
        self.handler = logging.StreamHandler(self.logbuf)
        exporter.log.addHandler(self.handler)
        self.stores = []

    def tearDown(self):
        exporter.log.removeHandler(self.handler)
        for s in self.stores:
            s.close()
        self.fake.close()
        self.tmp.cleanup()

    def make(self, max_pairs=200, batch=50, max_bytes=1 << 20, url=None):
        client = exporter.QueueClient(url or self.fake.url, KEY, timeout=2, max_bytes=max_bytes)
        store = exporter.Store(self.db, max_pairs)
        self.stores.append(store)
        state = exporter.State()
        return exporter.Poller(client, store, state, batch, 5), state

    def metrics(self, state):
        return state.render().decode()


class SchemaTests(Base):
    def test_official_record_counts(self):
        poller, state = self.make()
        self.fake.queue = [
            official_record(),
            official_record(latency_ms=500),
            official_record(failed=True, status=429, latency_ms=100, input_tokens=5, output_tokens=0, total_tokens=5),
        ]
        ok, _ = poller.poll_once()
        self.assertTrue(ok)
        m = self.metrics(state)
        self.assertIn('cliproxy_requests_total{provider="codex",model="gpt-5-codex",status="200"} 2', m)
        self.assertIn('cliproxy_requests_total{provider="codex",model="gpt-5-codex",status="429"} 1', m)
        self.assertIn('cliproxy_tokens_total{provider="codex",model="gpt-5-codex",type="input"} 205', m)
        self.assertIn('cliproxy_tokens_total{provider="codex",model="gpt-5-codex",type="output"} 80', m)
        self.assertIn('cliproxy_tokens_total{provider="codex",model="gpt-5-codex",type="cache_read"} 60', m)
        self.assertNotIn('type="cache_creation"', m)
        self.assertIn('cliproxy_request_duration_seconds_sum{provider="codex",model="gpt-5-codex"} 2.1', m)
        self.assertIn('cliproxy_request_duration_seconds_count{provider="codex",model="gpt-5-codex"} 3', m)
        self.assertIn("cliproxy_exporter_up 1", m)
        self.assertIn("cliproxy_exporter_records_total 3", m)
        self.assertEqual(self.fake.queue, [])
        self.assertEqual(self.fake.seen_auth[0], f"Bearer {KEY}")

    def test_drains_multiple_batches(self):
        poller, state = self.make(batch=2)
        self.fake.queue = [official_record() for _ in range(5)]
        poller.poll_once()
        self.assertEqual(self.fake.queue, [])
        self.assertIn('status="200"} 5', self.metrics(state))


class PersistenceTests(Base):
    def test_counters_survive_restart(self):
        poller, _ = self.make()
        self.fake.queue = [official_record(), official_record(failed=True, status=500)]
        poller.poll_once()
        self.stores.pop().close()

        poller2, state2 = self.make()
        m = self.metrics(state2)
        self.assertIn('status="200"} 1', m)
        self.assertIn('status="500"} 1', m)
        self.fake.queue = [official_record()]
        poller2.poll_once()
        self.assertIn('cliproxy_requests_total{provider="codex",model="gpt-5-codex",status="200"} 2', self.metrics(state2))
        self.assertIn('type="total"} 420', self.metrics(state2))

    def test_invalid_counter_persists(self):
        poller, _ = self.make()
        self.fake.queue = ["not-an-object"]
        poller.poll_once()
        self.stores.pop().close()
        _, state2 = self.make()
        self.assertIn('cliproxy_exporter_invalid_records_total{reason="not_object"} 1', self.metrics(state2))


class SecretTests(Base):
    def assert_clean(self, text):
        for s in SECRETS:
            self.assertNotIn(s, text)

    def test_secrets_never_in_metrics_db_or_logs(self):
        poller, state = self.make()
        self.fake.queue = [official_record(), official_record(failed=True, status=401), "junk", {"api_key": API_KEY}]
        poller.poll_once()
        self.assert_clean(self.metrics(state))
        with open(self.db, "rb") as f:
            self.assert_clean(f.read().decode("latin-1"))
        self.assert_clean(self.logbuf.getvalue())

    def test_auth_failure_does_not_log_key(self):
        client = exporter.QueueClient(self.fake.url, "wrong-" + KEY, timeout=2, max_bytes=1 << 16)
        store = exporter.Store(self.db, 10)
        self.stores.append(store)
        poller = exporter.Poller(client, store, exporter.State(), 10, 1)
        ok, status = poller.poll_once()
        self.assertFalse(ok)
        self.assertEqual(status, 401)
        self.assert_clean(self.logbuf.getvalue())
        self.assertNotIn(KEY, repr(client))

    def test_secret_valued_label_rejected(self):
        poller, state = self.make()
        self.fake.queue = [official_record(model=API_KEY), official_record(provider=SOURCE)]
        poller.poll_once()
        m = self.metrics(state)
        self.assert_clean(m)
        self.assertIn('reason="secret_label"} 2', m)

    def test_redirect_refused_and_key_not_forwarded(self):
        target = FakeCLIProxy()
        try:
            self.fake.redirect_to = target.url + exporter.QUEUE_PATH + "?count=1"
            poller, state = self.make()
            ok, _ = poller.poll_once()
            self.assertFalse(ok)
            self.assertEqual(target.seen_auth, [])
            self.assertIn("cliproxy_exporter_up 0", self.metrics(state))
        finally:
            target.close()

    def test_env_proxy_ignored(self):
        os.environ["http_proxy"] = "http://127.0.0.1:1"
        try:
            poller, _ = self.make()
            self.fake.queue = [official_record()]
            self.assertTrue(poller.poll_once()[0])
        finally:
            del os.environ["http_proxy"]

    def test_config_validation(self):
        for bad in ("ftp://x", "http://user:pw@host", "http://h?x=1", "cliproxy:8317"):
            with self.assertRaises(ValueError):
                exporter.validate_base_url(bad)
        for bad in ("", "  ", "a\r\nX-Evil: 1"):
            with self.assertRaises(ValueError):
                exporter.validate_key(bad)


class MalformedTests(Base):
    def test_malformed_records_counted_valid_ones_kept(self):
        poller, state = self.make()
        bad_tokens = official_record()
        bad_tokens["tokens"]["input_tokens"] = -1
        bool_tokens = official_record()
        bool_tokens["tokens"]["output_tokens"] = True
        huge = official_record()
        huge["tokens"]["total_tokens"] = 10**15
        no_fail = official_record()
        del no_fail["fail"]
        self.fake.queue = [
            official_record(),
            "string record",
            [1, 2],
            official_record(provider=""),
            official_record(model=None),
            official_record(status=42),
            official_record(status="200"),
            no_fail,
            bad_tokens,
            bool_tokens,
            huge,
            official_record(latency_ms=-5),
            official_record(latency_ms=1.5),
        ]
        ok, _ = poller.poll_once()
        self.assertTrue(ok)
        m = self.metrics(state)
        self.assertIn('status="200"} 1', m)
        expected = {"not_object": 2, "bad_provider": 1, "bad_model": 1, "bad_status": 3, "bad_tokens": 3, "bad_latency": 2}
        for reason, n in expected.items():
            self.assertIn(f'cliproxy_exporter_invalid_records_total{{reason="{reason}"}} {n}', m)
        self.assertIn("cliproxy_exporter_records_total 1", m)

    def test_label_sanitised_and_truncated(self):
        poller, state = self.make()
        self.fake.queue = [official_record(provider='evil"}\nx', model="m" * 500)]
        poller.poll_once()
        m = self.metrics(state)
        self.assertIn('provider="evil___x"', m)
        self.assertIn('model="' + "m" * exporter.MAX_LABEL_LEN + '"', m)
        self.assertNotIn("m" * (exporter.MAX_LABEL_LEN + 1), m)

    def test_non_array_response_is_failure(self):
        for raw in (b'{"records": []}', b"not json", b"null", b"[" * 100000):
            self.fake.raw = raw
            poller, state = self.make()
            ok, _ = poller.poll_once()
            self.assertFalse(ok, raw[:20])
            self.assertIn("cliproxy_exporter_up 0", self.metrics(state))


class FailureTests(Base):
    def test_upstream_error_sets_down_keeps_last_success(self):
        poller, state = self.make()
        self.fake.queue = [official_record()]
        poller.poll_once()
        first = state.last_success
        self.assertGreater(first, 0)
        self.fake.status = 503
        ok, status = poller.poll_once()
        self.assertFalse(ok)
        self.assertEqual(status, 503)
        m = self.metrics(state)
        self.assertIn("cliproxy_exporter_up 0", m)
        self.assertIn(f"cliproxy_exporter_last_success_timestamp_seconds {first!r}", m)
        self.assertIn('cliproxy_exporter_polls_total{result="failure"} 1', m)
        self.assertIn('status="200"} 1', m)

    def test_unreachable_upstream(self):
        self.fake.close()
        poller, state = self.make()
        self.assertFalse(poller.poll_once()[0])
        self.assertIn("cliproxy_exporter_up 0", self.metrics(state))
        self.fake = FakeCLIProxy()

    def test_oversized_response_rejected(self):
        poller, state = self.make(max_bytes=1024)
        self.fake.queue = [official_record() for _ in range(20)]
        self.assertFalse(poller.poll_once()[0])
        self.assertIn("cliproxy_exporter_up 0", self.metrics(state))

    def test_timeout(self):
        self.fake.delay = 3
        poller, state = self.make()
        started = time.monotonic()
        self.assertFalse(poller.poll_once()[0])
        self.assertLess(time.monotonic() - started, 3)


class CardinalityTests(Base):
    def test_pairs_capped_with_overflow(self):
        poller, state = self.make(max_pairs=3)
        self.fake.queue = [official_record(model=f"model-{i}") for i in range(10)]
        poller.poll_once()
        m = self.metrics(state)
        req_lines = [l for l in m.splitlines() if l.startswith("cliproxy_requests_total{")]
        self.assertEqual(len(req_lines), 4)
        self.assertIn('cliproxy_requests_total{provider="_overflow_",model="_overflow_",status="200"} 7', m)
        self.assertIn("cliproxy_exporter_label_overflow_total 7", m)

    def test_cap_survives_restart(self):
        poller, _ = self.make(max_pairs=2)
        self.fake.queue = [official_record(model=f"a{i}") for i in range(2)]
        poller.poll_once()
        self.stores.pop().close()
        poller2, state2 = self.make(max_pairs=2)
        self.fake.queue = [official_record(model="new")]
        poller2.poll_once()
        self.assertNotIn('model="new"', self.metrics(state2))


class HttpTests(Base):
    def test_metrics_and_healthz_served_while_upstream_hangs(self):
        poller, state = self.make()
        self.fake.delay = 1.5
        srv = ThreadingHTTPServer(("127.0.0.1", 0), exporter.make_handler(state, stale_after=120))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        t = threading.Thread(target=poller.poll_once)
        t.start()
        try:
            started = time.monotonic()
            body = urllib.request.urlopen(base + "/metrics", timeout=1).read().decode()
            health = json.loads(urllib.request.urlopen(base + "/healthz", timeout=1).read())
            self.assertLess(time.monotonic() - started, 1)
            self.assertIn("cliproxy_exporter_start_time_seconds", body)
            self.assertTrue(health["poller_alive"])
        finally:
            t.join()
            srv.shutdown()
            srv.server_close()

    def test_healthz_503_when_poller_stale(self):
        state = exporter.State()
        state.heartbeat = time.time() - 1000
        srv = ThreadingHTTPServer(("127.0.0.1", 0), exporter.make_handler(state, stale_after=120))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/healthz", timeout=1)
            self.assertEqual(ctx.exception.code, 503)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
