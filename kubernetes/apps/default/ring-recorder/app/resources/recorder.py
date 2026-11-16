#!/usr/bin/env python3
"""Archive Ring recordings, analyse them, and serve a private event timeline."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import queue
import re
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, unquote, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


LOG = logging.getLogger("ring_recorder")

RING_API = "https://api.ring.com"
RING_OAUTH_URL = "https://oauth.ring.com/oauth/token"
RING_CLIENT_ID = "ring_official_android"
RING_USER_AGENT = "android:com.ringapp"
RING_API_VERSION = "11"

# ring-doorbell 0.9.13 endpoint names mirrored here:
# OAuth.ENDPOINT, NEW_SESSION_ENDPOINT, DEVICES_ENDPOINT,
# URL_DOORBELL_HISTORY, and URL_RECORDING_SHARE_PLAY.
# URL_RECORDING (/clients_api/dings/{id}/recording) is the alternative API,
# but Home Assistant's library uses share/play with disable_redirect=true.
SESSION_ENDPOINT = "/clients_api/session"
DEVICES_ENDPOINT = "/clients_api/ring_devices"
HISTORY_ENDPOINT = "/clients_api/doorbots/{device_id}/history"
RECORDING_ENDPOINT = "/clients_api/dings/{event_id}/share/play"

GEMINI_API = "https://generativelanguage.googleapis.com"
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MAX_CLIP_BYTES = 200 * 1024 * 1024
INLINE_CLIP_BYTES = 18 * 1024 * 1024
DOWNLOAD_CHUNK = 1024 * 1024
HISTORY_LIMIT = 50
ANALYSIS_SWEEP_SECONDS = 600
# Backlog drains a few clips per poll so new events are never stuck behind it.
DOWNLOADS_PER_POLL = 10
ANALYSIS_BACKOFF_SECONDS = 300
MAX_ANALYZE_CLIPS = 8
MAX_QUESTION_CHARS = 2000
NOT_READY_GIVE_UP = timedelta(hours=24)
AUTH_BACKOFF_SECONDS = 600
FILE_ACTIVE_TIMEOUT = 300
# The quick look runs on stills the moment a clip lands; the full pass watches the video.
# Backlog older than this only gets the quick look, which keeps Gemini usage low.
QUICK_ONLY_AFTER = timedelta(hours=48)
# Phone alerts only for events this fresh, so a backlog never pages anyone.
ALERT_MAX_AGE = timedelta(minutes=10)
FULL_UPDATE_MAX_AGE = timedelta(minutes=30)
CONTEXT_WINDOW = timedelta(minutes=5)
PRESENCE_LOOKBACK = timedelta(minutes=30)
PRESENCE_RETENTION = timedelta(days=30)
THUMB_WIDTH = 640
QUICK_FRAME_WIDTH = 512
QUICK_FRAME_SPACING = 2.0
DEFAULT_HERO_OFFSET = 3.0
THUMB_LINK_SECONDS = 7 * 24 * 3600
FFMPEG_TIMEOUT = 30
# Each decode of a 2K Ring clip costs tens of MB; a timeline full of uncached thumbnails
# must queue here instead of OOM-killing the pod.
FFMPEG_SLOTS = threading.BoundedSemaphore(2)
# A model that is out of quota or overloaded is skipped for a while instead of costing
# every clip a failed request first.
MODEL_COOLDOWN_SECONDS = 900
# Old clips are paced so the free tier's per-minute limit stays free for new ones.
BACKLOG_AGE = timedelta(hours=1)
BACKLOG_PACE_SECONDS = 12
SAFE_SLUG = re.compile(r"[^a-z0-9_-]+")
SAFE_EVENT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class AuthenticationError(RuntimeError):
    """Ring refresh credentials are no longer usable."""


class RecordingNotReady(RuntimeError):
    """The Ring recording is not ready to download yet."""


class GeminiError(RuntimeError):
    """transient=True means busy/quota/network: keep the clip queued and retry later."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient

    """A Gemini request failed or returned an unusable response."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def safe_slug(value: str) -> str:
    slug = SAFE_SLUG.sub("-", value.lower()).strip("-")
    return slug or "camera"


def read_limited(response: Any, limit: int = MAX_JSON_BYTES) -> bytes:
    body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError("response exceeded the configured limit")
    return body


def ring_detection(event: dict[str, Any], created: datetime) -> tuple[str | None, float | None]:
    """Ring's own classifier (human/motion/...) and seconds into the clip where it confirmed it."""
    cv = event.get("cv_properties")
    if not isinstance(cv, dict):
        return None, None
    detection = cv.get("detection_type") if isinstance(cv.get("detection_type"), str) else None
    hero: float | None = None
    entries = cv.get("detection_types") if isinstance(cv.get("detection_types"), list) else []
    for entry in entries:
        stamps = entry.get("verified_timestamps") if isinstance(entry, dict) else None
        for stamp in stamps if isinstance(stamps, list) else []:
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
                offset = stamp / 1000 - created.timestamp()
                if 0 <= offset <= 600 and (hero is None or offset < hero):
                    hero = offset
    return detection, hero


def extract_frame(ffmpeg: str, clip: Path, offset: float, width: int) -> bytes:
    """One JPEG frame; falls back to the first frame when the offset is past the end."""
    for start in (max(offset, 0.0), 0.0):
        with FFMPEG_SLOTS:
            result = subprocess.run(
                [
                    ffmpeg, "-nostdin", "-loglevel", "error", "-threads", "1",
                    "-ss", f"{start:.2f}", "-i", str(clip),
                    "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5",
                    "-f", "image2", "-c:v", "mjpeg", "-",
                ],
                capture_output=True,
                timeout=FFMPEG_TIMEOUT,
                check=False,
            )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    raise ValueError("ffmpeg could not extract a frame")


def load_scenes(path: Path) -> dict[str, str]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(key): str(value) for key, value in loaded.items() if isinstance(value, str)}


def sign_thumb(secret: str, event_id: str, expires: int) -> str:
    message = f"thumb:{event_id}:{expires}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def atomic_write_json(path: Path, value: Any, *, secret: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        if secret:
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        if secret:
            path.chmod(0o600)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def validate_download_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Ring returned an invalid recording URL")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return value
    if not address.is_global:
        raise ValueError("Ring returned a non-public recording URL")
    return value


def parse_range_header(value: str | None, size: int) -> tuple[int, int] | None:
    if value is None:
        return None
    if size <= 0 or not value.startswith("bytes=") or "," in value:
        raise ValueError("invalid range")
    raw = value[6:].strip()
    if "-" not in raw:
        raise ValueError("invalid range")
    start_text, end_text = raw.split("-", 1)
    if not start_text:
        if not end_text.isdigit():
            raise ValueError("invalid suffix range")
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("invalid suffix range")
        start = max(size - suffix, 0)
        return start, size - 1
    if not start_text.isdigit() or (end_text and not end_text.isdigit()):
        raise ValueError("invalid range")
    start = int(start_text)
    if start >= size:
        raise ValueError("range starts after the file")
    end = int(end_text) if end_text else size - 1
    if end < start:
        raise ValueError("range ends before it starts")
    return start, min(end, size - 1)


