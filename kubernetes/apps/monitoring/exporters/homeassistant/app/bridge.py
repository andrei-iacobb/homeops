"""Prometheus -> Home Assistant bridge.

Every INTERVAL seconds this runs a fixed set of PromQL queries and publishes each
result as a native Home Assistant MQTT sensor, going through Home Assistant's
own mqtt.publish service (POST /api/services/mqtt/publish) so it reuses the
broker HA already talks to. Stdlib only.

Fail-closed rules:
- A sensor is published only when its query returns exactly one finite sample.
  Missing, empty, multi-series, NaN/Inf, oversized, timed-out or failed queries
  mark that sensor offline. Nothing ever defaults to 0.
- Freshness lives in PromQL. An instant query stamps its result with the
  evaluation time, and aggregation drops the source timestamps, so every query
  filters source samples through timestamp() before aggregating and gates on
  the exporter's up series.
- Availability is retained per sensor, and every sensor is set offline on
  startup before the first query and again on SIGTERM.
- State is NOT retained. The HA MQTT docs warn that a retained state is replayed
  when HA restarts, which makes an expired value available again. HA restores
  non-retained state itself and keeps counting expire_after, so a stale value
  cannot come back.
- expire_after (90s = 3 missed cycles) covers a hard kill: publishing through
  HA's REST API means the bridge has no MQTT last will, so its retained
  availability stays "online" after a crash. expire_after is what flips the
  state to unavailable in that case.
"""

import http.server
import json
import logging
import math
import os
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("homeassistant-metrics")

INTERVAL = 30
EXPIRE_AFTER = 90
# Refreshes retained discovery after a broker restart without persistence;
# HA ignores a config it has already seen.
DISCOVERY_REFRESH = 300
QUERY_TIMEOUT = 10
PUBLISH_TIMEOUT = 5
# Every query aggregates to one series, so a real response is a few hundred
# bytes. Anything bigger is a broken query, not data.
MAX_PROM_BYTES = 64 * 1024
MAX_HA_BYTES = 64 * 1024
MAX_HA_STATES_BYTES = 8 * 1024 * 1024

DISCOVERY_PREFIX = "homeassistant"
TOPIC_PREFIX = "homelab-metrics"
ID_PREFIX = "homelab"

GRAFANA = "https://grafana.iacob.uk/d/"

# Freshness windows. Most jobs scrape every 15-30s; the iLO exporters scrape
# every 120s, so they get 300s to survive one missed scrape.
FRESH = 180
FRESH_ILO = 300

HOME_CLUSTER = "192.168.1.85:9100"
GPU_WORKER = "192.168.1.86:9100"

DEVICES = {
    "dl360": {
        "name": "DL360",
        "manufacturer": "HPE",
        "model": "ProLiant DL360 Gen9",
        "dashboard": "ilo",
    },
    "dl380": {
        "name": "DL380",
        "manufacturer": "HPE",
        "model": "ProLiant DL380 Gen9",
        "dashboard": "ilo",
    },
    "home_cluster": {
        "name": "home-cluster node",
        "manufacturer": "Sidero Labs",
        "model": "Talos Linux",
        "dashboard": "kubernetes",
    },
    "gpu_worker": {
        "name": "gpu-worker node",
        "manufacturer": "Sidero Labs",
        "model": "Talos Linux",
        "dashboard": "gpu",
    },
    "gpu": {
        "name": "GPU",
        "manufacturer": "NVIDIA",
        "model": "Quadro P1000",
        "dashboard": "gpu",
    },
    "truenas": {
        "name": "TrueNAS",
        "manufacturer": "iXsystems",
        "model": "TrueNAS",
        "dashboard": "truenas",
    },
    "cluster": {
        "name": "Kubernetes cluster",
        "manufacturer": "homeops",
        "model": "Talos Kubernetes",
        "dashboard": "kubernetes",
    },
}


def fresh(selector, max_age=FRESH):
    """Keep only source samples scraped within max_age seconds."""
    return f"({selector} and (time() - timestamp({selector}) < {max_age}))"


def up(job, instance=None):
    labels = f'job="{job}"' + (f',instance="{instance}"' if instance else "")
    return f"and on() (min(up{{{labels}}}) == 1)"


def ilo_max(host, metric, name_re=None):
    job = f"ilo-exporter-{host}"
    matcher = f'job="{job}"' + (f',name=~"{name_re}"' if name_re else "")
    return f"max({fresh(f'{metric}{{{matcher}}}', FRESH_ILO)}) {up(job)}"


