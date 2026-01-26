#!/usr/bin/env python3
"""
Stats Aggregation Script for Homepage Dashboard

Fetches statistics from arr stack APIs and other services,
then updates Homepage widgets.yaml configuration.
"""

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional
import urllib.request
import urllib.error
import urllib.parse

# API endpoints for different services
API_ENDPOINTS = {
    "sonarr": {
        "base": "/api/v3",
        "series": "/api/v3/series",
        "queue": "/api/v3/queue",
        "system": "/api/v3/system/status",
        "calendar": "/api/v3/calendar"
    },
    "radarr": {
        "base": "/api/v3",
        "movies": "/api/v3/movie",
        "queue": "/api/v3/queue",
        "system": "/api/v3/system/status",
        "calendar": "/api/v3/calendar"
    },
    "lidarr": {
        "base": "/api/v1",
        "artists": "/api/v1/artist",
        "albums": "/api/v1/album",
        "tracks": "/api/v1/track",
        "queue": "/api/v1/queue",
        "system": "/api/v1/system/status"
    },
    "readarr": {
        "base": "/api/v1",
        "books": "/api/v1/book",
        "authors": "/api/v1/author",
        "queue": "/api/v1/queue",
        "system": "/api/v1/system/status"
    },
    "bazarr": {
        "base": "/api",
        "subtitles": "/api/subtitles",
        "movies": "/api/movies",
        "series": "/api/series"
    },
    "prowlarr": {
        "base": "/api/v1",
        "indexers": "/api/v1/indexer",
        "system": "/api/v1/system/status"
    },
    "jellyfin": {
        "base": "/",
        "items": "/Items",
        "sessions": "/Sessions",
        "library": "/Library/MediaFolders"
    },
    "immich": {
        "base": "/api",
        "assets": "/api/asset",
        "users": "/api/user",
        "stats": "/api/server-info/stats"
    },
    "calibre": {
        "base": "/api",
        "books": "/api/books",
        "authors": "/api/authors",
        "categories": "/api/categories"
    },
    "qbittorrent": {
        "base": "/api/v2",
        "torrents": "/api/v2/torrents/info",
        "transfer": "/api/v2/transfer/info"
    },
    "sabnzbd": {
        "base": "/api",
        "queue": "/api?mode=queue",
        "history": "/api?mode=history"
    }
}


def get_api_key(service: str) -> Optional[str]:
    """Get API key from environment variable or secret file."""
    # Try environment variable first
    env_key = f"{service.upper()}_API_KEY"
    api_key = os.getenv(env_key)
    if api_key:
        return api_key
    
    # Try reading from secret file
    secret_path = Path(f"/secrets/{service}-api-key")
    if secret_path.exists():
        return secret_path.read_text().strip()
    
    return None


def make_api_request(url: str, api_key: Optional[str] = None, headers: Optional[Dict] = None) -> Optional[Dict]:
    """Make API request and return JSON response."""
    if headers is None:
        headers = {}
    
    if api_key:
        headers["X-Api-Key"] = api_key
    
    headers.setdefault("Accept", "application/json")
    headers.setdefault("User-Agent", "Homepage-Stats/1.0")
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP error for {url}: {e.code}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"URL error for {url}: {e.reason}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"JSON decode error for {url}: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Error fetching {url}: {e}", file=sys.stderr)
        return None


def fetch_sonarr_stats(base_url: str, api_key: str) -> Dict:
    """Fetch Sonarr statistics."""
    stats = {}
    
    # Get series
    series_url = f"{base_url}{API_ENDPOINTS['sonarr']['series']}"
    series_data = make_api_request(series_url, api_key)
    if series_data:
        stats["series"] = len(series_data)
        # Count episodes
        total_episodes = sum(s.get("episodeCount", 0) for s in series_data)
        stats["episodes"] = total_episodes
    
    # Get queue
    queue_url = f"{base_url}{API_ENDPOINTS['sonarr']['queue']}"
    queue_data = make_api_request(queue_url, api_key)
    if queue_data:
        stats["queue"] = len(queue_data)
    
    return stats


