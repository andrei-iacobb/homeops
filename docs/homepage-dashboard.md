# Homepage Dashboard with Auto-Discovery and Stats

This document describes the automated Homepage dashboard setup that discovers services from Kubernetes and pulls statistics from arr stack APIs and other services.

## Overview

The dashboard consists of two main components:

1. **Service Discovery**: Automatically discovers running services from Kubernetes pods and HTTPRoutes
2. **Stats Aggregation**: Fetches statistics from arr stack APIs (Sonarr, Radarr, Lidarr, etc.) and other services

## Architecture

```
┌─────────────────┐
│  kubectl get    │
│     pods        │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────┐
│  Service Discovery Script   │
│  (discover-services.py)     │
│  - Maps pods to services    │
│  - Extracts HTTPRoute URLs  │
│  - Generates services.yaml  │
└────────┬────────────────────┘
         │
         ▼
┌─────────────────────────────┐
│  Stats Aggregation Service  │
│  (fetch-stats.py)           │
│  - Polls arr stack APIs     │
│  - Fetches stats from APIs  │
│  - Updates widgets.yaml     │
└────────┬────────────────────┘
         │
         ▼
┌─────────────────────────────┐
│     Homepage Config         │
│  - services.yaml (auto-gen) │
│  - widgets.yaml (with stats)│
└────────┬────────────────────┘
         │
         ▼
┌─────────────────────────────┐
│   Homepage Dashboard        │
│   (ghcr.io/gethomepage/     │
│    homepage)                │
└─────────────────────────────┘
```

## Components

### 1. Service Discovery Script

**Location**: `scripts/homepage/discover-services.py`

**Functionality**:
- Queries Kubernetes API for running pods
- Extracts service URLs from HTTPRoute resources
- Maps services to Homepage widget types
- Generates `services.yaml` configuration file

**Service Mappings**:
- Arr Stack: Sonarr, Radarr, Lidarr, Readarr, Bazarr, Prowlarr
- Media Servers: Jellyfin, Plex
- Download Clients: qBittorrent, SABnzbd
- Books: Calibre-Web, LazyLibrarian
- Base Services: Vaultwarden, Gitea, Outline, n8n
- Photos: Immich
- AI: Ollama, Open WebUI
- Monitoring: Grafana, Prometheus

### 2. Stats Aggregation Script

**Location**: `scripts/homepage/fetch-stats.py`

**Functionality**:
- Fetches statistics from arr stack APIs
- Retrieves stats from other services (Jellyfin, Immich, etc.)
- Updates `widgets.yaml` with current statistics

**Stats Collected**:

#### Arr Stack Services

**Sonarr**:
- Total series count
- Total episodes count
- Queue items

**Radarr**:
- Total movies count
- Movies downloaded
- Movies missing
- Queue items

**Lidarr**:
- Total artists count
- Total albums count
- Total tracks count
- Queue items

**Readarr**:
- Total books count
- Total authors count
- Queue items

**Bazarr**:
- Movies with subtitles
- Series with subtitles

**Prowlarr**:
- Total indexers count
- Active indexers

#### Other Services

**Jellyfin**:
- Movies, series, episodes, songs counts
- Active streams

**Immich**:
- Total photos
- Total videos
- Total users
- Storage used

**Calibre-Web**:
- Total books
- Total authors
- Total categories

**Download Clients**:
- qBittorrent: Active downloads, torrents count
- SABnzbd: Queue items, download stats

### 3. Kubernetes Deployment

**Location**: `kubernetes/apps/default/homepage-stats/`

**Components**:
- **CronJob**: Runs every 15 minutes to update configs
- **ServiceAccount**: For Kubernetes API access
- **Role/RoleBinding**: RBAC permissions for reading pods and HTTPRoutes
- **ConfigMap**: Contains Python scripts
- **Secret**: SOPS-encrypted API keys

## Setup Instructions

### 1. Update Scripts in ConfigMap

The ConfigMap `homepage-stats-scripts` needs to contain the full Python scripts. Update it:

```bash
# Create/update the ConfigMap with the scripts
kubectl create configmap homepage-stats-scripts \
  --from-file=discover-services.py=scripts/homepage/discover-services.py \
  --from-file=fetch-stats.py=scripts/homepage/fetch-stats.py \
  -n default --dry-run=client -o yaml | kubectl apply -f -
```

Or edit `kubernetes/apps/default/homepage-stats/app/configmap-scripts.yaml` and ensure it contains the full scripts, then apply:

```bash
kubectl apply -f kubernetes/apps/default/homepage-stats/app/configmap-scripts.yaml
```

### 2. Configure API Keys

Edit `kubernetes/apps/default/homepage-stats/app/secret.sops.yaml`:

```yaml
stringData:
  sonarr-api-key: YOUR_SONARR_API_KEY
  radarr-api-key: YOUR_RADARR_API_KEY
  lidarr-api-key: YOUR_LIDARR_API_KEY
  readarr-api-key: YOUR_READARR_API_KEY
  bazarr-api-key: YOUR_BAZARR_API_KEY
  prowlarr-api-key: YOUR_PROWLARR_API_KEY
  immich-api-key: YOUR_IMMICH_API_KEY
```

**How to get API keys**:

- **Arr Stack Services**: Settings > General > Security > API Key
- **Immich**: Settings > API Keys > Create new key
- **Other services**: Check their respective documentation