def node_cpu(instance):
    idle = f'node_cpu_seconds_total{{job="node-exporter",instance="{instance}",mode="idle"}}'
    return (
        f"100 * (1 - avg(rate({idle}[2m])))"
        f" and on() (time() - max(timestamp({idle})) < {FRESH})"
        f" {up('node-exporter', instance)}"
    )


def node_ratio(instance, numerator, denominator, extra="", used=False):
    labels = f'job="node-exporter",instance="{instance}"{extra}'
    ratio = f"sum({fresh(f'{numerator}{{{labels}}}')}) / sum({fresh(f'{denominator}{{{labels}}}')})"
    body = f"(1 - {ratio})" if used else f"({ratio})"
    return f"100 * {body} {up('node-exporter', instance)}"


def node_memory_used(instance):
    return node_ratio(instance, "node_memory_MemAvailable_bytes", "node_memory_MemTotal_bytes", used=True)


def node_var_free(instance):
    # Talos keeps everything writable (images, PVCs, logs) on /var.
    return node_ratio(
        instance, "node_filesystem_avail_bytes", "node_filesystem_size_bytes", ',mountpoint="/var"'
    )


def gpu(expr):
    return f"{expr} {up('gpu-exporter')}"


def gpu_max(metric):
    selector = metric + '{job="gpu-exporter"}'
    return f"max({fresh(selector)})"


def pool_used(pool):
    labels = f'job="truenas-exporter",host="truenas",pool="{pool}"'
    # Ratio of sums stays correct if a rollout briefly exposes two pods.
    return (
        f"100 * sum({fresh(f'truenas_pool_allocated_bytes{{{labels}}}')})"
        f" / sum({fresh(f'truenas_pool_size_bytes{{{labels}}}')})"
        f" {up('truenas-exporter')}"
    )


KSM = 'job="kube-state-metrics"'
NODE_READY = f'kube_node_status_condition{{{KSM},condition="Ready",status="true"}}'
DEPLOY_UNAVAILABLE = f"kube_deployment_status_replicas_unavailable{{{KSM}}}"


def sensor(key, device, name, query, unit=None, device_class=None, icon=None, precision=1):
    return {
        "key": key,
        "device": device,
        "name": name,
        "query": query,
        "unit": unit,
        "device_class": device_class,
        "icon": icon,
        "precision": precision,
    }


def _ilo_sensors(host):
    return [
        sensor(f"{host}_cpu_temperature", host, "CPU temperature",
               ilo_max(host, "ilo_chassis_temperature_current", ".*CPU.*"), "°C", "temperature", precision=0),
        sensor(f"{host}_inlet_temperature", host, "Inlet temperature",
               ilo_max(host, "ilo_chassis_temperature_current", ".*Inlet Ambient.*"), "°C", "temperature", precision=0),
        sensor(f"{host}_fan_speed", host, "Fan speed",
               ilo_max(host, "ilo_chassis_fan_current_percent"), "%", icon="mdi:fan", precision=0),
        sensor(f"{host}_power", host, "Power draw",
               ilo_max(host, "ilo_power_current_watt"), "W", "power", precision=0),
    ]


def _node_sensors(device, instance):
    return [
        sensor(f"{device}_cpu_usage", device, "CPU usage", node_cpu(instance), "%", icon="mdi:cpu-64-bit"),
        sensor(f"{device}_memory_used", device, "Memory used", node_memory_used(instance), "%", icon="mdi:memory"),
        sensor(f"{device}_disk_free", device, "Disk free (/var)", node_var_free(instance), "%", icon="mdi:harddisk"),
    ]


