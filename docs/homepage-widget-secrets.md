# Homepage Widget Secrets

Homepage service widgets (TrueNAS, Proxmox, AdGuard, Jellyfin, Plex, *arr apps, etc.) use API keys and credentials. These are injected via **Flux postBuild substituteFrom** from the `cluster-secrets` Secret.

## Required Keys

Add the following keys to `cluster-secrets` (run `sops kubernetes/components/sops/cluster-secrets.sops.yaml` and add under `stringData:`). Once added, Flux will substitute `${HOMEPAGE_*}` placeholders in the Homepage ConfigMap when applying.

### Infrastructure

| Key | Description | Where to get it |
|-----|-------------|-----------------|
| `HOMEPAGE_TRUENAS_KEY` | TrueNAS API key | TrueNAS → System Settings → API Keys |
| `HOMEPAGE_PROXMOX_USER` | Proxmox API user | e.g. `root@pam!homepage` or `user@pam!TokenID` ([create token](https://gethomepage.dev/configs/proxmox/#create-token)) |
| `HOMEPAGE_PROXMOX_PASS` | Proxmox API token secret | The secret when creating the API token |

### Network (AdGuard)

| Key | Description | Where to get it |
|-----|-------------|-----------------|
| `HOMEPAGE_ADGUARD_1_URL` | Primary AdGuard URL | e.g. `http://192.168.1.x` (no trailing slash). Same host as `adguard-1-secret` ADGUARD_IP, port usually 80. |
| `HOMEPAGE_ADGUARD_1_USER` | Primary AdGuard admin user | Same as `adguard-1-secret` ADGUARD_USER |
| `HOMEPAGE_ADGUARD_1_PASS` | Primary AdGuard admin password | Same as `adguard-1-secret` ADGUARD_PASSWORD |
| `HOMEPAGE_ADGUARD_2_URL` | Secondary AdGuard URL | e.g. `http://192.168.1.y` |
| `HOMEPAGE_ADGUARD_2_USER` | Secondary AdGuard admin user | Same as `adguard-2-secret` ADGUARD_USER |
| `HOMEPAGE_ADGUARD_2_PASS` | Secondary AdGuard admin password | Same as `adguard-2-secret` ADGUARD_PASSWORD |

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
| `HOMEPAGE_QBT_USER` | qBittorrent web UI username | Web UI login |
| `HOMEPAGE_QBT_PASS` | qBittorrent web UI password | Web UI login |
| `HOMEPAGE_IMMICH_KEY` | Immich API key | Account Settings → API Keys (needs `server.statistics`; use admin user) |

## Optional

- **AdGuard**: If you use only one instance, you can leave `HOMEPAGE_ADGUARD_2_*` unset; the Secondary card will have empty URL/auth and the widget will fail until you add them or remove the service.
- **TrueNAS**: Use `version: 2` for TrueNAS ≥ 25.04 (Websocket API); otherwise default is 1.
- **Immich**: Use `version: 2` for Immich ≥ v1.118.

## After Adding Secrets

1. Save and close the SOPS editor.
2. Commit and push `cluster-secrets.sops.yaml`.
3. Reconcile Flux: `flux reconcile kustomization flux-system --with-source` then `flux reconcile kustomization homepage -n default`.
4. Restart Homepage if needed: `kubectl rollout restart deployment/homepage -n default`.

## References

- [Homepage widgets](https://gethomepage.dev/widgets/)
- [Homepage services config](https://gethomepage.dev/configs/services/)
