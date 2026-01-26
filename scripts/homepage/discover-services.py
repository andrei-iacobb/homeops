#!/usr/bin/env python3
"""
Service Discovery Script for Homepage Dashboard

Discovers services from Kubernetes pods and HTTPRoutes,
then generates Homepage services.yaml configuration.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

# Service mappings: service name pattern -> Homepage widget config
SERVICE_MAPPINGS = {
    # Arr Stack
    "sonarr": {
        "name": "Sonarr",
        "widget": "sonarr",
        "category": "Media",
        "icon": "sonarr.svg",
        "description": "TV Show Management"
    },
    "radarr": {
        "name": "Radarr",
        "widget": "radarr",
        "category": "Media",
        "icon": "radarr.svg",
        "description": "Movie Management"
    },
    "lidarr": {
        "name": "Lidarr",
        "widget": "lidarr",
        "category": "Media",
        "icon": "lidarr.svg",
        "description": "Music Management"
    },
    "readarr": {
        "name": "Readarr",
        "widget": "readarr",
        "category": "Media",
        "icon": "readarr.svg",
        "description": "Book Management"
    },
    "bazarr": {
        "name": "Bazarr",
        "widget": "bazarr",
        "category": "Media",
        "icon": "bazarr.svg",
        "description": "Subtitle Management"
    },
    "prowlarr": {
        "name": "Prowlarr",
        "widget": "prowlarr",
        "category": "Media",
        "icon": "prowlarr.svg",
        "description": "Indexer Manager"
    },
    "overseerr": {
        "name": "Overseerr",
        "widget": "overseerr",
        "category": "Media",
        "icon": "overseerr.svg",
        "description": "Media Request Manager"
    },
    # Media Servers
    "jellyfin": {
        "name": "Jellyfin",
        "widget": "jellyfin",
        "category": "Media",
        "icon": "jellyfin.svg",
        "description": "Media Server"
    },
    "plex": {
        "name": "Plex",
        "widget": "plex",
        "category": "Media",
        "icon": "plex.svg",
        "description": "Media Server"
    },
    # Download Clients
    "qbittorrent": {
        "name": "qBittorrent",
        "widget": "qbittorrent",
        "category": "Media",
        "icon": "qbittorrent.svg",
        "description": "Torrent Client"
    },
    "sabnzbd": {
        "name": "SABnzbd",
        "widget": "sabnzbd",
        "category": "Media",
        "icon": "sabnzbd.svg",
        "description": "Usenet Client"
    },
    # Books
    "calibre": {
        "name": "Calibre-Web",
        "widget": "calibre",
        "category": "Notes",
        "icon": "calibre.svg",
        "description": "eBook Library"
    },
    "lazylibrarian": {
        "name": "LazyLibrarian",
        "widget": "lazylibrarian",
        "category": "Notes",
        "icon": "lazylibrarian.svg",
        "description": "Book Metadata Manager"
    },
    # Base Services
    "vaultwarden": {
        "name": "Vaultwarden",
        "widget": "vaultwarden",
        "category": "Base",
        "icon": "vaultwarden.svg",
        "description": "Password Manager"
    },
    "gitea": {
        "name": "Gitea",
        "widget": "gitea",
        "category": "Base",
        "icon": "gitea.svg",
        "description": "Git Service"
    },
    "outline": {
        "name": "Outline",
        "widget": "outline",
        "category": "Base",
        "icon": "outline.svg",
        "description": "Team Wiki"
    },
    "n8n": {
        "name": "n8n",
        "widget": "n8n",
        "category": "Base",
        "icon": "n8n.svg",
        "description": "Workflow Automation"
    },
    # Photos
    "immich": {
        "name": "Immich",
        "widget": "immich",
        "category": "Notes",
        "icon": "immich.svg",
        "description": "Photo Backup"
    },
    # AI
    "ollama": {
        "name": "Ollama",
        "widget": "ollama",
        "category": "Base",
        "icon": "ollama.svg",
        "description": "LLM Server"
    },
    "openwebui": {
        "name": "Open WebUI",
        "widget": "openwebui",
        "category": "Base",
        "icon": "openwebui.svg",
        "description": "AI Chat Interface"
    },
    # Monitoring
    "grafana": {
        "name": "Grafana",
        "widget": "grafana",
        "category": "Monitoring",
        "icon": "grafana.svg",
        "description": "Monitoring Dashboard"
    },
    "prometheus": {
        "name": "Prometheus",
        "widget": "prometheus",
        "category": "Monitoring",
        "icon": "prometheus.svg",
        "description": "Metrics Server"
    },
    # Other
    "tdarr": {
        "name": "Tdarr",
        "widget": "tdarr",
        "category": "Media",
        "icon": "tdarr.svg",
        "description": "Media Transcoding"
    },
    "recyclarr": {
        "name": "Recyclarr",
        "widget": "recyclarr",
        "category": "Media",
        "icon": "recyclarr.svg",
        "description": "TRaSH Guides Sync"
    },
    "huntarr": {
        "name": "Huntarr",
        "widget": "huntarr",
        "category": "Media",
        "icon": "huntarr.svg",
        "description": "Hunt Missing Media"
    },
    "recommendarr": {
        "name": "Recommendarr",
        "widget": "recommendarr",
        "category": "Media",
        "icon": "recommendarr.svg",
        "description": "Media Recommendations"
    },
    "lidify": {
        "name": "Lidify",
        "widget": "lidify",
        "category": "Notes",
        "icon": "lidify.svg",
        "description": "Audiobook Manager"
    },
}

# Namespace to category mapping
NAMESPACE_CATEGORIES = {
    "media": "Media",
    "default": "Base",
    "monitoring": "Monitoring",
    "databases": "Base",
    "network": "Base",
    "ai": "Base",
    "kube-system": "Monitoring",
}


def run_kubectl(command: List[str]) -> Dict:
    """Run kubectl command and return JSON output."""
    try:
        result = subprocess.run(
            ["kubectl"] + command,
            capture_output=True,
            text=True,
            check=True
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"Error running kubectl: {e.stderr}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}", file=sys.stderr)
        sys.exit(1)


def get_running_pods() -> List[Dict]:
    """Get all running pods from Kubernetes."""
    pods = run_kubectl(["get", "pods", "-A", "-o", "json"])
    running_pods = []
    
    for item in pods.get("items", []):
        status = item.get("status", {})
        phase = status.get("phase", "")
        conditions = status.get("conditions", [])
        
        # Check if pod is running
        is_ready = any(
            cond.get("type") == "Ready" and cond.get("status") == "True"
            for cond in conditions
        )
        
        if phase == "Running" and is_ready:
            running_pods.append(item)
    
    return running_pods


def get_httproutes() -> Dict[str, str]:
    """Get HTTPRoute resources and map service names to hostnames."""
    routes = run_kubectl(["get", "httproute", "-A", "-o", "json"])
    route_map = {}
    
    for item in routes.get("items", []):
        metadata = item.get("metadata", {})
        name = metadata.get("name", "")
        namespace = metadata.get("namespace", "")
        spec = item.get("spec", {})
        hostnames = spec.get("hostnames", [])
        
        if hostnames:
            # Use first hostname
            route_map[f"{namespace}/{name}"] = hostnames[0]
            route_map[name] = hostnames[0]  # Also map by name only
    
    return route_map


def identify_service(pod_name: str, namespace: str) -> Optional[Dict]:
    """Identify service type from pod name."""
    pod_lower = pod_name.lower()
    
    # Check for exact matches first
    for pattern, config in SERVICE_MAPPINGS.items():
        if pattern in pod_lower:
            return config.copy()
    
    # Try namespace-based categorization
    if namespace in NAMESPACE_CATEGORIES:
        category = NAMESPACE_CATEGORIES[namespace]
        return {
            "name": pod_name.split("-")[0].title(),
            "widget": None,
            "category": category,
            "icon": None,
            "description": "Service"
        }
    
    return None


def generate_services_yaml(services: List[Dict], output_path: Path):
    """Generate Homepage services.yaml file."""
    # Group services by category
    categories = {}
    for service in services:
        category = service.get("category", "Other")
        if category not in categories:
            categories[category] = []
        categories[category].append(service)
    
    # Generate YAML
    yaml_lines = []
    yaml_lines.append("# Auto-generated by discover-services.py")
    yaml_lines.append("# Do not edit manually - this file is regenerated automatically")
    yaml_lines.append("")
    
    for category in sorted(categories.keys()):
        yaml_lines.append(f"{category}:")
        for service in sorted(categories[category], key=lambda x: x["name"]):
            yaml_lines.append(f"  {service['name']}:")
            yaml_lines.append(f"    href: https://{service['url']}")
            
            if service.get("widget"):
                yaml_lines.append(f"    widget:")
                yaml_lines.append(f"      type: {service['widget']}")
                yaml_lines.append(f"      url: https://{service['url']}")
                # API key will be added by stats service
            
            if service.get("icon"):
                yaml_lines.append(f"    icon: {service['icon']}")
            
            if service.get("description"):
                yaml_lines.append(f"    description: {service['description']}")
            
            yaml_lines.append("")
    
    # Write to file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(yaml_lines))
    print(f"Generated services.yaml with {len(services)} services", file=sys.stderr)


def main():
    """Main function."""
    # Get running pods
    pods = get_running_pods()
    print(f"Found {len(pods)} running pods", file=sys.stderr)
    
    # Get HTTPRoutes
    routes = get_httproutes()
    print(f"Found {len(routes)} HTTPRoutes", file=sys.stderr)
    
    # Discover services
    services = []
    for pod in pods:
        metadata = pod.get("metadata", {})
        name = metadata.get("name", "")
        namespace = metadata.get("namespace", "")
        
        # Skip system pods
        if namespace in ["kube-system", "flux-system"] and "homepage" not in name.lower():
            continue
        
        # Identify service
        service_config = identify_service(name, namespace)
        if not service_config:
            continue
        
        # Get URL from HTTPRoute
        url = routes.get(f"{namespace}/{name}") or routes.get(name)
        if not url:
            # Try to construct from service name
            service_name = name.split("-")[0]
            url = routes.get(service_name)
        
        if url:
            service_config["url"] = url
            services.append(service_config)
            print(f"Discovered: {service_config['name']} -> {url}", file=sys.stderr)
    
    # Generate services.yaml
    output_path = Path("/app/config/services.yaml")
    if len(sys.argv) > 1:
        output_path = Path(sys.argv[1])
    
    generate_services_yaml(services, output_path)


if __name__ == "__main__":
    main()