SENSORS = [
    *_ilo_sensors("dl360"),
    *_ilo_sensors("dl380"),
    *_node_sensors("home_cluster", HOME_CLUSTER),
    *_node_sensors("gpu_worker", GPU_WORKER),
    sensor("gpu_temperature", "gpu", "Temperature", gpu(gpu_max("nvidia_smi_temperature_gpu")),
           "°C", "temperature", precision=0),
    sensor("gpu_utilization", "gpu", "Utilization", gpu(f"100 * {gpu_max('nvidia_smi_utilization_gpu_ratio')}"),
           "%", icon="mdi:expansion-card", precision=0),
    sensor("gpu_memory_used", "gpu", "Memory used",
           gpu(f"100 * {gpu_max('nvidia_smi_memory_used_bytes')} / {gpu_max('nvidia_smi_memory_total_bytes')}"),
           "%", icon="mdi:memory"),
    # No GPU power sensor: the P1000 reports power.draw as N/A, so the exporter
    # never emits nvidia_smi_power_draw_watts (checked live 2026-09-30).
    # Pool names confirmed live on 2026-09-30: SSD, plex, sexy-pool.
    sensor("truenas_ssd_used", "truenas", "SSD pool used", pool_used("SSD"), "%", icon="mdi:database"),
    sensor("truenas_plex_used", "truenas", "plex pool used", pool_used("plex"), "%", icon="mdi:database"),
    sensor("truenas_sexy_pool_used", "truenas", "sexy-pool used", pool_used("sexy-pool"), "%", icon="mdi:database"),
    sensor("truenas_unhealthy_pools", "truenas", "Unhealthy pools",
           f"sum(max by (host, pool) ({fresh('truenas_pool_healthy')}) == bool 0)"
           f" {up('truenas-exporter')}", icon="mdi:database-alert", precision=0),
    sensor("cluster_nodes_ready", "cluster", "Nodes ready",
           f"sum(max by (node) ({fresh(NODE_READY)}))"
           f" {up('kube-state-metrics')}",
           icon="mdi:server-network", precision=0),
    sensor("cluster_deployments_unavailable", "cluster", "Deployments unavailable",
           f"sum(max by (namespace, deployment) ({fresh(DEPLOY_UNAVAILABLE)}) > bool 0)"
           f" {up('kube-state-metrics')}",
           icon="mdi:kubernetes", precision=0),
    # up is written by Prometheus itself, so there is no exporter to gate on.
    sensor("cluster_targets_down", "cluster", "Scrape targets down",
           f"sum(max by (job, instance) ({fresh('up', FRESH_ILO)}) == bool 0)",
           icon="mdi:target", precision=0),
]


def object_id(s):
    return f"{ID_PREFIX}_{s['key']}"


def state_topic(s):
    return f"{TOPIC_PREFIX}/{object_id(s)}/state"


def availability_topic(s):
    return f"{TOPIC_PREFIX}/{object_id(s)}/availability"


def discovery_topic(s):
    return f"{DISCOVERY_PREFIX}/sensor/{object_id(s)}/config"


def discovery_payload(s):
    oid = object_id(s)
    dev = DEVICES[s["device"]]
    payload = {
        "name": s["name"],
        "unique_id": oid,
        "default_entity_id": f"sensor.{oid}",
        "state_topic": state_topic(s),
        "availability_topic": availability_topic(s),
        "payload_available": "online",
        "payload_not_available": "offline",
        "expire_after": EXPIRE_AFTER,
        "state_class": "measurement",
        "suggested_display_precision": s["precision"],
        "device": {
            "identifiers": [f"{ID_PREFIX}_{s['device']}"],
            "name": dev["name"],
            "manufacturer": dev["manufacturer"],
            "model": dev["model"],
            "configuration_url": GRAFANA + dev["dashboard"],
        },
        "origin": {"name": "homeassistant-metrics bridge"},
    }
    if s["unit"]:
        payload["unit_of_measurement"] = s["unit"]
    if s["device_class"]:
        payload["device_class"] = s["device_class"]
    if s["icon"]:
        payload["icon"] = s["icon"]
    return payload


def format_value(value):
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would carry the bearer token to wherever it points.
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class Prometheus:
    def __init__(self, base_url, opener=None):
        self.base_url = base_url.rstrip("/")
        self.opener = opener or _opener

    def query(self, expr, timeout=QUERY_TIMEOUT):
        """Return one finite float, or (None, reason). Never raises."""
        url = f"{self.base_url}/api/v1/query?" + urllib.parse.urlencode(
            {"query": expr, "timeout": f"{max(1, int(timeout) - 1)}s"}
        )
        try:
            with self.opener.open(url, timeout=timeout) as resp:
                if resp.status != 200:
                    return None, "http_status"
                body = resp.read(MAX_PROM_BYTES + 1)
        except urllib.error.HTTPError as e:
            e.close()
            return None, "http_status"
        except (OSError, ValueError):
            return None, "unreachable"
        if len(body) > MAX_PROM_BYTES:
            return None, "oversized"
        return parse_vector(body)


def parse_vector(body):
    try:
        doc = json.loads(body)
        if doc.get("status") != "success":
            return None, "query_error"
        data = doc["data"]
        if data.get("resultType") != "vector":
            return None, "bad_shape"
        result = data["result"]
        if not result:
            return None, "empty"
        if len(result) != 1:
            return None, "multiple_series"
        value = float(result[0]["value"][1])
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None, "bad_shape"
    if not math.isfinite(value):
        return None, "nonfinite"
    return value, None


