# Dual-Domain Private Networking Setup

This plan configures your cluster to use two domains:
- **`iacob.co.uk`**: Public domain (kept for future public services, certificates, Cloudflare)
- **`iacob`**: Private domain for internal services only (envoy-internal gateway)

## Architecture Overview

- **SECRET_DOMAIN**: Remains `iacob.co.uk` (used for certificates, Cloudflare DNS, public gateway hostnames)
- **Private Domain**: `iacob` (used for private service hostnames and k8s-gateway DNS)
- **Private Services**: Use `*.iacob` hostnames with `envoy-internal` gateway
- **Public Services** (later): Use `*.iacob.co.uk` hostnames with `envoy-external` gateway
- **Certificates**: Continue using Let's Encrypt with `iacob.co.uk` wildcard cert (covers `*.iacob.co.uk`)

## Files to Modify

### 1. k8s-gateway Configuration (Private DNS)
- **[kubernetes/apps/network/k8s-gateway/app/helmrelease.yaml](kubernetes/apps/network/k8s-gateway/app/helmrelease.yaml)**: Change `domain` from `"${SECRET_DOMAIN}"` to `"iacob"` (line 13)
  - This makes k8s-gateway provide DNS for the `iacob` domain only
  - Public DNS for `iacob.co.uk` will still be handled by Cloudflare DNS (external-dns)

### 2. Private Service HTTPRoutes (Switch to envoy-internal with .iacob domain)
- **[kubernetes/apps/default/echo/app/helmrelease.yaml](kubernetes/apps/default/echo/app/helmrelease.yaml)**:
  - Change `hostnames` from `["{{ .Release.Name }}.${SECRET_DOMAIN}"]` to `["{{ .Release.Name }}.iacob"]` (line 63)
  - Change `parentRefs` from `envoy-external` to `envoy-internal` (line 65)

- **[kubernetes/apps/flux-system/flux-instance/app/httproute.yaml](kubernetes/apps/flux-system/flux-instance/app/httproute.yaml)**:
  - Change `hostnames` from `["flux-webhook.${SECRET_DOMAIN}"]` to `["flux-webhook.iacob"]` (line 7)
  - Change `parentRefs` from `envoy-external` to `envoy-internal` (line 9)

### 3. What Stays Unchanged

- **cluster.yaml**: Keep `cloudflare_domain: "iacob.co.uk"` (SECRET_DOMAIN remains for public services)
- **Certificates**: Continue using `iacob.co.uk` wildcard cert (works for `*.iacob.co.uk`)
- **envoy-external gateway**: Keeps using `external.iacob.co.uk` (for future public services)
- **envoy-internal gateway**: Keeps using `internal.iacob.co.uk` as gateway hostname (but routes `*.iacob` services)
- **Cloudflare components**: Remain active and configured for `iacob.co.uk`

## AdGuard Home Configuration

Configure AdGuard Home to forward DNS queries for the `iacob` domain to your k8s-gateway:

1. **Access AdGuard Home**: Navigate to your AdGuard Home admin interface (typically http://<adguard-ip>:3000)

2. **Set Up Conditional Forwarding**:
   - Go to **Settings → DNS settings**
   - Scroll to find **Upstream DNS servers** or **Private reverse DNS servers**
   - Look for **Conditional forwarding** or **Domain-specific upstream servers**
   - Add entry:
     - **Domain**: `iacob` (or `*.iacob`)
     - **Upstream server**: `192.168.1.6:53` (your k8s-gateway IP)
     - **Protocol**: UDP (default for DNS)

   Alternative method if conditional forwarding isn't available:
   - Go to **Filters → DNS rewrites**
   - This may require a different approach - check AdGuard Home documentation

3. **Verify Configuration**:
   ```bash
   # Test DNS resolution through AdGuard
   dig @<adguard-ip> echo.iacob
   # Should resolve to 192.168.1.7 (envoy-internal gateway IP)

   # Test direct k8s-gateway resolution
   dig @192.168.1.6 echo.iacob
   # Should also resolve to 192.168.1.7
   ```

4. **Set AdGuard as DNS Server**:
   - Configure your home router to use AdGuard Home IP as the primary DNS server
   - Or configure individual devices/Docker containers to use AdGuard IP as DNS

## Future Service Deployment

### Private Services (current setup)
When deploying new services (like huntarr):
- Create HTTPRoute with:
  - `hostnames: ["service-name.iacob"]`
  - `parentRefs` pointing to `envoy-internal` gateway in `network` namespace
- Services accessible only from private network via `https://service-name.iacob`

### Public Services (future setup)
When deploying public services later:
- Create HTTPRoute with:
  - `hostnames: ["service-name.iacob.co.uk"]`
  - `parentRefs` pointing to `envoy-external` gateway in `network` namespace
- external-dns will automatically create Cloudflare DNS records
- Services accessible from internet via Cloudflare Tunnel

## Summary

- **Private services**: `*.iacob` → envoy-internal → private network only
- **Public services**: `*.iacob.co.uk` → envoy-external → Cloudflare Tunnel → internet
- **DNS**: AdGuard forwards `iacob` to k8s-gateway (192.168.1.6), `iacob.co.uk` resolves via Cloudflare
- **Certificates**: Wildcard cert for `*.iacob.co.uk` works for public services (private services may need separate cert handling if needed)