class Config:
    def __init__(self) -> None:
        self.storage_dir = Path(os.environ.get("STORAGE_DIR", "/data"))
        self.public_base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        self.poll_seconds = max(10, int(os.environ.get("POLL_SECONDS", "60")))
        # 0 keeps footage forever; Ring itself only keeps it for its plan's window.
        self.retention_days = int(os.environ.get("RETENTION_DAYS", "180"))
        if self.retention_days < 0:
            raise ValueError("RETENTION_DAYS must be 0 (forever) or a positive number of days")
        # Device slugs whose footage must never be sent to Gemini (indoor cameras).
        self.gemini_exclude = {
            slug.strip() for slug in os.environ.get("GEMINI_EXCLUDE_DEVICES", "").split(",") if slug.strip()
        }
        self.time_zone = ZoneInfo(os.environ.get("TIME_ZONE", "Europe/London"))
        self.gemini_model = os.environ.get("GEMINI_MODEL", "").strip()
        # Questions asked through /api/analyze use a stronger (slower) model list.
        self.gemini_ask_model = os.environ.get("GEMINI_ASK_MODEL", "").strip() or self.gemini_model
        self.gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.api_token = os.environ.get("API_TOKEN", "")
        self.ring_refresh_token = os.environ.get("RING_REFRESH_TOKEN", "").strip()
        self.ring_hardware_id = os.environ.get("RING_HARDWARE_ID", "").strip()
        self.port = int(os.environ.get("PORT", "8080"))
        self.index_path = Path(os.environ.get("INDEX_PATH", "/app/index.html"))
        self.card_path = Path(os.environ.get("CARD_PATH", "/app/ring-events-card.js"))
        # Hosts reachable from the internet. Every request on them needs a session
        # obtained by proving a valid Home Assistant login (POST /api/session).
        self.auth_hosts = {
            host.strip().lower() for host in os.environ.get("AUTH_HOSTS", "").split(",") if host.strip()
        }
        self.ha_url = os.environ.get("HA_URL", "").rstrip("/")
        # Outdoor cameras are polled this often so the quick look lands fast;
        # indoor ones keep POLL_SECONDS.
        self.fast_poll_seconds = max(5, int(os.environ.get("FAST_POLL_SECONDS", "15")))
        self.gemini_quick_model = os.environ.get("GEMINI_QUICK_MODEL", "").strip() or self.gemini_model
        self.ffmpeg = os.environ.get("FFMPEG", "ffmpeg")
        self.scenes = load_scenes(Path(os.environ.get("SCENES_PATH", "/app/scenes.json")))
        # Home Assistant webhook that turns quick looks into phone alerts (policy lives in HA).
        self.ha_webhook_url = os.environ.get("HA_WEBHOOK_URL", "").strip()
        # Public base for signed thumbnail links inside notifications.
        self.notify_base_url = os.environ.get("NOTIFY_BASE_URL", "").rstrip("/")
        # Cameras where any person counts as worth an alert (the doorbell).
        self.alert_devices = {
            slug.strip() for slug in os.environ.get("ALERT_DEVICES", "front-door").split(",") if slug.strip()
        }

    @property
    def database_path(self) -> Path:
        return self.storage_dir / "events.db"

    @property
    def token_path(self) -> Path:
        return self.storage_dir / "ring-token.json"

    @property
    def clips_dir(self) -> Path:
        return self.storage_dir / "clips"

    @property
    def thumbs_dir(self) -> Path:
        return self.storage_dir / "thumbs"


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.ready = 0
        self.clips_archived = 0
        self.analyses_ok = 0
        self.analyses_failed = 0
        self.last_successful_poll = 0.0
        self.token_refresh_failures = 0

    def set_ready(self, value: bool) -> None:
        with self._lock:
            self.ready = int(value)

    def poll_ok(self) -> None:
        with self._lock:
            self.ready = 1
            self.last_successful_poll = time.time()

    def archived(self) -> None:
        with self._lock:
            self.clips_archived += 1

    def analysis_ok(self) -> None:
        with self._lock:
            self.analyses_ok += 1

    def analysis_failed(self) -> None:
        with self._lock:
            self.analyses_failed += 1

    def token_failed(self) -> None:
        with self._lock:
            self.ready = 0
            self.token_refresh_failures += 1

    def render(self, store: "EventStore") -> bytes:
        with self._lock:
            values = {
                "ready": self.ready,
                "clips": self.clips_archived,
                "ok": self.analyses_ok,
                "failed": self.analyses_failed,
                "poll": self.last_successful_poll,
                "refresh": self.token_refresh_failures,
            }
        stats = store.stats()
        lines = [
            "# HELP ring_recorder_ready 1 when Ring polling is authenticated and healthy",
            "# TYPE ring_recorder_ready gauge",
            f"ring_recorder_ready {values['ready']}",
            "# HELP ring_recorder_clips_archived_total Clips archived since process start",
            "# TYPE ring_recorder_clips_archived_total counter",
            f"ring_recorder_clips_archived_total {values['clips']}",
            "# HELP ring_recorder_analyses_ok_total Successful Gemini analyses",
            "# TYPE ring_recorder_analyses_ok_total counter",
            f"ring_recorder_analyses_ok_total {values['ok']}",
            "# HELP ring_recorder_analyses_failed_total Failed Gemini analyses",
            "# TYPE ring_recorder_analyses_failed_total counter",
            f"ring_recorder_analyses_failed_total {values['failed']}",
            "# HELP ring_recorder_last_successful_poll_timestamp_seconds Last successful Ring poll",
            "# TYPE ring_recorder_last_successful_poll_timestamp_seconds gauge",
            f"ring_recorder_last_successful_poll_timestamp_seconds {values['poll']:.3f}",
            "# HELP ring_recorder_token_refresh_failures_total Failed Ring token refreshes",
            "# TYPE ring_recorder_token_refresh_failures_total counter",
            f"ring_recorder_token_refresh_failures_total {values['refresh']}",
            "# HELP ring_recorder_events Current retained events",
            "# TYPE ring_recorder_events gauge",
            f"ring_recorder_events {stats['events']}",
            "# HELP ring_recorder_clip_bytes Current retained clip bytes",
            "# TYPE ring_recorder_clip_bytes gauge",
            f"ring_recorder_clip_bytes {stats['bytes']}",
        ]
        return ("\n".join(lines) + "\n").encode()


