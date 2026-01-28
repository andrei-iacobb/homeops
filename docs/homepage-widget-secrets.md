# Homepage Widget Secrets

Homepage service widgets (TrueNAS, Proxmox, AdGuard, Jellyfin, Plex, *arr apps, etc.) use API keys and credentials. These are injected via **Flux postBuild substituteFrom** from the `cluster-secrets` Secret.

## Status: What’s configured vs what you still need

**Already in `cluster-secrets` (widgets will work once Flux reconciles):**

- **Infrastructure:** TrueNAS key, Proxmox token (`root@pam!again` + secret)
- **Network:** AdGuard Primary (192.168.1.120, andrei / andreiadmin), AdGuard Secondary (192.168.1.125, andrei / andreiadmin)
- **Media:** Radarr, Sonarr, Prowlarr, SABnzbd API keys

**Still required (add to `cluster-secrets` via `sops kubernetes/components/sops/cluster-secrets.sops.yaml`):**

| Key | Where to get it |
|-----|-----------------|
| `HOMEPAGE_JELLYFIN_KEY` | Jellyfin → Dashboard → API Keys |
| `HOMEPAGE_PLEX_TOKEN` | [Plex token](https://www.plexopedia.com/plex-media-server/general/plex-token/) |
| `HOMEPAGE_LIDARR_KEY` | Lidarr → Settings → General |
| `HOMEPAGE_READARR_KEY` | Readarr → Settings → General |
| `HOMEPAGE_BAZARR_KEY` | Bazarr → Settings → General |
| `HOMEPAGE_OVERSEERR_KEY` | Overseerr → Settings → General |
| `HOMEPAGE_QBT_USER` | qBittorrent web UI username |
| `HOMEPAGE_QBT_PASS` | qBittorrent web UI password |
| `HOMEPAGE_IMMICH_KEY` | Immich → Account Settings → API Keys (`server.statistics`; use admin) |

Until these are set, those widgets will keep using `${HOMEPAGE_...}` and won’t load data. The rest (TrueNAS, Proxmox, AdGuard, Radarr, Sonarr, Prowlarr, SABnzbd) are ready.

---

## Full key reference

### Infrastructure

| Key | Description | Where to get it |
|-----|-------------|-----------------|
| `HOMEPAGE_TRUENAS_KEY` | TrueNAS API key | TrueNAS → System Settings → API Keys |
| `HOMEPAGE_PROXMOX_USER` | Proxmox API user | e.g. `root@pam!TokenID` ([create token](https://gethomepage.dev/configs/proxmox/#create-token)) |
| `HOMEPAGE_PROXMOX_PASS` | Proxmox API token secret | Secret shown when creating the token |

### Network (AdGuard)

| Key | Description | Where to get it |
|-----|-------------|-----------------|
| `HOMEPAGE_ADGUARD_1_URL` | Primary AdGuard URL | e.g. `http://192.168.1.120` (no trailing slash) |
| `HOMEPAGE_ADGUARD_1_USER` | Primary AdGuard admin user | Web UI login |
| `HOMEPAGE_ADGUARD_1_PASS` | Primary AdGuard admin password | Web UI login |
| `HOMEPAGE_ADGUARD_2_URL` | Secondary AdGuard URL | e.g. `http://192.168.1.125` |
| `HOMEPAGE_ADGUARD_2_USER` | Secondary AdGuard admin user | Web UI login |
| `HOMEPAGE_ADGUARD_2_PASS` | Secondary AdGuard admin password | Web UI login |

### Media & Base

| Key | Description | Where to get it |
|-----|-------------|-----------------|
| `HOMEPAGE_JELLYFIN_KEY` | Jellyfin API key | Jellyfin → Dashboard → API Keys |
| `HOMEPAGE_PLEX_TOKEN` | Plex token | [Plex token help](https://www.plexopedia.com/plex-media-server/general/plex-token/) |
| `HOMEPAGE_SONARR_KEY` | Sonarr API key | Settings → General |
| `HOMEPAGE_RADARR_KEY` | Radarr API key | Settings → General |
| `HOMEPAGE_LIDARR_KEY` | Lidarr API key | Settings → General |
| `HOMEPAGE_READARR_KEY` | Readarr API key | Settings → General |
| `HOMEPAGE_PROWLARR_KEY` | Prowlarr API key | Settings → General |
| `HOMEPAGE_BAZARR_KEY` | Bazarr API key | Settings → General |
| `HOMEPAGE_OVERSEERR_KEY` | Overseerr API key | Settings → General |
| `HOMEPAGE_SAB_KEY` | SABnzbd API key | Config → General |
| `HOMEPAGE_QBT_USER` | qBittorrent web UI username | Web UI login |
| `HOMEPAGE_QBT_PASS` | qBittorrent web UI password | Web UI login |
| `HOMEPAGE_IMMICH_KEY` | Immich API key | Account Settings → API Keys (needs `server.statistics`; use admin user) |

## Optional

- **AdGuard:** If you use only one instance, leave `HOMEPAGE_ADGUARD_2_*` unset (or remove the Secondary service).
- **TrueNAS:** Use `version: 2` for TrueNAS ≥ 25.04 (Websocket API); otherwise default is 1.
- **Immich:** Use `version: 2` for Immich ≥ v1.118.

## After adding or changing secrets

1. Save and close the SOPS editor (or finish `sops --set` updates).
2. Commit and push `cluster-secrets.sops.yaml`.
3. Reconcile Flux: `flux reconcile kustomization flux-system --with-source` then `flux reconcile kustomization homepage -n default`.
4. Restart Homepage if needed: `kubectl rollout restart deployment/homepage -n default`.

## References

- [Homepage widgets](https://gethomepage.dev/widgets/)
- [Homepage services config](https://gethomepage.dev/configs/services/)
