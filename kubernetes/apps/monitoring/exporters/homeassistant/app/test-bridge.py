"""Run: python3 -m unittest -v test_bridge (from this directory)."""

import email.message
import http.server
import io
import json
import re
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import bridge

DASHBOARD = Path(__file__).resolve().parents[6] / "docs" / "home-assistant-monitoring.yaml"


def vector(*values):
    return json.dumps(
        {
            "status": "success",
            "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1.0, v]} for v in values]},
        }
    ).encode()


class FakeResponse(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status


class FakeOpener:
    def __init__(self, respond):
        self.respond = respond
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        return self.respond(req)


class FakeProm:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def query(self, expr, timeout=None):
        self.calls.append(expr)
        value = self.values.get(expr)
        return (value, None) if value is not None else (None, "empty")


class FakeHA:
    def __init__(self, fail_after=None):
        self.published = []
        self.fail_after = fail_after

    def publish(self, topic, payload, retain, timeout=None):
        if self.fail_after is not None and len(self.published) >= self.fail_after:
            return False
        self.published.append((topic, payload, retain))
        return True


class ParseVectorTest(unittest.TestCase):
    def test_single_finite_sample(self):
        self.assertEqual(bridge.parse_vector(vector("42.5")), (42.5, None))

    def test_rejects_unusable_results(self):
        cases = {
            "empty": vector(),
            "multiple_series": vector("1", "2"),
            "nonfinite": vector("NaN"),
            "bad_shape": vector("abc"),
            "query_error": json.dumps({"status": "error", "error": "boom"}).encode(),
        }
        for reason, body in cases.items():
            with self.subTest(reason):
                self.assertEqual(bridge.parse_vector(body), (None, reason))
        for body in (vector("+Inf"), vector("-Inf")):
            self.assertEqual(bridge.parse_vector(body), (None, "nonfinite"))

    def test_rejects_wrong_shapes(self):
        matrix = json.dumps({"status": "success", "data": {"resultType": "matrix", "result": []}}).encode()
        for body in (matrix, b"not json", b"[]", json.dumps({"status": "success"}).encode()):
            with self.subTest(body=body[:20]):
                self.assertEqual(bridge.parse_vector(body)[0], None)


class PrometheusClientTest(unittest.TestCase):
    def query(self, respond):
        return bridge.Prometheus("http://prom:9090", opener=FakeOpener(respond)).query("up", timeout=5)

    def test_oversized_response_is_rejected(self):
        body = vector("1") + b" " * bridge.MAX_PROM_BYTES
        self.assertEqual(self.query(lambda _: FakeResponse(body)), (None, "oversized"))

    def test_transport_failures_are_unavailable(self):
        def http_error(_):
            raise urllib.error.HTTPError("u", 503, "x", email.message.Message(), io.BytesIO(b""))

        def refused(_):
            raise urllib.error.URLError(ConnectionRefusedError())

        def timeout(_):
            raise TimeoutError()

        self.assertEqual(self.query(http_error), (None, "http_status"))
        self.assertEqual(self.query(refused), (None, "unreachable"))
        self.assertEqual(self.query(timeout), (None, "unreachable"))

    def test_sends_server_side_timeout_below_client_timeout(self):
        opener = FakeOpener(lambda _: FakeResponse(vector("1")))
        bridge.Prometheus("http://prom:9090", opener=opener).query("up", timeout=5)
        url, timeout = opener.requests[0]
        self.assertIn("timeout=4s", url)
        self.assertEqual(timeout, 5)


class QueryDefinitionTest(unittest.TestCase):
    def test_every_query_gates_source_freshness_by_timestamp(self):
        for s in bridge.SENSORS:
            with self.subTest(s["key"]):
                self.assertIn("timestamp(", s["query"])
                self.assertNotIn("vector(", s["query"], "no default value may be injected")

    def test_exporter_backed_queries_gate_on_up(self):
        for s in bridge.SENSORS:
            if s["key"] == "cluster_targets_down":
                continue
            with self.subTest(s["key"]):
                self.assertRegex(s["query"], r"and on\(\) \(min\(up\{job=\"[a-z0-9-]+\"")

    def test_ilo_queries_use_ilo_job_and_window(self):
        for host in ("dl360", "dl380"):
            for kind in ("cpu_temperature", "inlet_temperature", "fan_speed", "power"):
                q = next(s["query"] for s in bridge.SENSORS if s["key"] == f"{host}_{kind}")
                self.assertIn(f'job="ilo-exporter-{host}"', q)
                self.assertIn(f"< {bridge.FRESH_ILO}", q)

    def test_node_queries_pin_instance(self):
        for device, instance in (("home_cluster", "192.168.1.85:9100"), ("gpu_worker", "192.168.1.86:9100")):
            for s in (s for s in bridge.SENSORS if s["device"] == device):
                self.assertIn(f'instance="{instance}"', s["query"])


class DiscoveryTest(unittest.TestCase):
    def test_ids_are_stable_unique_and_consistent(self):
        ids = [bridge.object_id(s) for s in bridge.SENSORS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 20)
        for s in bridge.SENSORS:
            p = bridge.discovery_payload(s)
            oid = bridge.object_id(s)
            self.assertRegex(oid, r"^homelab_[a-z0-9_]+$")
            self.assertEqual(p["unique_id"], oid)
            self.assertEqual(p["default_entity_id"], f"sensor.{oid}")
            self.assertEqual(bridge.discovery_topic(s), f"homeassistant/sensor/{oid}/config")
            self.assertEqual(p["expire_after"], 90)
            self.assertEqual(p["availability_topic"], bridge.availability_topic(s))
            self.assertNotEqual(p["state_topic"], p["availability_topic"])

    def test_units_match_device_classes(self):
        for s in bridge.SENSORS:
            p = bridge.discovery_payload(s)
            with self.subTest(s["key"]):
                if p.get("device_class") == "temperature":
                    self.assertEqual(p["unit_of_measurement"], "°C")
                elif p.get("device_class") == "power":
                    self.assertEqual(p["unit_of_measurement"], "W")
                elif s["key"].startswith("cluster_") or s["key"] == "truenas_unhealthy_pools":
                    self.assertNotIn("unit_of_measurement", p)
                else:
                    self.assertEqual(p["unit_of_measurement"], "%")

    def test_payload_is_json_serializable(self):
        for s in bridge.SENSORS:
            json.loads(json.dumps(bridge.discovery_payload(s)))

    @unittest.skipUnless(DASHBOARD.exists(), "dashboard not in checkout")
    def test_dashboard_references_exactly_the_published_entities(self):
        referenced = set(re.findall(r"sensor\.homelab_[a-z0-9_]+", DASHBOARD.read_text()))
        published = {f"sensor.{bridge.object_id(s)}" for s in bridge.SENSORS}
        self.assertEqual(referenced - published, set(), "dashboard uses an entity the bridge never publishes")
        self.assertEqual(published - referenced, set(), "published sensor missing from the dashboard")


class BridgeCycleTest(unittest.TestCase):
    def setUp(self):
        self.sensors = bridge.SENSORS[:3]
        self.ok, self.missing, self.third = self.sensors

    def make(self, values, ha=None):
        prom = FakeProm(values)
        ha = ha or FakeHA()
        return bridge.Bridge(prom, ha, sensors=self.sensors), prom, ha

    def test_startup_publishes_discovery_then_offline_without_querying(self):
        b, prom, ha = self.make({})
        self.assertTrue(b.startup())
        self.assertEqual(prom.calls, [])
        configs = [p for p in ha.published if p[0].endswith("/config")]
        offline = [p for p in ha.published if p[0].endswith("/availability")]
        self.assertEqual(len(configs), 3)
        self.assertTrue(all(retain for _, _, retain in configs))
        self.assertEqual({(t, p, r) for t, p, r in offline},
                         {(bridge.availability_topic(s), "offline", True) for s in self.sensors})

    def test_startup_reports_failure_when_ha_rejects(self):
        b, _, _ = self.make({}, ha=FakeHA(fail_after=0))
        self.assertFalse(b.startup())

    def test_value_publishes_state_unretained_then_online_retained(self):
        b, _, ha = self.make({self.ok["query"]: 41.0})
        b.last_discovery = b.clock()
        b.cycle()
        mine = [p for p in ha.published if bridge.object_id(self.ok) in p[0]]
        self.assertEqual(mine, [
            (bridge.state_topic(self.ok), "41", False),
            (bridge.availability_topic(self.ok), "online", True),
        ])
        self.assertTrue(b.available[bridge.object_id(self.ok)])

    def test_missing_value_publishes_offline_and_no_state(self):
        b, _, ha = self.make({self.ok["query"]: 41.0})
        b.last_discovery = b.clock()
        b.cycle()
        mine = [p for p in ha.published if bridge.object_id(self.missing) in p[0]]
        self.assertEqual(mine, [(bridge.availability_topic(self.missing), "offline", True)])
        self.assertFalse(b.available[bridge.object_id(self.missing)])

    def test_publish_failure_stops_cycle_and_leaves_rest_offline(self):
        values = {s["query"]: 1.0 for s in self.sensors}
        b, _, ha = self.make(values, ha=FakeHA(fail_after=1))
        b.last_discovery = b.clock()
        b.cycle()
        self.assertEqual(len(ha.published), 1)
        self.assertFalse(any(b.available.values()), "state without online must not count as available")
        self.assertGreater(b.publish_failures, 0)

    def test_slow_prometheus_hits_deadline_and_marks_rest_offline(self):
        now = [1000.0]

        class SlowProm(FakeProm):
            def query(self, expr, timeout=None):
                now[0] += bridge.INTERVAL  # one query eats the whole budget
                return 5.0, None

        ha = FakeHA()
        b = bridge.Bridge(SlowProm({}), ha, sensors=self.sensors, clock=lambda: now[0])
        b.last_discovery = now[0]
        b.cycle()
        self.assertEqual(b.query_failures.get("deadline"), 2)
        self.assertNotIn(("online"), [p for _, p, _ in ha.published])

    def test_discovery_is_refreshed_periodically(self):
        now = [10_000.0]
        ha = FakeHA()
        b = bridge.Bridge(FakeProm({}), ha, sensors=self.sensors, clock=lambda: now[0])
        b.last_discovery = now[0]
        b.cycle()
        self.assertFalse(any(t.endswith("/config") for t, _, _ in ha.published))
        now[0] += bridge.DISCOVERY_REFRESH
        b.cycle()
        self.assertEqual(sum(t.endswith("/config") for t, _, _ in ha.published), 3)


class HomeAssistantClientTest(unittest.TestCase):
    TOKEN = "tok-do-not-log"

    def test_publish_sends_bearer_and_service_payload(self):
        opener = FakeOpener(lambda _: FakeResponse(b"[]"))
        ok = bridge.HomeAssistant("http://ha:8123/", self.TOKEN, opener=opener).publish("a/b", "1", False)
        self.assertTrue(ok)
        req, _ = opener.requests[0]
        self.assertEqual(req.full_url, "http://ha:8123/api/services/mqtt/publish")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {self.TOKEN}")
        self.assertEqual(json.loads(req.data), {"topic": "a/b", "payload": "1", "retain": False, "qos": 1})

    def test_error_logs_carry_no_body_or_token(self):
        def unauthorized(_):
            raise urllib.error.HTTPError("u", 401, "x", email.message.Message(), io.BytesIO(b"secret-body " + self.TOKEN.encode()))

        ha = bridge.HomeAssistant("http://ha:8123", self.TOKEN, opener=FakeOpener(unauthorized))
        with self.assertLogs("homeassistant-metrics", "WARNING") as logs:
            self.assertFalse(ha.publish("a/b", "1", True))
        text = "\n".join(logs.output)
        self.assertIn("401", text)
        self.assertNotIn("secret-body", text)
        self.assertNotIn(self.TOKEN, text)

    def test_redirect_is_not_followed(self):
        hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append((self.path, self.headers.get("Authorization")))
                self.send_response(302 if self.path.startswith("/api") else 200)
                self.send_header("Location", "/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST

            def log_message(self, format, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            with self.assertLogs("homeassistant-metrics", "WARNING"):
                self.assertFalse(bridge.HomeAssistant(url, self.TOKEN).publish("a", "1", False))
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual([p for p, _ in hits], ["/api/services/mqtt/publish"])


class HttpEndpointTest(unittest.TestCase):
    def setUp(self):
        self.now = [5000.0]
        self.b = bridge.Bridge(FakeProm({}), FakeHA(), sensors=bridge.SENSORS[:2], clock=lambda: self.now[0])
        self.b.last_publish_ok = 4990.0
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), bridge.make_handler(self.b))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode()

    def test_metrics_are_served_from_snapshot(self):
        self.b.snapshot()
        status, body = self.get("/metrics")
        self.assertEqual(status, 200)
        self.assertIn("homelab_bridge_up 1", body)
        self.assertIn("homelab_bridge_last_successful_publish_timestamp_seconds 4990.000", body)
        self.assertIn('homelab_bridge_sensor_available{sensor="homelab_dl360_cpu_temperature"} 0', body)
        self.b.last_publish_ok = 9999.0
        self.assertNotIn("9999", self.get("/metrics")[1], "served body must be the cached snapshot")

    def test_healthz_goes_503_when_loop_stalls(self):
        self.b.snapshot()
        self.assertEqual(self.get("/healthz")[0], 200)
        self.now[0] += bridge.INTERVAL * 4
        self.b.snapshot()
        self.assertEqual(self.get("/healthz")[0], 503)
        self.assertIn("homelab_bridge_up 0", self.get("/metrics")[1])

    def test_unknown_path_404(self):
        self.assertEqual(self.get("/")[0], 404)


class FormatTest(unittest.TestCase):
    def test_format_value(self):
        self.assertEqual(bridge.format_value(2.0), "2")
        self.assertEqual(bridge.format_value(13.19123), "13.191")
        self.assertEqual(bridge.format_value(0.5), "0.5")



class APIHealthTests(unittest.TestCase):
    def test_counts_exclude_bridge_and_do_not_emit_names(self):
        rows = [{"entity_id": "light.private", "state": "on"}, {"entity_id": "sensor.bad", "state": "unavailable"}, {"entity_id": "sensor.pending", "state": "unknown"}, {"entity_id": "sensor.homelab_cpu", "state": "unavailable"}]
        ha = bridge.HomeAssistant("http://ha", "token", FakeOpener(lambda _: FakeResponse(json.dumps(rows).encode())))
        self.assertEqual(ha.entity_counts(), {"available": 1, "unavailable": 1, "unknown": 1})

    def test_bad_and_oversized_responses_fail_closed(self):
        for body in (b"{}", b"invalid", b'[{"entity_id": "sensor.a"}]', b"x" * (bridge.MAX_HA_STATES_BYTES + 1)):
            ha = bridge.HomeAssistant("http://ha", "token", FakeOpener(lambda _, body=body: FakeResponse(body)))
            self.assertIsNone(ha.entity_counts())

    def test_cached_health_expires_without_snapshot(self):
        now = [100.0]
        b = bridge.Bridge(FakeProm({}), FakeHA(), sensors=[], clock=lambda: now[0])
        b.snapshot()
        self.assertTrue(b.cached()[1])
        now[0] += 121
        self.assertFalse(b.cached()[1])

    def test_failed_api_drops_previous_counts(self):
        class HA(FakeHA):
            def entity_counts(self, timeout=5):
                return self.counts
        ha = HA(); ha.counts = {"available": 1, "unknown": 0, "unavailable": 0}
        b = bridge.Bridge(FakeProm({}), ha, sensors=[])
        b.cycle(); self.assertIn(b'homeassistant_entities{state="available"} 1', b.render_metrics())
        ha.counts = None
        b.cycle(); self.assertIn(b"homeassistant_api_up 0", b.render_metrics())
        self.assertNotIn(b'homeassistant_entities{state=', b.render_metrics())

if __name__ == "__main__":
    unittest.main()
