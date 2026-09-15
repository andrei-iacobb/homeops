#!/usr/bin/env python3
"""Persist PetKit event images and expose a small, dashboard-friendly timeline."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import signal
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


LOG = logging.getLogger("petkit_snapshotter")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
SAFE_SLUG = re.compile(r"[^a-z0-9_-]+")

HA_URL = os.environ["HA_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", "/data"))
POLL_SECONDS = max(10, int(os.environ.get("POLL_SECONDS", "30")))
RETENTION_DAYS = max(1, int(os.environ.get("RETENTION_DAYS", "30")))
MAX_SNAPSHOTS = max(10, int(os.environ.get("MAX_SNAPSHOTS", "1000")))
TIME_ZONE = ZoneInfo(os.environ.get("TIME_ZONE", "Europe/London"))
PORT = int(os.environ.get("PORT", "8080"))

MANIFEST_PATH = STORAGE_DIR / "manifest.json"
SNAPSHOT_ROOT = STORAGE_DIR / "snapshots"

EVENTS = {
    "image.tommy_food_last_visit_event": {
        "device": "tommy",
        "device_label": "Tommy",
        "event": "visit",
        "event_label": "Visit",
    },
    "image.tommy_food_last_eat_event": {
        "device": "tommy",
        "device_label": "Tommy",
        "event": "eat",
        "event_label": "Eating",
    },
    "image.tommy_food_last_feed_event": {
        "device": "tommy",
        "device_label": "Tommy",
        "event": "feed",
        "event_label": "Feeding",
    },
    "image.downstairs_feeder_last_visit_event": {
        "device": "downstairs",
        "device_label": "Downstairs feeder",
        "event": "visit",
        "event_label": "Visit",
    },
    "image.downstairs_feeder_last_eat_event": {
        "device": "downstairs",
        "device_label": "Downstairs feeder",
        "event": "eat",
        "event_label": "Eating",
    },
    "image.downstairs_feeder_last_feed_event": {
        "device": "downstairs",
        "device_label": "Downstairs feeder",
        "event": "feed",
        "event_label": "Feeding",
    },
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def safe_slug(value: str) -> str:
    slug = SAFE_SLUG.sub("-", value.lower()).strip("-")
    return slug or "snapshot"


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or value in {"unknown", "unavailable", ""}:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TIME_ZONE)
    return parsed.astimezone(timezone.utc)


def format_event_time(value: str) -> str:
    parsed = parse_datetime(value)
    return parsed.isoformat(timespec="seconds") if parsed else value


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as temp:
        temp.write(data)
        temp.flush()
        os.fsync(temp.fileno())
        temp_path = Path(temp.name)
    os.replace(temp_path, path)


def fetch_json(path: str) -> Any:
    request = Request(
        f"{HA_URL}{path}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {HA_TOKEN}",
            "User-Agent": "petkit-snapshotter/1.0",
        },
    )
    with urlopen(request, timeout=20) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Home Assistant response exceeded the configured limit")
    return json.loads(body)


def fetch_image(image_path: str) -> bytes:
    # The path is taken from Home Assistant's image entity, but still constrain it
    # to the image proxy so a bad integration response cannot turn this into SSRF.
    parsed = urlparse(image_path)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/api/image_proxy/"):
        raise ValueError("Home Assistant returned an unexpected image path")
    request = Request(
        f"{HA_URL}{parsed.path}{('?' + parsed.query) if parsed.query else ''}",
        headers={
            "Accept": "image/*",
            "Authorization": f"Bearer {HA_TOKEN}",
            "User-Agent": "petkit-snapshotter/1.0",
        },
    )
    with urlopen(request, timeout=30) as response:
        content_type = response.headers.get_content_type()
        if not content_type.startswith("image/"):
            raise ValueError(f"Home Assistant returned {content_type}, not an image")
        body = response.read(MAX_IMAGE_BYTES + 1)
    if len(body) > MAX_IMAGE_BYTES:
        raise ValueError("PetKit image exceeded the configured limit")
    if not body:
        raise ValueError("PetKit returned an empty image")
    return body


def metric_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def snapshot_url(filename: str) -> str:
    base_url = PUBLIC_BASE_URL or ""
    return f"{base_url}/snapshots/{filename}"


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.up = 0
        self.ready = 0
        self.last_success = 0.0
        self.last_error = 0.0
        self.poll_failures = 0
        self.captured: dict[tuple[str, str], int] = {}
        self.fetch_failures: dict[str, int] = {entity: 0 for entity in EVENTS}

    def poll_ok(self) -> None:
        with self._lock:
            self.up = 1
            self.ready = 1
            self.last_success = time.time()

    def poll_failed(self) -> None:
        with self._lock:
            self.up = 0
            self.last_error = time.time()
            self.poll_failures += 1

    def capture(self, device: str, event: str) -> None:
        with self._lock:
            key = (device, event)
            self.captured[key] = self.captured.get(key, 0) + 1

    def fetch_failed(self, entity: str) -> None:
        with self._lock:
            self.fetch_failures[entity] = self.fetch_failures.get(entity, 0) + 1

    def render(self, store: "SnapshotStore") -> bytes:
        with self._lock:
            lines = [
                "# HELP petkit_snapshotter_up 1 while the last Home Assistant poll succeeded",
                "# TYPE petkit_snapshotter_up gauge",
                f"petkit_snapshotter_up {self.up}",
                "# HELP petkit_snapshotter_ready 1 after the first successful Home Assistant poll",
                "# TYPE petkit_snapshotter_ready gauge",
                f"petkit_snapshotter_ready {self.ready}",
                "# HELP petkit_snapshotter_last_success_timestamp_seconds Unix timestamp of the last successful poll",
                "# TYPE petkit_snapshotter_last_success_timestamp_seconds gauge",
                f"petkit_snapshotter_last_success_timestamp_seconds {self.last_success:.3f}",
                "# HELP petkit_snapshotter_last_error_timestamp_seconds Unix timestamp of the last failed poll",
                "# TYPE petkit_snapshotter_last_error_timestamp_seconds gauge",
                f"petkit_snapshotter_last_error_timestamp_seconds {self.last_error:.3f}",
                "# HELP petkit_snapshotter_poll_failures_total Failed Home Assistant polls",
                "# TYPE petkit_snapshotter_poll_failures_total counter",
                f"petkit_snapshotter_poll_failures_total {self.poll_failures}",
                "# HELP petkit_snapshotter_captured_total PetKit images captured by device and event",
                "# TYPE petkit_snapshotter_captured_total counter",
            ]
            for (device, event), value in sorted(self.captured.items()):
                lines.append(
                    f'petkit_snapshotter_captured_total{{device="{metric_label(device)}",event="{metric_label(event)}"}} {value}'
                )
            lines.extend(
                [
                    "# HELP petkit_snapshotter_fetch_failures_total Failed image fetches by Home Assistant entity",
                    "# TYPE petkit_snapshotter_fetch_failures_total counter",
                ]
            )
            for entity, value in sorted(self.fetch_failures.items()):
                lines.append(
                    f'petkit_snapshotter_fetch_failures_total{{entity="{metric_label(entity)}"}} {value}'
                )

        stats = store.stats()
        lines.extend(
            [
                "# HELP petkit_snapshotter_snapshots Current snapshots in the retained manifest",
                "# TYPE petkit_snapshotter_snapshots gauge",
                f"petkit_snapshotter_snapshots {stats['count']}",
                "# HELP petkit_snapshotter_storage_bytes Bytes used by retained snapshot JPEGs",
                "# TYPE petkit_snapshotter_storage_bytes gauge",
                f"petkit_snapshotter_storage_bytes {stats['bytes']}",
            ]
        )
        for device, count in sorted(stats["devices"].items()):
            lines.append(
                f'petkit_snapshotter_snapshots_by_device{{device="{metric_label(device)}"}} {count}'
            )
        return ("\n".join(lines) + "\n").encode()


class SnapshotStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.manifest: dict[str, Any] = {"version": 1, "updated_at": iso_now(), "snapshots": []}
        self.last_seen: dict[str, str] = {}
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not MANIFEST_PATH.exists():
            return
        try:
            loaded = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            snapshots = loaded.get("snapshots", [])
            if not isinstance(snapshots, list):
                raise ValueError("manifest snapshots is not a list")
            self.manifest = {
                "version": 1,
                "updated_at": loaded.get("updated_at", iso_now()),
                "snapshots": [item for item in snapshots if isinstance(item, dict)],
            }
            for item in self.manifest["snapshots"]:
                entity = item.get("source_entity")
                state = item.get("source_state")
                # The manifest is newest-first, so keep the first state per entity.
                # Otherwise an older retained event would be re-captured on restart.
                if isinstance(entity, str) and isinstance(state, str) and entity not in self.last_seen:
                    self.last_seen[entity] = state
        except (OSError, ValueError, json.JSONDecodeError) as error:
            LOG.warning("Ignoring unreadable manifest: %s", type(error).__name__)

    def _write_manifest(self) -> None:
        payload = json.dumps(self.manifest, indent=2, sort_keys=False).encode()
        atomic_write(MANIFEST_PATH, payload)

    def _prune_locked(self) -> None:
        cutoff = utc_now() - timedelta(days=RETENTION_DAYS)
        kept: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        for item in self.manifest["snapshots"]:
            captured_at = parse_datetime(item.get("captured_at"))
            if captured_at and captured_at < cutoff:
                removed.append(item)
            elif len(kept) < MAX_SNAPSHOTS:
                kept.append(item)
            else:
                removed.append(item)
        self.manifest["snapshots"] = kept
        referenced = {item.get("filename") for item in kept}
        for item in removed:
            filename = item.get("filename")
            if not isinstance(filename, str) or filename in referenced:
                continue
            candidate = (SNAPSHOT_ROOT / filename).resolve()
            try:
                candidate.relative_to(SNAPSHOT_ROOT.resolve())
            except ValueError:
                continue
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                LOG.warning("Could not prune snapshot file: %s", type(error).__name__)

    def add(
        self,
        *,
        entity: str,
        info: dict[str, str],
        source_state: str,
        image: bytes,
    ) -> None:
        event_at = parse_datetime(source_state)
        if event_at is None:
            raise ValueError("event image has no usable timestamp")
        digest = hashlib.sha256(image).hexdigest()
        stamp = event_at.astimezone(TIME_ZONE).strftime("%Y%m%d-%H%M%S")
        relative_dir = Path(info["device"])
        filename = f"{stamp}-{safe_slug(info['event'])}-{digest[:12]}.jpg"
        relative_path = relative_dir / filename
        destination = SNAPSHOT_ROOT / relative_path
        atomic_write(destination, image)
        url = snapshot_url(relative_path.as_posix())
        item = {
            "id": f"{info['device']}-{stamp}-{info['event']}-{digest[:12]}",
            "device": info["device"],
            "device_label": info["device_label"],
            "event": info["event"],
            "event_label": info["event_label"],
            "event_at": format_event_time(source_state),
            "captured_at": iso_now(),
            "source_entity": entity,
            "source_state": source_state,
            "filename": relative_path.as_posix(),
            "url": url,
            "sha256": digest,
        }
        with self._lock:
            self.manifest["snapshots"].insert(0, item)
            self.manifest["updated_at"] = iso_now()
            self.last_seen[entity] = source_state
            self._prune_locked()
            self._write_manifest()

    def already_seen(self, entity: str, source_state: str) -> bool:
        with self._lock:
            return self.last_seen.get(entity) == source_state

    def manifest_bytes(self) -> bytes:
        with self._lock:
            # Rebuild URLs on read so changing the private gateway prefix also
            # fixes snapshots already retained on the NFS volume.
            snapshots = []
            for item in self.manifest["snapshots"]:
                current = dict(item)
                filename = current.get("filename")
                if isinstance(filename, str):
                    current["url"] = snapshot_url(filename)
                snapshots.append(current)
            payload = dict(self.manifest)
            payload["snapshots"] = snapshots
            return json.dumps(payload, separators=(",", ":")).encode()

    def get_file(self, relative_path: str) -> Path | None:
        candidate = (SNAPSHOT_ROOT / relative_path).resolve()
        try:
            candidate.relative_to(SNAPSHOT_ROOT.resolve())
        except ValueError:
            return None
        if candidate.suffix.lower() != ".jpg" or not candidate.is_file():
            return None
        return candidate

    def stats(self) -> dict[str, Any]:
        with self._lock:
            devices: dict[str, int] = {}
            for item in self.manifest["snapshots"]:
                device = item.get("device", "unknown")
                devices[device] = devices.get(device, 0) + 1
            total_bytes = 0
            for item in self.manifest["snapshots"]:
                filename = item.get("filename")
                if not isinstance(filename, str):
                    continue
                path = self.get_file(filename)
                if path:
                    try:
                        total_bytes += path.stat().st_size
                    except OSError:
                        pass
            return {"count": len(self.manifest["snapshots"]), "bytes": total_bytes, "devices": devices}


class Collector:
    def __init__(self) -> None:
        self.store = SnapshotStore()
        self.metrics = Metrics()

    def poll_once(self) -> None:
        states = fetch_json("/api/states")
        if not isinstance(states, list):
            raise ValueError("Home Assistant states response was not a list")
        by_entity = {state.get("entity_id"): state for state in states if isinstance(state, dict)}
        for entity, info in EVENTS.items():
            state = by_entity.get(entity)
            if not state or not isinstance(state.get("state"), str):
                continue
            source_state = state["state"]
            if parse_datetime(source_state) is None:
                continue
            if self.store.already_seen(entity, source_state):
                continue
            image_path = (state.get("attributes") or {}).get("entity_picture")
            if not isinstance(image_path, str):
                continue
            try:
                image = fetch_image(image_path)
                self.store.add(entity=entity, info=info, source_state=source_state, image=image)
            except (HTTPError, URLError, OSError, ValueError) as error:
                self.metrics.fetch_failed(entity)
                LOG.warning("Could not capture %s: %s", entity, type(error).__name__)
                continue
            self.metrics.capture(info["device"], info["event"])

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.poll_once()
            except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
                self.metrics.poll_failed()
                LOG.warning("Home Assistant poll failed: %s", type(error).__name__)
            else:
                self.metrics.poll_ok()
            stop.wait(POLL_SECONDS)


class RequestHandler(BaseHTTPRequestHandler):
    server: "SnapshotHTTPServer"

    def _headers(self, content_type: str, length: int, cache_control: str = "no-store") -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = unquote(urlparse(self.path).path)
        if path in {"/", "/index.html"}:
            body = self.server.index_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self._headers(
                "text/html; charset=utf-8",
                len(body),
                "no-cache",
            )
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'self' https://hass.iacob.uk http://192.168.1.90:8123",
            )
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/manifest.json":
            self._send(HTTPStatus.OK, self.server.collector.store.manifest_bytes(), "application/json")
            return
        if path == "/metrics":
            self._send(HTTPStatus.OK, self.server.collector.metrics.render(self.server.collector.store), "text/plain; version=0.0.4")
            return
        if path in {"/healthz", "/readyz"}:
            ready = self.server.collector.metrics.ready == 1
            status = HTTPStatus.OK if path == "/healthz" or ready else HTTPStatus.SERVICE_UNAVAILABLE
            self._send(status, b"ok\n" if status == HTTPStatus.OK else b"waiting for Home Assistant\n", "text/plain; charset=utf-8")
            return
        if path.startswith("/snapshots/"):
            relative_path = path.removeprefix("/snapshots/")
            snapshot_path = self.server.collector.store.get_file(relative_path)
            if snapshot_path is None:
                self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
                return
            try:
                body = snapshot_path.read_bytes()
            except OSError:
                self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
                return
            content_type = mimetypes.guess_type(snapshot_path.name)[0] or "image/jpeg"
            self.send_response(HTTPStatus.OK)
            self._headers(content_type, len(body), "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(body)
            return
        self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class SnapshotHTTPServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], collector: Collector, index_path: Path) -> None:
        super().__init__(address, RequestHandler)
        self.collector = collector
        self.index_path = index_path


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    index_path = Path(os.environ.get("INDEX_PATH", "/app/index.html"))
    collector = Collector()
    stop = threading.Event()
    http = SnapshotHTTPServer(("0.0.0.0", PORT), collector, index_path)
    http_thread = threading.Thread(target=http.serve_forever, name="http", daemon=True)
    http_thread.start()

    def shutdown(_signum: int, _frame: Any) -> None:
        stop.set()
        http.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        collector.run(stop)
    finally:
        http.shutdown()
        http.server_close()


if __name__ == "__main__":
    main()