class EventStore:
    def __init__(self, config: Config) -> None:
        self.config = config
        config.storage_dir.mkdir(parents=True, exist_ok=True)
        config.clips_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.config.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    device_name TEXT NOT NULL,
                    device_slug TEXT NOT NULL UNIQUE,
                    device_kind TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    device_name TEXT NOT NULL,
                    device_slug TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    duration REAL,
                    clip_path TEXT,
                    size INTEGER,
                    downloaded_at TEXT,
                    analysis TEXT,
                    analysis_status TEXT NOT NULL DEFAULT 'pending',
                    analysed_at TEXT,
                    recording_status TEXT NOT NULL DEFAULT 'pending',
                    recording_ready INTEGER NOT NULL DEFAULT 0,
                    recording_first_seen TEXT NOT NULL,
                    recording_error TEXT
                );
                CREATE INDEX IF NOT EXISTS events_created_at_idx ON events(created_at DESC);
                CREATE INDEX IF NOT EXISTS events_device_created_idx
                    ON events(device_slug, created_at DESC);
                CREATE INDEX IF NOT EXISTS events_analysis_status_idx ON events(analysis_status);
                CREATE INDEX IF NOT EXISTS events_recording_status_idx ON events(recording_status);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
            if "recording_ready" not in columns:
                connection.execute(
                    "ALTER TABLE events ADD COLUMN recording_ready INTEGER NOT NULL DEFAULT 0"
                )
            for name, ddl in (
                ("detection", "TEXT"),
                ("hero_offset", "REAL"),
                ("quick", "TEXT"),
                ("quick_status", "TEXT"),
                ("alerted", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE events ADD COLUMN {name} {ddl}")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS presence (
                    person TEXT NOT NULL,
                    state TEXT NOT NULL,
                    at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS presence_at_idx ON presence(at);
                CREATE INDEX IF NOT EXISTS events_quick_status_idx ON events(quick_status);
                """
            )

    def upsert_device(self, device: dict[str, str]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO devices(device_id, device_name, device_slug, device_kind, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    device_name=excluded.device_name,
                    device_slug=excluded.device_slug,
                    device_kind=excluded.device_kind,
                    updated_at=excluded.updated_at
                """,
                (
                    device["id"],
                    device["name"],
                    device["slug"],
                    device.get("kind"),
                    iso_utc(),
                ),
            )

    def event_exists(self, event_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return row is not None

    def event_archived(self, event_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT clip_path, recording_status FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return bool(row and (row["clip_path"] or row["recording_status"] == "expired"))

    def update_recording_readiness(self, event_id: str, event: dict[str, Any]) -> None:
        recording = event.get("recording")
        ready = isinstance(recording, dict) and recording.get("status") == "ready"
        created = parse_datetime(event.get("created_at"))
        detection, hero = ring_detection(event, created) if created else (None, None)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE events SET recording_ready = ?,
                    detection = COALESCE(?, detection), hero_offset = COALESCE(?, hero_offset)
                WHERE event_id = ?
                """,
                (int(ready), detection, hero, event_id),
            )

    def add_discovered_event(self, event: dict[str, Any], device: dict[str, str], disabled: bool) -> bool:
        created = parse_datetime(event.get("created_at"))
        if created is None:
            return False
        event_id = str(event.get("id", "")).strip()
        if not event_id or not SAFE_EVENT_ID.fullmatch(event_id):
            return False
        status = "disabled" if disabled else "pending"
        recording = event.get("recording")
        recording_ready = int(
            isinstance(recording, dict) and recording.get("status") == "ready"
        )
        detection, hero = ring_detection(event, created)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO events(
                    event_id, device_id, device_name, device_slug, kind, created_at,
                    duration, analysis_status, recording_status, recording_ready,
                    recording_first_seen, detection, hero_offset
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    event_id,
                    device["id"],
                    device["name"],
                    device["slug"],
                    str(event.get("kind") or "unknown"),
                    iso_utc(created),
                    float(event["duration"]) if isinstance(event.get("duration"), (int, float)) else None,
                    status,
                    recording_ready,
                    iso_utc(),
                    detection,
                    hero,
                ),
            )
        return cursor.rowcount > 0

    def pending_recordings(self) -> list[sqlite3.Row]:
        cutoff = iso_utc(utc_now() - NOT_READY_GIVE_UP)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE recording_status = 'pending' AND recording_ready = 1
                    AND recording_first_seen >= ?
                ORDER BY created_at DESC
                """,
                (cutoff,),
            ).fetchall()
        return list(rows)

    def pending_recording_ids(self, device_id: str) -> set[str]:
        cutoff = iso_utc(utc_now() - NOT_READY_GIVE_UP)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id FROM events
                WHERE device_id = ? AND recording_status = 'pending'
                    AND recording_first_seen >= ?
                """,
                (device_id, cutoff),
            ).fetchall()
        return {str(row["event_id"]) for row in rows}

    def mark_recording_expired(self) -> int:
        cutoff = iso_utc(utc_now() - NOT_READY_GIVE_UP)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE events SET recording_status = 'expired', recording_error = 'not ready after 24 hours'
                WHERE recording_status = 'pending' AND recording_first_seen < ?
                    AND (recording_ready = 0 OR recording_error = 'not ready')
                """,
                (cutoff,),
            )
        return cursor.rowcount

    def mark_recording_error(self, event_id: str, message: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE events SET recording_error = ? WHERE event_id = ?",
                (message[:500], event_id),
            )

    def mark_downloaded(self, event_id: str, relative_path: str, size: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE events SET clip_path = ?, size = ?, downloaded_at = ?,
                    recording_status = 'ready', recording_error = NULL
                WHERE event_id = ?
                """,
                (relative_path, size, iso_utc(), event_id),
            )

    def mark_analysis(self, event_id: str, status: str, analysis: dict[str, Any] | None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE events SET analysis_status = ?, analysis = ?, analysed_at = ?
                WHERE event_id = ?
                """,
                (status, json.dumps(analysis) if analysis is not None else None, iso_utc(), event_id),
            )

    def exclude_devices(self, slugs: set[str]) -> None:
        """Stop queued analysis for excluded devices; nothing of theirs is sent again."""
        if not slugs:
            return
        marks = ",".join("?" for _ in slugs)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE events SET analysis_status = 'excluded' "
                f"WHERE analysis_status = 'pending' AND device_slug IN ({marks})",
                tuple(sorted(slugs)),
            )

    def requeue_analysis(self, event_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE events SET analysis_status = 'pending', analysis = NULL, analysed_at = NULL
                WHERE event_id = ? AND clip_path IS NOT NULL
                """,
                (event_id,),
            )
        return cursor.rowcount > 0

    def pending_analyses(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id FROM events
                WHERE clip_path IS NOT NULL AND analysis_status = 'pending'
                    AND quick_status IS NOT NULL
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [str(row["event_id"]) for row in rows]

    def pending_quick(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id FROM events
                WHERE clip_path IS NOT NULL AND analysis_status = 'pending'
                    AND quick_status IS NULL
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [str(row["event_id"]) for row in rows]

    def set_quick(
        self, event_id: str, status: str, quick: dict[str, Any] | None, *, alerted: bool = False
    ) -> None:
        # One write: the full-pass sweep sees quick_status and alerted together.
        with self.connect() as connection:
            connection.execute(
                "UPDATE events SET quick_status = ?, quick = ?, alerted = MAX(alerted, ?) WHERE event_id = ?",
                (status, json.dumps(quick) if quick is not None else None, int(alerted), event_id),
            )

    def nearby_events(self, row: sqlite3.Row, window: timedelta) -> list[sqlite3.Row]:
        created = parse_datetime(row["created_at"])
        if created is None:
            return []
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM events
                    WHERE created_at >= ? AND created_at <= ? AND event_id != ?
                    ORDER BY created_at LIMIT 12
                    """,
                    (iso_utc(created - window), iso_utc(created + window), row["event_id"]),
                ).fetchall()
            )

    def add_presence(self, person: str, state: str, at: datetime) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO presence(person, state, at) VALUES (?, ?, ?)", (person, state, iso_utc(at))
            )

    def presence_between(self, start: datetime, end: datetime) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM presence WHERE at >= ? AND at <= ? ORDER BY at LIMIT 20",
                    (iso_utc(start), iso_utc(end)),
                ).fetchall()
            )

    def thumb_for_event(self, event_id: str) -> Path | None:
        """Thumbnail path mirroring the clip path; None when there is no clip."""
        row = self.get_row(event_id)
        if row is None or not row["clip_path"]:
            return None
        root = self.config.thumbs_dir.resolve()
        candidate = (self.config.thumbs_dir / str(row["clip_path"])).with_suffix(".jpg").resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    def get_row(self, event_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()

    def clip_for_event(self, event_id: str) -> Path | None:
        row = self.get_row(event_id)
        if row is None or not row["clip_path"]:
            return None
        root = self.config.clips_dir.resolve()
        candidate = (self.config.clips_dir / str(row["clip_path"])).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        if candidate.suffix.lower() != ".mp4" or not candidate.is_file():
            return None
        return candidate

    def devices(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.device_id, d.device_name, d.device_slug, d.device_kind,
                    COUNT(e.event_id) AS event_count
                FROM devices d LEFT JOIN events e ON e.device_id = d.device_id
                GROUP BY d.device_id ORDER BY d.device_name COLLATE NOCASE
                """
            ).fetchall()
        return [
            {
                "id": row["device_id"],
                "name": row["device_name"],
                "slug": row["device_slug"],
                "kind": row["device_kind"],
                "event_count": row["event_count"],
            }
            for row in rows
        ]

    def query_events(
        self,
        *,
        device: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        notable: bool = False,
        event_ids: Iterable[str] | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        values: list[Any] = []
        if device:
            clauses.append("device_slug = ?")
            values.append(device)
        if since:
            clauses.append("created_at >= ?")
            values.append(iso_utc(since))
        if until:
            clauses.append("created_at < ?")
            values.append(iso_utc(until))
        if notable:
            clauses.append("json_extract(analysis, '$.notable') = 1")
        if event_ids is not None:
            ids = list(event_ids)
            if not ids:
                return []
            clauses.append(f"event_id IN ({','.join('?' for _ in ids)})")
            values.extend(ids)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(limit, 500)))
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM events {where} ORDER BY created_at DESC LIMIT ?", values
            ).fetchall()
        return list(rows)

    def row_to_api(self, row: sqlite3.Row) -> dict[str, Any]:
        created = parse_datetime(row["created_at"])
        analysis: dict[str, Any] = {}
        if row["analysis"]:
            try:
                loaded = json.loads(row["analysis"])
                if isinstance(loaded, dict):
                    analysis = loaded
            except json.JSONDecodeError:
                pass
        clip_url = None
        if row["clip_path"]:
            clip_url = f"{self.config.public_base_url}/clips/{quote(str(row['event_id']))}.mp4"
        quick: dict[str, Any] | None = None
        if row["quick"]:
            try:
                loaded_quick = json.loads(row["quick"])
                quick = loaded_quick if isinstance(loaded_quick, dict) else None
            except json.JSONDecodeError:
                quick = None
        item = {
            "thumb_url": (
                f"{self.config.public_base_url}/thumbs/{quote(str(row['event_id']))}.jpg"
                if row["clip_path"] else None
            ),
            "quick": quick,
            "detection": row["detection"],
            "id": row["event_id"],
            "device": row["device_slug"],
            "device_name": row["device_name"],
            "kind": row["kind"],
            "created_at": iso_utc(created) if created else row["created_at"],
            "local_time": created.astimezone(self.config.time_zone).isoformat(timespec="seconds") if created else None,
            "duration": row["duration"],
            "clip_url": clip_url,
            "size": row["size"],
            "analysis": analysis or None,
            "status": row["analysis_status"],
            "recording_status": row["recording_status"],
        }
        item.update(analysis)
        return item

    def prune(self, now: datetime | None = None) -> int:
        if not self.config.retention_days:
            with self.connect() as connection:
                connection.execute(
                    "DELETE FROM presence WHERE at < ?",
                    (iso_utc((now or utc_now()) - PRESENCE_RETENTION),),
                )
            return 0
        cutoff = iso_utc((now or utc_now()) - timedelta(days=self.config.retention_days))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT event_id, clip_path FROM events WHERE created_at < ?", (cutoff,)
            ).fetchall()
            for row in rows:
                relative = row["clip_path"]
                if not relative:
                    continue
                root = self.config.clips_dir.resolve()
                candidate = (self.config.clips_dir / str(relative)).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError:
                    continue
                candidate.unlink(missing_ok=True)
                thumb = (self.config.thumbs_dir / str(relative)).with_suffix(".jpg")
                thumb.unlink(missing_ok=True)
            connection.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
            connection.execute(
                "DELETE FROM presence WHERE at < ?",
                (iso_utc((now or utc_now()) - PRESENCE_RETENTION),),
            )
        self._remove_empty_clip_dirs()
        return len(rows)

    def _remove_empty_clip_dirs(self) -> None:
        directories = [*self.config.clips_dir.rglob("*"), *self.config.thumbs_dir.rglob("*")]
        for directory in sorted(directories, reverse=True):
            if directory.is_dir():
                try:
                    directory.rmdir()
                except OSError:
                    pass

    def stats(self) -> dict[str, int]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS events, COALESCE(SUM(size), 0) AS bytes FROM events"
            ).fetchone()
        return {"events": int(row["events"]), "bytes": int(row["bytes"])}


class RingClient:
    def __init__(self, config: Config, metrics: Metrics) -> None:
        self.config = config
        self.metrics = metrics
        self.access_token = ""
        self.refresh_token = self._load_refresh_token()
        self.session_created = False

    def _load_refresh_token(self) -> str:
        try:
            loaded = json.loads(self.config.token_path.read_text(encoding="utf-8"))
            token = loaded.get("refresh_token") if isinstance(loaded, dict) else None
            if isinstance(token, str) and token:
                return token
            LOG.error("Persisted Ring token has no refresh_token, using the environment seed")
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as error:
            LOG.error("Persisted Ring token is unreadable: %s", type(error).__name__)
        return self.config.ring_refresh_token

    def refresh(self) -> None:
        if not self.refresh_token:
            self.metrics.token_failed()
            raise AuthenticationError("RING_REFRESH_TOKEN is empty")
        data = urlencode(
            {"grant_type": "refresh_token", "refresh_token": self.refresh_token}
        ).encode()
        basic = base64.b64encode(f"{RING_CLIENT_ID}:".encode()).decode()
        request = Request(
            RING_OAUTH_URL,
            data=data,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Authorization": f"Basic {basic}",
                # Ring sits behind Cloudflare, which rejects the default
                # Python-urllib agent with error 1010 (HTTP 403).
                "User-Agent": RING_USER_AGENT,
                "hardware_id": self.config.ring_hardware_id,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(read_limited(response))
        except HTTPError as error:
            body = error.read(MAX_JSON_BYTES).decode("utf-8", "replace")
            if error.code == 401 or "invalid_grant" in body:
                self.metrics.token_failed()
                raise AuthenticationError("Ring refresh token was rejected") from error
            raise
        if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
            self.metrics.token_failed()
            raise AuthenticationError("Ring refresh response did not contain an access token")
        rotated = payload.get("refresh_token")
        if not isinstance(rotated, str) or not rotated:
            self.metrics.token_failed()
            raise AuthenticationError("Ring refresh response did not contain a refresh token")
        atomic_write_json(self.config.token_path, payload, secret=True)
        self.access_token = payload["access_token"]
        self.refresh_token = rotated

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> Any:
        if not self.access_token:
            self.refresh()
        url = f"{RING_API}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        body = json_bytes(payload) if payload is not None else None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": RING_USER_AGENT,
            "hardware_id": self.config.ring_hardware_id,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                raw = read_limited(response)
        except HTTPError as error:
            if error.code == 401 and retry_auth:
                self.refresh()
                return self._request(
                    path, method=method, params=params, payload=payload, retry_auth=False
                )
            raise
        return json.loads(raw) if raw else None

    def create_session(self) -> None:
        if not self.config.ring_hardware_id:
            raise AuthenticationError("RING_HARDWARE_ID is empty")
        payload = {
            "device": {
                "hardware_id": self.config.ring_hardware_id,
                "metadata": {
                    "api_version": RING_API_VERSION,
                    "device_model": f"ring-doorbell:{RING_USER_AGENT}",
                },
                "os": "android",
            }
        }
        self._request(SESSION_ENDPOINT, method="POST", payload=payload)
        self.session_created = True

    def devices(self) -> list[dict[str, str]]:
        if not self.session_created:
            self.create_session()
        payload = self._request(DEVICES_ENDPOINT)
        if not isinstance(payload, dict):
            raise ValueError("Ring devices response was not an object")
        result: list[dict[str, str]] = []
        used_slugs: set[str] = set()
        for group in ("doorbots", "authorized_doorbots", "stickup_cams"):
            devices = payload.get(group, [])
            if not isinstance(devices, list):
                continue
            for raw in devices:
                if not isinstance(raw, dict) or raw.get("id") is None:
                    continue
                name = str(raw.get("description") or raw.get("device_name") or raw["id"])
                base_slug = safe_slug(name)
                slug = base_slug
                suffix = 2
                while slug in used_slugs:
                    slug = f"{base_slug}-{suffix}"
                    suffix += 1
                used_slugs.add(slug)
                result.append(
                    {
                        "id": str(raw["id"]),
                        "name": name,
                        "slug": slug,
                        "kind": str(raw.get("kind") or group.rstrip("s")),
                    }
                )
        return result

    def history(self, device_id: str, older_than: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": HISTORY_LIMIT}
        if older_than:
            params["older_than"] = older_than
        payload = self._request(
            HISTORY_ENDPOINT.format(device_id=quote(device_id, safe="")), params=params
        )
        if not isinstance(payload, list):
            raise ValueError("Ring history response was not a list")
        return [item for item in payload if isinstance(item, dict)]

    def recording_url(self, event_id: str) -> str:
        payload = self._request(
            RECORDING_ENDPOINT.format(event_id=quote(event_id, safe="")),
            params={"disable_redirect": "true"},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("url"), str) or not payload["url"]:
            raise RecordingNotReady("Ring recording URL is not ready")
        return validate_download_url(payload["url"])


class GeminiClient:
    RESPONSE_SCHEMA = {
        "type": "OBJECT",
        "properties": {
            "label": {"type": "STRING"},
            "summary": {"type": "STRING"},
            "detail": {"type": "STRING"},
            "people": {"type": "INTEGER"},
            "vehicles": {"type": "INTEGER"},
            "animals": {"type": "ARRAY", "items": {"type": "STRING"}},
            "packages": {"type": "BOOLEAN"},
            "activity_tags": {"type": "ARRAY", "items": {"type": "STRING"}},
            "notable": {"type": "BOOLEAN"},
            "confidence": {"type": "NUMBER"},
            "persons": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "description": {"type": "STRING"},
                        "role": {
                            "type": "STRING",
                            "enum": ["resident", "visitor", "delivery", "passer-by", "worker", "unknown"],
                        },
                        "action": {"type": "STRING"},
                    },
                    "required": ["description", "role", "action"],
                },
            },
        },
        "required": [
            "persons",
            "label",
            "summary",
            "detail",
            "people",
            "vehicles",
            "animals",
            "packages",
            "activity_tags",
            "notable",
            "confidence",
        ],
    }

    QUICK_SCHEMA = {
        "type": "OBJECT",
        "properties": {
            "headline": {"type": "STRING"},
            "label": {"type": "STRING"},
            "people": {"type": "ARRAY", "items": {"type": "STRING"}},
            "vehicles": {"type": "INTEGER"},
            "notable": {"type": "BOOLEAN"},
            "nothing_there": {"type": "BOOLEAN"},
        },
        "required": ["headline", "label", "people", "vehicles", "notable", "nothing_there"],
    }

    def __init__(self, config: Config) -> None:
        self.config = config
        self._cooldown: dict[str, float] = {}
        self._cooldown_lock = threading.Lock()

    def _request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: Any = None,
        headers: dict[str, str] | None = None,
        retries: bool = True,
    ) -> tuple[Any, Any]:
        request_headers = {"x-goog-api-key": self.config.gemini_api_key}
        if headers:
            request_headers.update(headers)
        body = None if payload is None else json_bytes(payload)
        if body is not None and "Content-Type" not in request_headers:
            request_headers["Content-Type"] = "application/json"
        attempts = 5 if retries else 1
        for attempt in range(attempts):
            request = Request(url, data=body, headers=request_headers, method=method)
            try:
                with urlopen(request, timeout=300) as response:
                    raw = read_limited(response)
                    return (json.loads(raw) if raw else None), response.headers
            except HTTPError as error:
                if error.code != 429 and error.code < 500:
                    detail = error.read(4096).decode("utf-8", "replace")
                    raise GeminiError(f"Gemini HTTP {error.code}: {detail[:300]}") from error
                if attempt == attempts - 1:
                    raise GeminiError(
                        f"Gemini HTTP {error.code} after {attempts} attempts", transient=True
                    ) from error
            except URLError as error:
                if attempt == attempts - 1:
                    raise GeminiError(
                        f"Gemini network error after {attempts} attempts", transient=True
                    ) from error
            time.sleep(min(2**attempt, 16))
        raise GeminiError("Gemini request failed")

    def _upload_file(self, path: Path) -> tuple[str, str]:
        size = path.stat().st_size
        start_payload = {"file": {"display_name": path.name}}
        _, headers = self._request_json(
            f"{GEMINI_API}/upload/v1beta/files",
            method="POST",
            payload=start_payload,
            headers={
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": "video/mp4",
            },
        )
        upload_url = headers.get("X-Goog-Upload-URL")
        if not upload_url:
            raise GeminiError("Gemini Files API did not return an upload URL")
        with path.open("rb") as stream:
            data = stream.read()
        uploaded = None
        for attempt in range(5):
            request = Request(
                upload_url,
                data=data,
                headers={
                    "x-goog-api-key": self.config.gemini_api_key,
                    "Content-Type": "video/mp4",
                    "Content-Length": str(size),
                    "X-Goog-Upload-Offset": "0",
                    "X-Goog-Upload-Command": "upload, finalize",
                },
                method="POST",
            )
            try:
                with urlopen(request, timeout=300) as response:
                    uploaded = json.loads(read_limited(response))
                break
            except HTTPError as error:
                if (error.code != 429 and error.code < 500) or attempt == 4:
                    raise GeminiError(
                        f"Gemini file upload HTTP {error.code} after {attempt + 1} attempts"
                    ) from error
            except URLError as error:
                if attempt == 4:
                    raise GeminiError("Gemini file upload network error after 5 attempts") from error
            time.sleep(min(2**attempt, 16))
        file_info = uploaded.get("file") if isinstance(uploaded, dict) else None
        if not isinstance(file_info, dict) or not isinstance(file_info.get("name"), str):
            raise GeminiError("Gemini Files API returned no file name")
        name = file_info["name"]
        deadline = time.monotonic() + FILE_ACTIVE_TIMEOUT
        try:
            while True:
                current, _ = self._request_json(f"{GEMINI_API}/v1beta/{name}")
                state = current.get("state") if isinstance(current, dict) else None
                state_name = state.get("name") if isinstance(state, dict) else state
                if state_name == "ACTIVE":
                    uri = current.get("uri")
                    if not isinstance(uri, str):
                        raise GeminiError("Gemini active file had no URI")
                    return name, uri
                if state_name == "FAILED":
                    raise GeminiError("Gemini file processing failed")
                if time.monotonic() >= deadline:
                    raise GeminiError("Gemini file processing timed out")
                time.sleep(2)
        except Exception:
            self._delete_file(name)
            raise

    def _delete_file(self, name: str) -> None:
        try:
            self._request_json(f"{GEMINI_API}/v1beta/{name}", method="DELETE", retries=False)
        except (GeminiError, HTTPError, URLError):
            LOG.warning("Could not delete temporary Gemini file %s", name)

    def _video_parts(self, clips: list[tuple[Path, str]]) -> tuple[list[dict[str, Any]], list[str]]:
        parts: list[dict[str, Any]] = []
        uploaded: list[str] = []
        inline_bytes = 0
        try:
            for path, label in clips:
                parts.append({"text": label})
                size = path.stat().st_size
                # json.dumps and UTF-8 encoding each create another request copy.
                # Keep the whole request to one clip's inline memory budget.
                if size < INLINE_CLIP_BYTES and inline_bytes + size < INLINE_CLIP_BYTES:
                    parts.append(
                        {
                            "inline_data": {
                                "mime_type": "video/mp4",
                                "data": base64.b64encode(path.read_bytes()).decode(),
                            }
                        }
                    )
                    inline_bytes += size
                else:
                    name, uri = self._upload_file(path)
                    uploaded.append(name)
                    parts.append(
                        {"file_data": {"mime_type": "video/mp4", "file_uri": uri}}
                    )
        except Exception:
            for name in uploaded:
                self._delete_file(name)
            raise
        return parts, uploaded

    def _generate(
        self,
        contents: list[dict[str, Any]],
        generation_config: dict[str, Any],
        model_list: str | None = None,
    ) -> str:
        model_list = model_list if model_list is not None else self.config.gemini_model
        if not self.config.gemini_api_key:
            raise GeminiError("GEMINI_API_KEY is empty")
        if not model_list:
            raise GeminiError("GEMINI_MODEL is empty")
        payload = {"contents": contents, "generationConfig": generation_config}
        # GEMINI_MODEL is a comma-separated preference list; fall through to the
        # next model only when the current one is busy or out of quota.
        listed = [m.strip() for m in model_list.split(",") if m.strip()]
        now = time.monotonic()
        with self._cooldown_lock:
            models = [m for m in listed if self._cooldown.get(m, 0.0) <= now]
        if not models:
            raise GeminiError("every Gemini model is cooling down", transient=True)
        response = None
        for index, model in enumerate(models):
            try:
                response, _ = self._request_json(
                    f"{GEMINI_API}/v1beta/models/{quote(model, safe='')}:generateContent",
                    method="POST",
                    payload=payload,
                    # Only the last model gets the slow retry loop; a busy
                    # earlier model falls through after one attempt.
                    retries=index == len(models) - 1,
                )
                break
            except GeminiError as error:
                if error.transient:
                    with self._cooldown_lock:
                        self._cooldown[model] = time.monotonic() + MODEL_COOLDOWN_SECONDS
                if not error.transient or index == len(models) - 1:
                    raise
                LOG.warning("Gemini model %s unavailable, trying %s", model, models[index + 1])
        try:
            return response["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as error:
            raise GeminiError("Gemini response contained no text") from error

    def _scene(self, event: dict[str, Any]) -> str:
        scene = self.config.scenes.get(str(event.get("device") or ""))
        return scene or "No layout notes for this camera."

    def quick_look(self, frames: list[bytes], event: dict[str, Any]) -> dict[str, Any]:
        """Fast first look from a few stills around Ring's detection moment."""
        local_time = event.get("local_time") or event.get("created_at")
        device = event.get("device_name") or event.get("device")
        kind = event.get("kind") or "motion"
        detection = event.get("detection") or "not classified"
        prompt = (
            f"{len(frames)} stills in time order, {QUICK_FRAME_SPACING:.0f} s apart, from a Ring {kind} alert "
            f"on the {device} camera at {local_time} (UK). Ring's detector said: {detection}.\n"
            f"Camera layout: {self._scene(event)}\n"
            "Say where people are, not which way they are going, unless the stills leave no doubt (stills "
            "are a poor guide to direction). headline: max 12 words, present tense, who + what + where, e.g. "
            "'Two men on the path by the front door', 'Courier at the door holding a parcel', 'Car in the "
            "parking area'. "
            "label: 1 to 3 words for a timeline chip. people: one short visual description per person "
            "(clothing, what they carry). Never guess identity, and only say delivery when a parcel or courier "
            "uniform is visible. vehicles: moving or arriving vehicles only. nothing_there: true only if no "
            "person, animal or moving vehicle is visible. notable: true only for clear warning signs (face "
            "hidden, trying doors or windows, looking into cars, taking a parcel); ordinary arrivals, visitors "
            "and deliveries are not notable."
        )
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for frame in frames:
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(frame).decode()}})
        text = self._generate(
            [{"role": "user", "parts": parts}],
            {
                "responseMimeType": "application/json",
                "responseSchema": self.QUICK_SCHEMA,
                "mediaResolution": "MEDIA_RESOLUTION_LOW",
            },
            self.config.gemini_quick_model,
        )
        try:
            result = json.loads(text)
        except json.JSONDecodeError as error:
            raise GeminiError("Gemini quick look was invalid JSON") from error
        if not isinstance(result, dict):
            raise GeminiError("Gemini quick look was not an object")
        return result

    def analyse_clip(self, path: Path, event: dict[str, Any], context: str = "") -> dict[str, Any]:
        local_time = event.get("local_time") or event.get("created_at")
        device = event.get("device_name") or event.get("device")
        kind = event.get("kind") or "unknown"
        detection = event.get("detection") or "not classified"
        # The quick look is not passed on: stills cannot show direction, and a wrong
        # "walking towards the street" talked the full pass out of what the video showed.
        prompt = (
            f"You review Ring recordings for a family home in the UK. This clip is a {kind} event on the "
            f"{device} camera starting {local_time}. Ring's detector said: {detection}.\n"
            f"Camera layout (use it to work out where people go): {self._scene(event)}\n"
            f"Other camera triggers around this time (facts, no descriptions):\n"
            f"{context or '- nothing else recorded'}\n"
            "\n"
            "Watch the whole clip and track each person from entry to exit. Decide where they came from and "
            "where they went using the layout, especially whether they went into or came out of the house. "
            "Anyone who opens the door and walks in, comes out of the house, or comes home through the back "
            "gate from the parking area is a resident unless something contradicts it; residents coming and "
            "going are routine. Indoor motion right after someone reaches the door means they came in. "
            "Phone presence is weak support only and never a reason to name someone.\n"
            "label: 1 to 3 words, sentence case, for a timeline chip (e.g. 'Arriving home', 'Leaving home', "
            "'Parcel delivered', 'Visitor at door', 'Passer-by', 'Car arrives', 'Cat in garden', "
            "'No one visible').\n"
            "summary: one plain sentence a person would say, under 20 words, naming the outcome.\n"
            "detail: 2 to 3 sentences on the sequence with rough timings, visible facts only.\n"
            "persons: one entry per person with role and what they did. people and vehicles are counts. "
            "activity_tags are short words such as delivery, visitor, passer-by, car, cat, family, resident, "
            "unknown person or nothing.\n"
            "notable: true only for unknown people lingering, trying doors or windows, looking into cars, "
            "taking parcels, or anyone unexpected at night. Residents and deliveries are not notable."
        )
        parts, uploaded = self._video_parts([(path, "Recording to analyse")])
        try:
            text = self._generate(
                [{"role": "user", "parts": [{"text": prompt}, *parts]}],
                {
                    "responseMimeType": "application/json",
                    "responseSchema": self.RESPONSE_SCHEMA,
                    # A fraction of the tokens per frame; enough to follow people on these cameras.
                    "mediaResolution": "MEDIA_RESOLUTION_LOW",
                },
            )
            result = json.loads(text)
            if not isinstance(result, dict):
                raise GeminiError("Gemini structured response was not an object")
            return result
        except json.JSONDecodeError as error:
            raise GeminiError("Gemini structured response was invalid JSON") from error
        finally:
            for name in uploaded:
                self._delete_file(name)

    def answer_question(
        self, clips: list[tuple[Path, dict[str, Any]]], question: str
    ) -> str:
        labelled = [
            (
                path,
                f"Clip {index}: {event.get('device_name') or event.get('device')}, "
                f"local time {event.get('local_time')}, kind {event.get('kind')}",
            )
            for index, (path, event) in enumerate(clips, start=1)
        ]
        parts, uploaded = self._video_parts(labelled)
        prompt = (
            "Answer the user's question from the selected Ring clips. This is a UK "
            "house. Use only evidence visible in the clips, distinguish the labelled "
            "clips, and say when the evidence is uncertain. "
            f"Question: {question}"
        )
        try:
            return self._generate(
                [{"role": "user", "parts": [{"text": prompt}, *parts]}],
                {},
                self.config.gemini_ask_model,
            ).strip()
        finally:
            for name in uploaded:
                self._delete_file(name)