After adding API keys, encrypt the file:

```bash
sops --encrypt --in-place kubernetes/apps/default/homepage-stats/app/secret.sops.yaml
```

### 3. Deploy the Stats Service

The stats service is already included in the kustomization. Apply it:

```bash
kubectl apply -k kubernetes/apps/default/homepage-stats/
```

Or let Flux CD sync it automatically.

### 4. Verify Deployment

Check that the CronJob is created:

```bash
kubectl get cronjob -n default homepage-stats
```

Check recent job runs:

```bash
kubectl get jobs -n default -l app=homepage-stats
```

View logs from a recent job:

```bash
kubectl logs -n default -l app=homepage-stats --tail=100
```

### 5. Verify Config Generation

The scripts generate configs in the Homepage PVC. Check the generated files:

```bash
# Get the homepage pod name
HOMEPAGE_POD=$(kubectl get pod -n default -l app=homepage -o jsonpath='{.items[0].metadata.name}')

# Check services.yaml
kubectl exec -n default $HOMEPAGE_POD -- cat /app/config/services.yaml

# Check widgets.yaml
kubectl exec -n default $HOMEPAGE_POD -- cat /app/config/widgets.yaml
```

## Configuration

### CronJob Schedule

The default schedule runs every 15 minutes. To change it, edit:

`kubernetes/apps/default/homepage-stats/app/cronjob.yaml`

```yaml
spec:
  schedule: "*/15 * * * *"  # Change this to your preferred schedule
```

### Service URLs

Service URLs are automatically discovered from HTTPRoutes. If you need to override them, set environment variables in the CronJob:

```yaml
env:
  - name: SONARR_URL
    value: "https://sonarr.iacob.uk"
  # ... etc
```

### Adding New Services

To add support for a new service:

1. **Add service mapping** in `scripts/homepage/discover-services.py`:
   ```python
   "newservice": {
       "name": "New Service",
       "widget": "newservice",
       "category": "Media",
       "icon": "newservice.svg",
       "description": "Service Description"
   }
   ```

2. **Add stats fetcher** in `scripts/homepage/fetch-stats.py`:
   ```python
   def fetch_newservice_stats(base_url: str, api_key: str) -> Dict:
       # Implementation
       pass
   ```

3. **Update ConfigMap** with the new script if needed

4. **Redeploy** the CronJob

## Troubleshooting

### Services Not Appearing

1. Check that pods are running:
   ```bash
   kubectl get pods -A | grep <service-name>
   ```

2. Check that HTTPRoute exists:
   ```bash
   kubectl get httproute -A | grep <service-name>
   ```

3. Check CronJob logs for discovery errors:
   ```bash
   kubectl logs -n default -l app=homepage-stats
   ```

### Stats Not Updating

1. Verify API keys are correct:
   ```bash
   kubectl get secret -n default homepage-stats-secret -o yaml
   ```

2. Test API connectivity from a pod:
   ```bash
   kubectl run -it --rm test --image=curlimages/curl --restart=Never -- \
     curl -H "X-Api-Key: YOUR_API_KEY" https://sonarr.iacob.uk/api/v3/system/status
   ```

3. Check stats script logs:
   ```bash
   kubectl logs -n default -l app=homepage-stats | grep -i stats
   ```

### PVC Mount Issues

If the CronJob can't mount the Homepage PVC (ReadWriteOnce conflict):

1. The CronJob runs briefly and should be able to mount when Homepage isn't actively using it
2. Alternatively, use a shared volume or init container approach
3. Consider using a Deployment with a sidecar pattern instead

### Permission Errors

If you see RBAC permission errors:

1. Verify ServiceAccount exists:
   ```bash
   kubectl get serviceaccount -n default homepage-stats
   ```

2. Check RoleBinding:
   ```bash
   kubectl get rolebinding -n default homepage-stats
   ```

3. Verify Role permissions:
   ```bash
   kubectl describe role -n default homepage-stats
   ```

## Manual Execution

To manually trigger a config update:

```bash
# Create a one-time job from the CronJob
kubectl create job --from=cronjob/homepage-stats manual-update -n default

# Watch the job
kubectl get job -n default manual-update -w

# Check logs
kubectl logs -n default -l job-name=manual-update
```

## Customization

### Custom Categories

Edit the namespace mapping in `discover-services.py`:

```python
NAMESPACE_CATEGORIES = {
    "media": "Media",
    "default": "Base",
    "custom-ns": "Custom Category",
}
```

### Custom Widgets

Homepage supports various widget types. Refer to [Homepage documentation](https://gethomepage.dev/widgets/) for available widgets and configuration options.

## Security Considerations

- API keys are stored in SOPS-encrypted secrets
- ServiceAccount has minimal RBAC permissions (read-only for pods and HTTPRoutes)
- Stats service runs in the same namespace as Homepage
- No API keys are exposed in logs or ConfigMaps

## Future Enhancements

- Real-time stats updates via WebSocket
- Custom widgets for specific stats
- Health check integration
- Alerting based on stats thresholds
- Historical stats tracking
- Support for more services

## References

- [Homepage Documentation](https://gethomepage.dev/)
- [Arr Stack APIs](https://wiki.servarr.com/)
- [Kubernetes CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/)