class HomeAssistant:
    def __init__(self, base_url, token, opener=None):
        self.url = base_url.rstrip("/") + "/api/services/mqtt/publish"
        self.states_url = base_url.rstrip("/") + "/api/states"
        self._token = token
        self.opener = opener or _opener

    def entity_counts(self, timeout=5):
        """Aggregate health only; no entity names or attributes leave HA."""
        req = urllib.request.Request(self.states_url, headers={"Authorization": f"Bearer {self._token}"})
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                body = resp.read(MAX_HA_STATES_BYTES + 1)
            if len(body) > MAX_HA_STATES_BYTES:
                return None
            rows = json.loads(body)
            if not isinstance(rows, list):
                return None
            counts = {"available": 0, "unavailable": 0, "unknown": 0}
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("entity_id"), str) or not isinstance(row.get("state"), str):
                    return None
                if row["entity_id"].startswith("sensor.homelab_"):
                    continue
                state = row["state"]
                counts[state if state in ("unavailable", "unknown") else "available"] += 1
            return counts
        except urllib.error.HTTPError as e:
            e.close()
            return None
        except (OSError, ValueError, TypeError):
            return None

    def publish(self, topic, payload, retain, timeout=PUBLISH_TIMEOUT):
        """True on 2xx. Logs only a status code or error class, never a body."""
        body = json.dumps({"topic": topic, "payload": payload, "retain": retain, "qos": 1}).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                resp.read(MAX_HA_BYTES)
                if 200 <= resp.status < 300:
                    return True
                log.warning("mqtt.publish returned HTTP %s", resp.status)
        except urllib.error.HTTPError as e:
            e.close()
            log.warning("mqtt.publish returned HTTP %s", e.code)
        except (OSError, ValueError) as e:
            log.warning("mqtt.publish failed: %s", type(e).__name__)
        return False