class Recorder:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.metrics = Metrics()
        self.store = EventStore(self.config)
        self.ring = RingClient(self.config, self.metrics)
        self.gemini = GeminiClient(self.config)
        # Newest first, so a backlog never delays today's clips.
        self.analysis_queue: queue.PriorityQueue[tuple[float, str]] = queue.PriorityQueue()
        self.quick_queue: queue.PriorityQueue[tuple[float, str]] = queue.PriorityQueue()
        self.store.exclude_devices(self.config.gemini_exclude)
        self._devices: list[dict[str, str]] = []
        self._last_full_poll = 0.0

    def _priority(self, event_id: str) -> float:
        row = self.store.get_row(event_id)
        created = parse_datetime(row["created_at"]) if row else None
        return -created.timestamp() if created else 0.0

    def enqueue_analysis(self, event_id: str) -> None:
        self.analysis_queue.put((self._priority(event_id), event_id))

    def enqueue_quick(self, event_id: str) -> None:
        self.quick_queue.put((self._priority(event_id), event_id))

    def hero_offset(self, row: sqlite3.Row) -> float:
        if row["hero_offset"] is not None:
            return float(row["hero_offset"])
        if row["kind"] == "ding":
            return 1.0
        duration = float(row["duration"] or 0)
        return min(DEFAULT_HERO_OFFSET, duration / 2) if duration else DEFAULT_HERO_OFFSET

    def ensure_thumb(self, event_id: str) -> Path | None:
        """Hero-frame JPEG for the timeline, made on first use and kept beside the clip."""
        thumb = self.store.thumb_for_event(event_id)
        if thumb is None or thumb.is_file():
            return thumb
        clip = self.store.clip_for_event(event_id)
        row = self.store.get_row(event_id)
        if clip is None or row is None:
            return None
        try:
            frame = extract_frame(self.config.ffmpeg, clip, self.hero_offset(row), THUMB_WIDTH)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            LOG.warning("Could not make thumbnail for %s: %s", event_id, type(error).__name__)
            return None
        thumb.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{thumb.name}.", suffix=".tmp", dir=thumb.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(frame)
            os.replace(temporary, thumb)
        except OSError:
            Path(temporary).unlink(missing_ok=True)
            return None
        return thumb

    def thumb_link(self, event_id: str) -> str | None:
        """Signed, expiring thumbnail URL a phone can fetch without a browser session."""
        if not self.config.notify_base_url or not self.config.api_token:
            return None
        expires = int(time.time()) + THUMB_LINK_SECONDS
        signature = sign_thumb(self.config.api_token, event_id, expires)
        return f"{self.config.notify_base_url}/thumbs/{quote(event_id)}.jpg?exp={expires}&sig={signature}"

    def notify(self, row: sqlite3.Row, stage: str, label: str, message: str, notable: bool) -> None:
        if not self.config.ha_webhook_url:
            return
        created = parse_datetime(row["created_at"])
        payload = {
            "event_id": row["event_id"],
            # One notification per camera: the full pass replaces the quick one, which
            # replaces Home Assistant's instant doorbell alert.
            "tag": f"ring-{row['device_slug']}",
            "stage": stage,
            "camera": row["device_name"],
            "camera_slug": row["device_slug"],
            "kind": row["kind"],
            "time": created.astimezone(self.config.time_zone).strftime("%H:%M") if created else "",
            "label": label,
            "message": message,
            "notable": notable,
            "image": self.thumb_link(str(row["event_id"])),
        }
        request = Request(
            self.config.ha_webhook_url,
            data=json_bytes(payload),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10):
                pass
        except (HTTPError, URLError, OSError) as error:
            LOG.warning("Home Assistant webhook failed: %s", type(error).__name__)

    def should_alert(self, row: sqlite3.Row, quick: dict[str, Any]) -> bool:
        if row["kind"] == "ding" or quick.get("notable") is True:
            return True
        people = quick.get("people") if isinstance(quick.get("people"), list) else []
        return row["device_slug"] in self.config.alert_devices and (bool(people) or row["detection"] == "human")

    @staticmethod
    def analysis_from_quick(quick: dict[str, Any]) -> dict[str, Any]:
        people = quick.get("people") if isinstance(quick.get("people"), list) else []
        nothing = quick.get("nothing_there") is True
        return {
            "label": "No one visible" if nothing else str(quick.get("label") or "Motion"),
            "summary": str(quick.get("headline") or ""),
            "detail": "",
            "people": len(people),
            "vehicles": int(quick.get("vehicles") or 0) if isinstance(quick.get("vehicles"), int) else 0,
            "activity_tags": ["nothing"] if nothing else [],
            "notable": quick.get("notable") is True,
            "persons": [{"description": str(person), "role": "unknown", "action": ""} for person in people],
            "source": "quick",
        }

    def quick_event(self, event_id: str) -> str | None:
        row = self.store.get_row(event_id)
        if row is None or row["quick_status"] is not None or row["analysis_status"] != "pending":
            return None
        if row["device_slug"] in self.config.gemini_exclude:
            self.store.set_quick(event_id, "skipped", None)
            return None
        clip = self.store.clip_for_event(event_id)
        created = parse_datetime(row["created_at"])
        if clip is None or created is None:
            return None
        age = utc_now() - created
        hero = self.hero_offset(row)
        try:
            frames = [
                extract_frame(self.config.ffmpeg, clip, hero + step * QUICK_FRAME_SPACING, QUICK_FRAME_WIDTH)
                for step in (-1, 0, 1)
            ]
            event = self.store.row_to_api(row)
            quick = self.gemini.quick_look(frames, event)
        except GeminiError as error:
            if error.transient and age > QUICK_ONLY_AFTER:
                LOG.warning("Gemini quick look busy (%s); backing off", error)
                return "backoff"
            return self._quick_failed(event_id, age, str(error))
        except (OSError, ValueError, subprocess.TimeoutExpired, HTTPError, URLError) as error:
            return self._quick_failed(event_id, age, type(error).__name__)
        alert = age <= ALERT_MAX_AGE and self.should_alert(row, quick)
        self.store.set_quick(event_id, "ok", quick, alerted=alert)
        if alert:
            self.notify(
                row, "quick", str(quick.get("label") or "Motion"), str(quick.get("headline") or ""),
                quick.get("notable") is True,
            )
        # Stills miss small or fast movement (night on the high cameras), so an empty quick
        # look never skips the full pass; only the old backlog stops at the quick look.
        if age > QUICK_ONLY_AFTER:
            self.store.mark_analysis(event_id, "ok", self.analysis_from_quick(quick))
            self.metrics.analysis_ok()
        else:
            self.enqueue_analysis(event_id)
        return "backlog" if age > BACKLOG_AGE else None

    def _quick_failed(self, event_id: str, age: timedelta, reason: str) -> str | None:
        LOG.warning("Quick look failed for %s: %s", event_id, reason)
        self.store.set_quick(event_id, "failed", None)
        if age > QUICK_ONLY_AFTER:
            # Backlog never earns a full pass.
            self.store.mark_analysis(event_id, "failed", {"error": reason[:500]})
            self.metrics.analysis_failed()
            return "backlog"
        self.enqueue_analysis(event_id)
        return "backlog" if age > BACKLOG_AGE else None

    def deep_context(self, row: sqlite3.Row) -> str:
        lines: list[str] = []
        zone = self.config.time_zone
        # Facts only. Other clips' AI descriptions are left out so one wrong description
        # cannot talk the next one into the same mistake.
        for other in self.store.nearby_events(row, CONTEXT_WINDOW):
            when = parse_datetime(other["created_at"])
            stamp = when.astimezone(zone).strftime("%H:%M:%S") if when else "?"
            what = str(other["kind"])
            if other["detection"] == "human":
                what += ", Ring saw a person"
            if other["device_slug"] in self.config.gemini_exclude:
                what += " (indoor camera)"
            lines.append(f"- {stamp} {other['device_name']}: {what}")
        created = parse_datetime(row["created_at"])
        if created:
            for entry in self.store.presence_between(created - PRESENCE_LOOKBACK, created + CONTEXT_WINDOW):
                when = parse_datetime(entry["at"])
                stamp = when.astimezone(zone).strftime("%H:%M") if when else "?"
                lines.append(f"- {stamp} {entry['person']}'s phone reported {entry['state']}")
        return "\n".join(lines)

    def history_events(self, device: dict[str, str]) -> list[dict[str, Any]]:
        discovered: list[dict[str, Any]] = []
        older_than: str | None = None
        pending_ids = self.store.pending_recording_ids(device["id"])
        backfilling = True
        while True:
            page = self.ring.history(device["id"], older_than)
            if not page:
                break
            for event in page:
                event_id = str(event.get("id", ""))
                if not event_id:
                    continue
                if event_id in pending_ids:
                    self.store.update_recording_readiness(event_id, event)
                    pending_ids.discard(event_id)
                if not backfilling:
                    continue
                if self.store.event_exists(event_id):
                    # Ring history is newest first: the first known event means
                    # everything older is already in the DB.
                    self.store.update_recording_readiness(event_id, event)
                    backfilling = False
                    continue
                if self.store.add_discovered_event(
                    event,
                    device,
                    disabled=not bool(self.config.gemini_api_key)
                    or device["slug"] in self.config.gemini_exclude,
                ):
                    discovered.append(event)
            if not backfilling:
                break
            last_id = str(page[-1].get("id", ""))
            if not last_id or last_id == older_than:
                break
            older_than = last_id
            time.sleep(1)
        return discovered

    def _download(self, event_id: str, destination: Path) -> int:
        url = self.ring.recording_url(event_id)
        request = Request(url, headers={"User-Agent": RING_USER_AGENT})
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary_path = Path(temporary)
        total = 0
        try:
            with os.fdopen(descriptor, "wb") as output, urlopen(request, timeout=180) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_CLIP_BYTES:
                    raise ValueError("Ring recording exceeded 200 MB")
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_CLIP_BYTES:
                        raise ValueError("Ring recording exceeded 200 MB")
                    output.write(chunk)
                if total == 0:
                    raise ValueError("Ring returned an empty recording")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, destination)
            return total
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary_path.unlink(missing_ok=True)
            raise

    def process_pending_recordings(self) -> None:
        expired = self.store.mark_recording_expired()
        if expired:
            LOG.warning("Gave up on %d Ring recordings that stayed unready for 24 hours", expired)
        for row in self.store.pending_recordings()[:DOWNLOADS_PER_POLL]:
            event_id = str(row["event_id"])
            created = parse_datetime(row["created_at"])
            day = created.astimezone(self.config.time_zone).date().isoformat() if created else "unknown"
            relative = Path(str(row["device_slug"])) / day / f"{event_id}.mp4"
            destination = self.config.clips_dir / relative
            try:
                size = self._download(event_id, destination)
            except RecordingNotReady:
                self.store.mark_recording_error(event_id, "not ready")
                continue
            except (HTTPError, URLError, OSError, ValueError) as error:
                self.store.mark_recording_error(event_id, type(error).__name__)
                LOG.warning("Could not archive Ring event %s: %s", event_id, type(error).__name__)
                continue
            self.store.mark_downloaded(event_id, relative.as_posix(), size)
            self.metrics.archived()
            self.ensure_thumb(event_id)
            if self.config.gemini_api_key and row["device_slug"] not in self.config.gemini_exclude:
                self.enqueue_quick(event_id)

    def poll_once(self) -> None:
        try:
            failures = self._poll_histories()
        finally:
            self.process_pending_recordings()
        if failures:
            raise ValueError(f"{failures} camera histories failed")
        self.metrics.poll_ok()

    def _poll_histories(self) -> int:
        now = time.monotonic()
        full = not self._devices or now - self._last_full_poll >= self.config.poll_seconds
        if full:
            try:
                devices = self.ring.devices()
            except AuthenticationError:
                raise
            except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError):
                if not self._devices:
                    raise
                LOG.warning("Ring device list failed; keeping the cached cameras")
            else:
                self._devices = devices
                # Auth works and the API answers: serve the archive while the (possibly
                # hours-long) first backfill is still downloading.
                self.metrics.set_ready(True)
                for device in devices:
                    self.store.upsert_device(device)
            self._last_full_poll = now
        failures = 0
        for device in self._devices:
            if not (full or device["slug"] not in self.config.gemini_exclude):
                continue
            try:
                self.history_events(device)
            except AuthenticationError:
                raise
            except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
                failures += 1
                LOG.warning("Ring history for %s failed: %s", device["slug"], type(error).__name__)
        return failures

    def polling_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            delay = min(self.config.fast_poll_seconds, self.config.poll_seconds)
            try:
                self.poll_once()
            except AuthenticationError as error:
                self.metrics.set_ready(False)
                LOG.critical("Ring authentication failed: %s. Backing off for 10 minutes.", error)
                delay = AUTH_BACKOFF_SECONDS
            except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
                self.metrics.set_ready(False)
                detail = f" {error.code} {urlparse(error.url or '').path}" if isinstance(error, HTTPError) else ""
                LOG.warning("Ring poll failed: %s%s", type(error).__name__, detail)
            stop.wait(delay)

    def quick_loop(self, stop: threading.Event) -> None:
        next_sweep = 0.0
        while not stop.is_set():
            if time.monotonic() >= next_sweep:
                for event_id in self.store.pending_quick():
                    self.enqueue_quick(event_id)
                next_sweep = time.monotonic() + ANALYSIS_SWEEP_SECONDS
            try:
                _, event_id = self.quick_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                result = self.quick_event(event_id)
                if result == "backlog":
                    stop.wait(BACKLOG_PACE_SECONDS)
                elif result == "backoff":
                    while True:
                        try:
                            self.quick_queue.get_nowait()
                            self.quick_queue.task_done()
                        except queue.Empty:
                            break
                    next_sweep = time.monotonic() + ANALYSIS_BACKOFF_SECONDS
                    stop.wait(ANALYSIS_BACKOFF_SECONDS)
            except Exception:  # keep the worker alive; the sweep retries later
                LOG.exception("Quick look crashed for %s", event_id)
            finally:
                self.quick_queue.task_done()

    def analysis_loop(self, stop: threading.Event) -> None:
        # Pending rows are re-swept every ANALYSIS_SWEEP_SECONDS so clips left
        # pending by a busy/quota error are retried without a restart.
        next_sweep = 0.0
        while not stop.is_set():
            if time.monotonic() >= next_sweep:
                for event_id in self.store.pending_analyses():
                    self.enqueue_analysis(event_id)
                next_sweep = time.monotonic() + ANALYSIS_SWEEP_SECONDS
            try:
                _, event_id = self.analysis_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                result = self.analyse_event(event_id)
                if result == "backlog":
                    stop.wait(BACKLOG_PACE_SECONDS)
                if result == "backoff":
                    # Drain the queue; the next sweep refills it newest first.
                    while True:
                        try:
                            self.analysis_queue.get_nowait()
                            self.analysis_queue.task_done()
                        except queue.Empty:
                            break
                    next_sweep = time.monotonic() + ANALYSIS_BACKOFF_SECONDS
                    stop.wait(ANALYSIS_BACKOFF_SECONDS)
            finally:
                self.analysis_queue.task_done()

    def analyse_event(self, event_id: str) -> str | None:
        row = self.store.get_row(event_id)
        path = self.store.clip_for_event(event_id)
        if row is None or path is None or row["analysis_status"] != "pending":
            return None
        if row["device_slug"] in self.config.gemini_exclude:
            self.store.mark_analysis(event_id, "excluded", None)
            return None
        if not self.config.gemini_api_key:
            self.store.mark_analysis(event_id, "disabled", None)
            return
        event = self.store.row_to_api(row)
        created = parse_datetime(row["created_at"])
        paced = "backlog" if created is None or utc_now() - created > BACKLOG_AGE else None
        try:
            analysis = self.gemini.analyse_clip(path, event, self.deep_context(row))
        except GeminiError as error:
            if error.transient:
                LOG.warning("Gemini busy or out of quota (%s); backing off", error)
                return "backoff"
            self.store.mark_analysis(event_id, "failed", {"error": str(error)[:500]})
            self.metrics.analysis_failed()
            LOG.warning("Gemini analysis failed for %s: %s", event_id, error)
            return paced
        except (HTTPError, URLError, OSError, ValueError) as error:
            self.store.mark_analysis(event_id, "failed", {"error": str(error)[:500]})
            self.metrics.analysis_failed()
            LOG.warning("Gemini analysis failed for %s: %s", event_id, type(error).__name__)
            return paced
        self.store.mark_analysis(event_id, "ok", analysis)
        self.metrics.analysis_ok()
        current = self.store.get_row(event_id)
        if current is not None and current["alerted"] and created and utc_now() - created <= FULL_UPDATE_MAX_AGE:
            self.notify(
                row, "full", str(analysis.get("label") or "Motion"), str(analysis.get("summary") or ""),
                analysis.get("notable") is True,
            )
        return paced

    def retention_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                removed = self.store.prune()
                if removed:
                    LOG.info("Pruned %d expired Ring events", removed)
            except (OSError, sqlite3.Error) as error:
                LOG.warning("Retention prune failed: %s", type(error).__name__)
            stop.wait(3600)