def fetch_radarr_stats(base_url: str, api_key: str) -> Dict:
    """Fetch Radarr statistics."""
    stats = {}
    
    # Get movies
    movies_url = f"{base_url}{API_ENDPOINTS['radarr']['movies']}"
    movies_data = make_api_request(movies_url, api_key)
    if movies_data:
        stats["movies"] = len(movies_data)
        downloaded = sum(1 for m in movies_data if m.get("hasFile", False))
        stats["downloaded"] = downloaded
        stats["missing"] = len(movies_data) - downloaded
    
    # Get queue
    queue_url = f"{base_url}{API_ENDPOINTS['radarr']['queue']}"
    queue_data = make_api_request(queue_url, api_key)
    if queue_data:
        stats["queue"] = len(queue_data)
    
    return stats


def fetch_lidarr_stats(base_url: str, api_key: str) -> Dict:
    """Fetch Lidarr statistics."""
    stats = {}
    
    # Get artists
    artists_url = f"{base_url}{API_ENDPOINTS['lidarr']['artists']}"
    artists_data = make_api_request(artists_url, api_key)
    if artists_data:
        stats["artists"] = len(artists_data)
    
    # Get albums
    albums_url = f"{base_url}{API_ENDPOINTS['lidarr']['albums']}"
    albums_data = make_api_request(albums_url, api_key)
    if albums_data:
        stats["albums"] = len(albums_data)
    
    # Get tracks
    tracks_url = f"{base_url}{API_ENDPOINTS['lidarr']['tracks']}"
    tracks_data = make_api_request(tracks_url, api_key)
    if tracks_data:
        stats["tracks"] = len(tracks_data)
    
    # Get queue
    queue_url = f"{base_url}{API_ENDPOINTS['lidarr']['queue']}"
    queue_data = make_api_request(queue_url, api_key)
    if queue_data:
        stats["queue"] = len(queue_data)
    
    return stats


def fetch_readarr_stats(base_url: str, api_key: str) -> Dict:
    """Fetch Readarr statistics."""
    stats = {}
    
    # Get books
    books_url = f"{base_url}{API_ENDPOINTS['readarr']['books']}"
    books_data = make_api_request(books_url, api_key)
    if books_data:
        stats["books"] = len(books_data)
    
    # Get authors
    authors_url = f"{base_url}{API_ENDPOINTS['readarr']['authors']}"
    authors_data = make_api_request(authors_url, api_key)
    if authors_data:
        stats["authors"] = len(authors_data)
    
    # Get queue
    queue_url = f"{base_url}{API_ENDPOINTS['readarr']['queue']}"
    queue_data = make_api_request(queue_url, api_key)
    if queue_data:
        stats["queue"] = len(queue_data)
    
    return stats


def fetch_bazarr_stats(base_url: str, api_key: str) -> Dict:
    """Fetch Bazarr statistics."""
    stats = {}
    
    # Get movies
    movies_url = f"{base_url}{API_ENDPOINTS['bazarr']['movies']}"
    movies_data = make_api_request(movies_url, api_key)
    if movies_data:
        stats["movies"] = len(movies_data)
    
    # Get series
    series_url = f"{base_url}{API_ENDPOINTS['bazarr']['series']}"
    series_data = make_api_request(series_url, api_key)
    if series_data:
        stats["series"] = len(series_data)
    
    return stats


def fetch_prowlarr_stats(base_url: str, api_key: str) -> Dict:
    """Fetch Prowlarr statistics."""
    stats = {}
    
    # Get indexers
    indexers_url = f"{base_url}{API_ENDPOINTS['prowlarr']['indexers']}"
    indexers_data = make_api_request(indexers_url, api_key)
    if indexers_data:
        stats["indexers"] = len(indexers_data)
        active = sum(1 for i in indexers_data if i.get("enable", False))
        stats["active"] = active
    
    return stats