class Bridge:
    def __init__(self, prom, ha, sensors=SENSORS, clock=time.time):
        self.prom = prom
        self.ha = ha
        self.sensors = sensors
        self.clock = clock
        self.available = {object_id(s): False for s in sensors}
        self.last_publish_ok = 0.0
        self.last_heartbeat = clock()
        self.last_discovery = 0.0
        self.last_cycle_duration = 0.0
        self.query_failures = {}
        self.publish_failures = 0
        self.ha_counts = None
        self.ha_api_up = 0
        self.lock = threading.Lock()

    def _publish(self, topic, payload, retain, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            return False
        ok = self.ha.publish(topic, payload, retain, timeout=min(PUBLISH_TIMEOUT, remaining))
        if ok:
            self.last_publish_ok = self.clock()
        else:
            self.publish_failures += 1
        return ok

    def publish_discovery(self, deadline):
        for s in self.sensors:
            if not self._publish(discovery_topic(s), json.dumps(discovery_payload(s)), True, deadline):
                return False
        self.last_discovery = self.clock()
        return True

    def mark_all_offline(self, deadline):
        for s in self.sensors:
            if not self._publish(availability_topic(s), "offline", True, deadline):
                return False
            self.available[object_id(s)] = False
        return True

    def startup(self):
        """Discovery, then every sensor offline. Must succeed before any query."""
        deadline = self.clock() + INTERVAL
        return self.publish_discovery(deadline) and self.mark_all_offline(deadline)

    def cycle(self):
        start = self.clock()
        self.last_heartbeat = start
        # Leave headroom so a slow Prometheus or HA cannot push cycles past
        # expire_after.
        query_deadline = start + INTERVAL * 0.5
        publish_deadline = start + INTERVAL * 0.9

        counts_method = getattr(self.ha, "entity_counts", None)
        self.ha_counts = counts_method(timeout=5) if counts_method else None
        self.ha_api_up = int(self.ha_counts is not None)
        results = []
        for s in self.sensors:
            remaining = query_deadline - self.clock()
            if remaining <= 0:
                value, reason = None, "deadline"
            else:
                value, reason = self.prom.query(s["query"], timeout=min(QUERY_TIMEOUT, remaining))
            if reason:
                self.query_failures[reason] = self.query_failures.get(reason, 0) + 1
            results.append((s, value))

        if start - self.last_discovery >= DISCOVERY_REFRESH:
            self.publish_discovery(publish_deadline)

        for s, value in results:
            oid = object_id(s)
            if value is None:
                if not self._publish(availability_topic(s), "offline", True, publish_deadline):
                    break
                if self.available[oid]:
                    log.info("%s unavailable", oid)
                self.available[oid] = False
                continue
            # State first, so the entity never comes online showing an old value.
            if not self._publish(state_topic(s), format_value(value), False, publish_deadline):
                break
            if not self._publish(availability_topic(s), "online", True, publish_deadline):
                break
            self.available[oid] = True

        self.last_cycle_duration = self.clock() - start
        self.last_heartbeat = self.clock()

    def healthy(self):
        return self.clock() - self.last_heartbeat < INTERVAL * 4

    def render_metrics(self):
        up_value = 1 if self.healthy() else 0
        lines = [
            "# HELP homeassistant_api_up Last bounded HA states API request succeeded.",
            "# TYPE homeassistant_api_up gauge",
            f"homeassistant_api_up {self.ha_api_up}",
            "# HELP homelab_bridge_up 1 while the bridge loop is running on schedule.",
            "# TYPE homelab_bridge_up gauge",
            f"homelab_bridge_up {up_value}",
            "# HELP homelab_bridge_last_successful_publish_timestamp_seconds Last accepted mqtt.publish call.",
            "# TYPE homelab_bridge_last_successful_publish_timestamp_seconds gauge",
            f"homelab_bridge_last_successful_publish_timestamp_seconds {self.last_publish_ok:.3f}",
            "# HELP homelab_bridge_cycle_duration_seconds Duration of the last query and publish cycle.",
            "# TYPE homelab_bridge_cycle_duration_seconds gauge",
            f"homelab_bridge_cycle_duration_seconds {self.last_cycle_duration:.3f}",
            "# HELP homelab_bridge_publish_failures_total Rejected or failed mqtt.publish calls.",
            "# TYPE homelab_bridge_publish_failures_total counter",
            f"homelab_bridge_publish_failures_total {self.publish_failures}",
            "# HELP homelab_bridge_query_failures_total Queries that produced no usable value, by reason.",
            "# TYPE homelab_bridge_query_failures_total counter",
        ]
        lines += [
            f'homelab_bridge_query_failures_total{{reason="{r}"}} {n}' for r, n in sorted(self.query_failures.items())
        ]
        lines += [
            "# HELP homelab_bridge_sensor_available 1 if the sensor was last published online.",
            "# TYPE homelab_bridge_sensor_available gauge",
        ]
        lines += [f'homelab_bridge_sensor_available{{sensor="{k}"}} {int(v)}' for k, v in self.available.items()]
        if self.ha_counts is not None:
            lines += ["# HELP homeassistant_entities Entity counts excluding bridge sensors.", "# TYPE homeassistant_entities gauge"]
            lines += [f'homeassistant_entities{{state="{state}"}} {count}' for state, count in self.ha_counts.items()]
        return ("\n".join(lines) + "\n").encode()

    def snapshot(self):
        with self.lock:
            self._metrics = self.render_metrics()
            self._healthy = self.healthy()

    def cached(self):
        with self.lock:
            return getattr(self, "_metrics", b""), self.healthy()


def make_handler(bridge):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            metrics, healthy = bridge.cached()
            if self.path == "/metrics":
                self._send(200, metrics, "text/plain; version=0.0.4")
            elif self.path == "/healthz":
                self._send(200 if healthy else 503, b"ok\n" if healthy else b"stale\n", "text/plain")
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


def _require_http_url(name, default=None):
    value = os.environ.get(name, default)
    parsed = urllib.parse.urlparse(value or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SystemExit(f"{name} must be an http(s) URL")
    return value


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    prom_url = _require_http_url(
        "PROM_URL", "http://prometheus-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090"
    )
    ha_url = _require_http_url("HASS_URL", "http://192.168.1.90:8123")
    token = os.environ.get("HASS_TOKEN", "").strip()
    if not token:
        raise SystemExit("HASS_TOKEN is not set")
    port = int(os.environ.get("LISTEN_PORT", "8080"))

    bridge = Bridge(Prometheus(prom_url), HomeAssistant(ha_url, token))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    bridge.snapshot()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), make_handler(bridge))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("serving /metrics and /healthz on :%d, %d sensors", port, len(SENSORS))

    backoff = 5
    while not stop.is_set():
        bridge.last_heartbeat = time.time()
        bridge.snapshot()
        if bridge.startup():
            log.info("discovery published, all sensors offline until first query")
            break
        log.warning("startup publish failed, retrying in %ds", backoff)
        stop.wait(backoff)
        backoff = min(backoff * 2, INTERVAL)

    last_online = None
    while not stop.is_set():
        bridge.cycle()
        bridge.snapshot()
        online = sum(bridge.available.values())
        if online != last_online:
            log.info("%d/%d sensors online", online, len(SENSORS))
            last_online = online
        stop.wait(max(0.0, INTERVAL - bridge.last_cycle_duration))

    # Best effort inside terminationGracePeriodSeconds; expire_after covers a miss.
    bridge.mark_all_offline(time.time() + 15)
    server.shutdown()
    log.info("stopped")


if __name__ == "__main__":
    main()