SESSION_COOKIE = "rr_session"
# Short and renewable: the card trades a fresh HA token for a new session on any 401,
# so an HA logout or token revocation stops clip access within the hour.
SESSION_TTL_SECONDS = 3600
MAX_SESSIONS = 500
# Rejected HA token checks would look like brute force to Home Assistant (ip_ban) and come
# from this pod, so cap them per client and overall.
LOGIN_FAILURE_LIMIT = 5
LOGIN_GLOBAL_FAILURE_LIMIT = 30
LOGIN_FAILURE_WINDOW_SECONDS = 300


class SessionStore:
    """In-memory browser sessions for AUTH_HOSTS. A restart logs everyone out; the card re-auths."""

    def __init__(self) -> None:
        self._sessions: dict[str, float] = {}
        self._failures: list[tuple[float, str]] = []
        self._inflight: dict[str, int] = {}
        self._lock = threading.Lock()

    def reserve_login(self, client: str) -> bool:
        """Atomically claim a login attempt; in-flight checks count as potential failures."""
        cutoff = time.monotonic() - LOGIN_FAILURE_WINDOW_SECONDS
        with self._lock:
            self._failures = [entry for entry in self._failures if entry[0] > cutoff]
            mine = sum(1 for _, who in self._failures if who == client) + self._inflight.get(client, 0)
            total = len(self._failures) + sum(self._inflight.values())
            if mine >= LOGIN_FAILURE_LIMIT or total >= LOGIN_GLOBAL_FAILURE_LIMIT:
                return False
            self._inflight[client] = self._inflight.get(client, 0) + 1
            return True

    def finish_login(self, client: str, *, rejected: bool) -> None:
        with self._lock:
            left = self._inflight.get(client, 1) - 1
            if left > 0:
                self._inflight[client] = left
            else:
                self._inflight.pop(client, None)
            if rejected:
                self._failures.append((time.monotonic(), client))

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def create(self) -> str:
        token = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        now = time.monotonic()
        with self._lock:
            self._sessions = {key: expiry for key, expiry in self._sessions.items() if expiry > now}
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=self._sessions.__getitem__)
                del self._sessions[oldest]
            self._sessions[token] = now + SESSION_TTL_SECONDS
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            expiry = self._sessions.get(token)
            if expiry is None:
                return False
            if expiry <= time.monotonic():
                del self._sessions[token]
                return False
            return True