def fetch_jellyfin_stats(base_url: str, api_key: Optional[str] = None) -> Dict:
    """Fetch Jellyfin statistics."""
    stats = {}
    
    # Jellyfin uses different auth - try without API key first
    # Get items (requires authentication, but we can try public endpoints)
    # For now, return empty - would need proper Jellyfin auth token
    # This is a placeholder for future implementation
    
    return stats


def fetch_immich_stats(base_url: str, api_key: str) -> Dict:
    """Fetch Immich statistics."""
    stats = {}
    
    # Get stats
    stats_url = f"{base_url}{API_ENDPOINTS['immich']['stats']}"
    stats_data = make_api_request(stats_url, api_key)
    if stats_data:
        stats["photos"] = stats_data.get("photos", 0)
        stats["videos"] = stats_data.get("videos", 0)
        stats["usage"] = stats_data.get("usage", 0)
    
    # Get users
    users_url = f"{base_url}{API_ENDPOINTS['immich']['users']}"
    users_data = make_api_request(users_url, api_key)
    if users_data:
        stats["users"] = len(users_data)
    
    return stats


def fetch_calibre_stats(base_url: str, api_key: Optional[str] = None) -> Dict:
    """Fetch Calibre-Web statistics."""
    stats = {}
    
    # Get books
    books_url = f"{base_url}{API_ENDPOINTS['calibre']['books']}"
    books_data = make_api_request(books_url, api_key)
    if books_data:
        stats["books"] = len(books_data)
    
    # Get authors
    authors_url = f"{base_url}{API_ENDPOINTS['calibre']['authors']}"
    authors_data = make_api_request(authors_url, api_key)
    if authors_data:
        stats["authors"] = len(authors_data)
    
    # Get categories
    categories_url = f"{base_url}{API_ENDPOINTS['calibre']['categories']}"
    categories_data = make_api_request(categories_url, api_key)
    if categories_data:
        stats["categories"] = len(categories_data)
    
    return stats


def fetch_qbittorrent_stats(base_url: str, api_key: Optional[str] = None) -> Dict:
    """Fetch qBittorrent statistics."""
    stats = {}
    
    # qBittorrent uses cookie-based auth, simplified for now
    # Get torrents
    torrents_url = f"{base_url}{API_ENDPOINTS['qbittorrent']['torrents']}"
    torrents_data = make_api_request(torrents_url, api_key)
    if torrents_data:
        stats["torrents"] = len(torrents_data)
        downloading = sum(1 for t in torrents_data if t.get("state") == "downloading")
        stats["downloading"] = downloading
    
    return stats


def fetch_sabnzbd_stats(base_url: str, api_key: Optional[str] = None) -> Dict:
    """Fetch SABnzbd statistics."""
    stats = {}
    
    # SABnzbd uses query parameters for API key
    queue_url = f"{base_url}{API_ENDPOINTS['sabnzbd']['queue']}"
    if api_key:
        queue_url += f"&apikey={api_key}"
    
    queue_data = make_api_request(queue_url)
    if queue_data and "queue" in queue_data:
        queue_info = queue_data["queue"]
        stats["queue"] = queue_info.get("noofslots_total", 0)
    
    return stats


# Service fetcher mapping
FETCHERS = {
    "sonarr": fetch_sonarr_stats,
    "radarr": fetch_radarr_stats,
    "lidarr": fetch_lidarr_stats,
    "readarr": fetch_readarr_stats,
    "bazarr": fetch_bazarr_stats,
    "prowlarr": fetch_prowlarr_stats,
    "jellyfin": fetch_jellyfin_stats,
    "immich": fetch_immich_stats,
    "calibre": fetch_calibre_stats,
    "qbittorrent": fetch_qbittorrent_stats,
    "sabnzbd": fetch_sabnzbd_stats,
}


def generate_widgets_yaml(stats: Dict[str, Dict], output_path: Path):
    """Generate Homepage widgets.yaml file with stats."""
    yaml_lines = []
    yaml_lines.append("# Auto-generated by fetch-stats.py")
    yaml_lines.append("# Do not edit manually - this file is regenerated automatically")
    yaml_lines.append("")
    yaml_lines.append("widgets:")
    yaml_lines.append("")
    
    # Add arr stack stats
    arr_services = ["sonarr", "radarr", "lidarr", "readarr", "bazarr", "prowlarr"]
    for service in arr_services:
        if service in stats and stats[service]:
            yaml_lines.append(f"  - type: {service}")
            yaml_lines.append(f"    service: {service}")
            yaml_lines.append("    stats:")
            for key, value in stats[service].items():
                yaml_lines.append(f"      {key}: {value}")
            yaml_lines.append("")
    
    # Add other service stats
    other_services = ["jellyfin", "immich", "calibre", "qbittorrent", "sabnzbd"]
    for service in other_services:
        if service in stats and stats[service]:
            yaml_lines.append(f"  - type: {service}")
            yaml_lines.append(f"    service: {service}")
            yaml_lines.append("    stats:")
            for key, value in stats[service].items():
                yaml_lines.append(f"      {key}: {value}")
            yaml_lines.append("")
    
    # Write to file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(yaml_lines))
    print(f"Generated widgets.yaml with stats for {len(stats)} services", file=sys.stderr)


def main():
    """Main function."""
    # Get service URLs from environment or config
    services_config = os.getenv("SERVICES_CONFIG", "/app/config/services.yaml")
    
    # For now, use hardcoded service URLs from HTTPRoutes
    # In production, this would read from services.yaml or Kubernetes
    service_urls = {
        "sonarr": os.getenv("SONARR_URL", "https://sonarr.iacob.uk"),
        "radarr": os.getenv("RADARR_URL", "https://radarr.iacob.uk"),
        "lidarr": os.getenv("LIDARR_URL", "https://lidarr.iacob.uk"),
        "readarr": os.getenv("READARR_URL", "https://readarr.iacob.uk"),
        "bazarr": os.getenv("BAZARR_URL", "https://bazarr.iacob.uk"),
        "prowlarr": os.getenv("PROWLARR_URL", "https://prowlarr.iacob.uk"),
        "jellyfin": os.getenv("JELLYFIN_URL", "https://jellyfin.iacob.uk"),
        "immich": os.getenv("IMMICH_URL", "https://photos.iacob.uk"),
        "calibre": os.getenv("CALIBRE_URL", "https://calibre.iacob.uk"),
        "qbittorrent": os.getenv("QBITTORRENT_URL", "https://qbittorrent.iacob.uk"),
        "sabnzbd": os.getenv("SABNZBD_URL", "https://sabnzbd.iacob.uk"),
    }
    
    # Fetch stats for each service
    all_stats = {}
    for service, base_url in service_urls.items():
        if service not in FETCHERS:
            continue
        
        api_key = get_api_key(service)
        if not api_key and service in ["sonarr", "radarr", "lidarr", "readarr", "bazarr", "prowlarr"]:
            print(f"Skipping {service}: no API key found", file=sys.stderr)
            continue
        
        print(f"Fetching stats for {service}...", file=sys.stderr)
        try:
            stats = FETCHERS[service](base_url, api_key)
            if stats:
                all_stats[service] = stats
                print(f"  Got stats: {stats}", file=sys.stderr)
        except Exception as e:
            print(f"Error fetching stats for {service}: {e}", file=sys.stderr)
    
    # Generate widgets.yaml
    output_path = Path("/app/config/widgets.yaml")
    if len(sys.argv) > 1:
        output_path = Path(sys.argv[1])
    
    generate_widgets_yaml(all_stats, output_path)


if __name__ == "__main__":
    main()