def validate_ha_token(ha_url: str, token: str) -> str:
    """'ok' when Home Assistant accepts the bearer token, 'rejected' on 401/403, else 'error'."""
    if not ha_url or not token or len(token) > 4096:
        return "rejected"
    request = Request(f"{ha_url}/api/", headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=10) as response:
            return "ok" if response.status == 200 else "error"
    except HTTPError as error:
        return "rejected" if error.code in (401, 403) else "error"
    except (URLError, OSError, TimeoutError) as error:
        LOG.warning("Home Assistant token check failed: %s", type(error).__name__)
        return "error"


class RingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], recorder: Recorder) -> None:
        super().__init__(address, RequestHandler)
        self.recorder = recorder
        self.sessions = SessionStore()


class RequestHandler(BaseHTTPRequestHandler):
    server: RingHTTPServer

    def _headers(
        self,
        content_type: str,
        length: int,
        *,
        cache_control: str = "no-store",
        cors: bool = True,
    ) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str = "application/json; charset=utf-8",
        *,
        cache_control: str = "no-store",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self._headers(content_type, len(body), cache_control=cache_control)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, value: Any) -> None:
        self._send(status, json_bytes(value))

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def _authorized(self) -> bool:
        expected = self.server.recorder.config.api_token
        if not expected:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "POST API is disabled")
            return False
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        supplied = header[len(prefix) :] if header.startswith(prefix) else ""
        if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False
        return True

    def _on_auth_host(self) -> bool:
        host = self.headers.get("Host", "").rsplit(":", 1)[0].strip().lower()
        return host in self.server.recorder.config.auth_hosts

    def _session_token(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return ""

    def _logged_in(self) -> bool:
        """Gate for AUTH_HOSTS; LAN hosts stay open as before."""
        if not self._on_auth_host() or self.server.sessions.valid(self._session_token()):
            return True
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "login required"})
        return False

    def _create_session(self) -> None:
        header = self.headers.get("Authorization", "")
        supplied = header[len("Bearer ") :] if header.startswith("Bearer ") else ""
        sessions = self.server.sessions
        if not supplied:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        # Cloudflare sets CF-Connecting-IP; the public route is only reachable through its tunnel.
        client = self.headers.get("CF-Connecting-IP", "").strip() or self.client_address[0]
        if not sessions.reserve_login(client):
            self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "too many failed logins, try later"})
            return
        result = "error"
        try:
            result = validate_ha_token(self.server.recorder.config.ha_url, supplied)
        finally:
            sessions.finish_login(client, rejected=result == "rejected")
        if result != "ok":
            status = HTTPStatus.UNAUTHORIZED if result == "rejected" else HTTPStatus.BAD_GATEWAY
            self._send_json(status, {"error": "unauthorized" if result == "rejected" else "home assistant unavailable"})
            return
        sessions.revoke(self._session_token())
        token = sessions.create()
        cookie = (
            f"{SESSION_COOKIE}={token}; Path=/ring-events; Max-Age={SESSION_TTL_SECONDS}; "
            "HttpOnly; Secure; SameSite=Strict"
        )
        self._send(HTTPStatus.OK, json_bytes({"ok": True}), extra_headers={"Set-Cookie": cookie})

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid content length")
            return None
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is empty or too large")
            return None
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid JSON")
            return None
        if not isinstance(value, dict):
            self._error(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
            return None
        return value

    def _parse_time(self, value: str | None, name: str) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            raise ValueError(f"invalid {name}")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.server.recorder.config.time_zone)
        return parsed.astimezone(timezone.utc)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if path in {"/", "/index.html"} and not self._logged_in():
            return
        if path in {"/", "/index.html"}:
            self._serve_asset(
                self.server.recorder.config.index_path,
                "text/html; charset=utf-8",
                extra_headers={
                    "Content-Security-Policy": (
                        "default-src 'self'; media-src 'self'; style-src 'self' 'unsafe-inline'; "
                        "script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
                        "frame-ancestors 'self' https://hass.iacob.uk "
                        "https://hass.iacob.co.uk http://192.168.1.90:8123"
                    )
                },
            )
        elif path == "/card.js":
            self._serve_asset(
                self.server.recorder.config.card_path,
                "text/javascript; charset=utf-8",
                cache_control="public, max-age=300",
            )
        elif path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
        elif path == "/readyz":
            ready = bool(self.server.recorder.metrics.ready)
            self._send_json(
                HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
                {"ready": ready},
            )
        elif path.startswith("/thumbs/") and path.endswith(".jpg") and self._signed_thumb(path, query):
            self._thumb(path.removeprefix("/thumbs/").removesuffix(".jpg"))
        elif not self._logged_in():
            return
        elif path.startswith("/thumbs/") and path.endswith(".jpg"):
            self._thumb(path.removeprefix("/thumbs/").removesuffix(".jpg"))
        elif path == "/metrics":
            self._send(
                HTTPStatus.OK,
                self.server.recorder.metrics.render(self.server.recorder.store),
                "text/plain; version=0.0.4; charset=utf-8",
            )
        elif path == "/api/events":
            self._events(query)
        elif path == "/api/devices":
            self._send_json(HTTPStatus.OK, {"devices": self.server.recorder.store.devices()})
        elif path == "/api/summary":
            self._summary(query)
        elif path.startswith("/api/events/"):
            event_id = path.removeprefix("/api/events/")
            self._event(event_id)
        elif path.startswith("/clips/") and path.endswith(".mp4"):
            event_id = path.removeprefix("/clips/").removesuffix(".mp4")
            self._clip(event_id)
        else:
            self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        if path == "/api/session":
            self._create_session()
            return
        if path == "/api/presence":
            # Machine-to-machine from Home Assistant: the API token alone is enough, even on
            # the public host, because this endpoint only appends a home/away row.
            if self._authorized():
                payload = self._read_json()
                if payload is not None:
                    self._presence(payload)
            return
        if not self._logged_in() or not self._authorized():
            return
        if path == "/api/analyze":
            payload = self._read_json()
            if payload is not None:
                self._analyze(payload)
            return
        if path.startswith("/api/events/") and path.endswith("/reanalyze"):
            event_id = path.removeprefix("/api/events/").removesuffix("/reanalyze").rstrip("/")
            self._reanalyze(event_id)
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def _serve_asset(
        self,
        path: Path,
        content_type: str,
        cache_control: str = "no-cache",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "asset not found")
            return
        self._send(
            HTTPStatus.OK,
            body,
            content_type,
            cache_control=cache_control,
            extra_headers=extra_headers,
        )

    def _events(self, query: dict[str, list[str]]) -> None:
        try:
            limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
            since = self._parse_time(query.get("since", [None])[0], "since")
            until = self._parse_time(query.get("until", [None])[0], "until")
        except (ValueError, TypeError) as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
            return
        rows = self.server.recorder.store.query_events(
            device=query.get("device", [None])[0],
            since=since,
            until=until,
            limit=limit,
            notable=query.get("notable", ["0"])[0] == "1",
        )
        self._send_json(
            HTTPStatus.OK,
            {"events": [self.server.recorder.store.row_to_api(row) for row in rows]},
        )

    def _event(self, event_id: str) -> None:
        if not SAFE_EVENT_ID.fullmatch(event_id):
            self._error(HTTPStatus.NOT_FOUND, "event not found")
            return
        row = self.server.recorder.store.get_row(event_id)
        if row is None:
            self._error(HTTPStatus.NOT_FOUND, "event not found")
            return
        self._send_json(HTTPStatus.OK, self.server.recorder.store.row_to_api(row))

    def _summary(self, query: dict[str, list[str]]) -> None:
        raw = query.get("date", [date.today().isoformat()])[0]
        try:
            local_day = date.fromisoformat(raw)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "date must use YYYY-MM-DD")
            return
        zone = self.server.recorder.config.time_zone
        start = datetime.combine(local_day, datetime.min.time(), zone).astimezone(timezone.utc)
        end = (datetime.combine(local_day, datetime.min.time(), zone) + timedelta(days=1)).astimezone(
            timezone.utc
        )
        rows = self.server.recorder.store.query_events(since=start, until=end, limit=500)
        events = []
        for row in rows:
            item = self.server.recorder.store.row_to_api(row)
            events.append(
                {
                    "id": item["id"],
                    "device": item["device"],
                    "device_name": item["device_name"],
                    "kind": item["kind"],
                    "local_time": item["local_time"],
                    "summary": item.get("summary"),
                    "notable": item.get("notable", False),
                    "activity_tags": item.get("activity_tags", []),
                }
            )
        self._send_json(HTTPStatus.OK, {"date": raw, "events": events})

    def _signed_thumb(self, path: str, query: dict[str, list[str]]) -> bool:
        secret = self.server.recorder.config.api_token
        expires_text = query.get("exp", [""])[0]
        supplied = query.get("sig", [""])[0]
        if (
            not secret or not supplied or len(expires_text) > 12 or not expires_text.isdigit()
            or int(expires_text) < time.time()
        ):
            return False
        event_id = path.removeprefix("/thumbs/").removesuffix(".jpg")
        expected = sign_thumb(secret, event_id, int(expires_text))
        return hmac.compare_digest(supplied.encode(), expected.encode())

    def _thumb(self, event_id: str) -> None:
        if not SAFE_EVENT_ID.fullmatch(event_id):
            self._error(HTTPStatus.NOT_FOUND, "thumbnail not found")
            return
        thumb = self.server.recorder.ensure_thumb(event_id)
        if thumb is None:
            self._error(HTTPStatus.NOT_FOUND, "thumbnail not found")
            return
        self._send(HTTPStatus.OK, thumb.read_bytes(), "image/jpeg", cache_control="private, max-age=86400")

    def _presence(self, payload: dict[str, Any]) -> None:
        person = payload.get("person")
        state = payload.get("state")
        if not isinstance(person, str) or not isinstance(state, str) or not person.strip() or not state.strip():
            self._error(HTTPStatus.BAD_REQUEST, "person and state are required")
            return
        self.server.recorder.store.add_presence(person.strip()[:64], state.strip()[:32], utc_now())
        self._send_json(HTTPStatus.OK, {"ok": True})

    def _clip(self, event_id: str) -> None:
        if not SAFE_EVENT_ID.fullmatch(event_id):
            self._error(HTTPStatus.NOT_FOUND, "clip not found")
            return
        path = self.server.recorder.store.clip_for_event(event_id)
        if path is None:
            self._error(HTTPStatus.NOT_FOUND, "clip not found")
            return
        size = path.stat().st_size
        try:
            selected = parse_range_header(self.headers.get("Range"), size)
        except ValueError:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self._headers("video/mp4", 0, cache_control="private, max-age=86400")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        start, end = selected if selected is not None else (0, size - 1)
        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if selected is not None else HTTPStatus.OK)
        self._headers("video/mp4", length, cache_control="private, max-age=86400")
        self.send_header("Accept-Ranges", "bytes")
        if selected is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(DOWNLOAD_CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _select_analyze_rows(self, payload: dict[str, Any]) -> list[sqlite3.Row] | None:
        event_ids = payload.get("event_ids")
        if event_ids is not None:
            if not isinstance(event_ids, list) or not all(isinstance(item, (str, int)) for item in event_ids):
                self._error(HTTPStatus.BAD_REQUEST, "event_ids must be a list")
                return None
            if not event_ids or len(event_ids) > MAX_ANALYZE_CLIPS:
                self._error(HTTPStatus.BAD_REQUEST, "event_ids must contain 1 to 8 values")
                return None
            ids = [str(item) for item in event_ids]
            return self.server.recorder.store.query_events(
                event_ids=ids, limit=MAX_ANALYZE_CLIPS
            )
        try:
            since = self._parse_time(payload.get("since"), "since")
            until = self._parse_time(payload.get("until"), "until")
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
            return None
        return self.server.recorder.store.query_events(
            device=str(payload["device"]) if payload.get("device") else None,
            since=since,
            until=until,
            limit=MAX_ANALYZE_CLIPS,
        )

    def _without_excluded(self, rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
        excluded = self.server.recorder.config.gemini_exclude
        return [row for row in rows if row["device_slug"] not in excluded]

    def _analyze(self, payload: dict[str, Any]) -> None:
        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            self._error(HTTPStatus.BAD_REQUEST, "question is required")
            return
        if len(question) > MAX_QUESTION_CHARS:
            self._error(HTTPStatus.BAD_REQUEST, "question exceeds 2000 characters")
            return
        rows = self._select_analyze_rows(payload)
        if rows is None:
            return
        rows = self._without_excluded(rows)
        if not rows:
            self._error(HTTPStatus.BAD_REQUEST, "no outdoor clips matched (indoor cameras are never sent to Gemini)")
            return
        clips: list[tuple[Path, dict[str, Any]]] = []
        for row in rows:
            event_id = str(row["event_id"])
            path = self.server.recorder.store.clip_for_event(event_id)
            if path is not None:
                clips.append((path, self.server.recorder.store.row_to_api(row)))
        if not clips:
            self._error(HTTPStatus.BAD_REQUEST, "no archived clips matched")
            return
        try:
            answer = self.server.recorder.gemini.answer_question(clips, question.strip())
        except (GeminiError, HTTPError, URLError, OSError, ValueError) as error:
            self._error(HTTPStatus.BAD_GATEWAY, str(error)[:500])
            return
        self._send_json(
            HTTPStatus.OK,
            {"answer": answer, "event_ids": [event["id"] for _, event in clips]},
        )

    def _reanalyze(self, event_id: str) -> None:
        if not SAFE_EVENT_ID.fullmatch(event_id):
            self._error(HTTPStatus.NOT_FOUND, "event not found")
            return
        if not self.server.recorder.config.gemini_api_key:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Gemini analysis is disabled")
            return
        if not self.server.recorder.store.requeue_analysis(event_id):
            self._error(HTTPStatus.NOT_FOUND, "event or clip not found")
            return
        self.server.recorder.enqueue_analysis(event_id)
        self._send_json(HTTPStatus.ACCEPTED, {"event_id": event_id, "status": "pending"})

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("http %s - %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    recorder = Recorder()
    stop = threading.Event()
    server = RingHTTPServer(("", recorder.config.port), recorder)

    def shutdown(signum: int, _frame: Any) -> None:
        LOG.info("Received signal %s, shutting down", signum)
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threads = [
        threading.Thread(target=recorder.polling_loop, args=(stop,), name="ring-poller", daemon=True),
        threading.Thread(target=recorder.quick_loop, args=(stop,), name="quick-look", daemon=True),
        threading.Thread(target=recorder.analysis_loop, args=(stop,), name="gemini-worker", daemon=True),
        threading.Thread(target=recorder.retention_loop, args=(stop,), name="retention", daemon=True),
    ]
    for thread in threads:
        thread.start()
    LOG.info("Ring recorder listening on port %d", recorder.config.port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        server.server_close()
        for thread in threads:
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
