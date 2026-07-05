export const meta = {
  name: 'homeops-audit',
  description: 'Parallel read-only homelab investigations: external exposure, image sources, DB backup design',
  phases: [
    { title: 'Audit' },
    { title: 'Verify' },
  ],
}

const KC = 'export KUBECONFIG=/Users/andreiiacob/homeops/kubeconfig'
const REPO = '/Users/andreiiacob/homeops'

const FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['summary', 'findings'],
  properties: {
    summary: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['title', 'severity', 'detail', 'evidence'],
        properties: {
          title: { type: 'string' },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'info'] },
          detail: { type: 'string' },
          evidence: { type: 'string', description: 'command output / http status that proves it' },
          recommendation: { type: 'string' },
        },
      },
    },
  },
}

const IMAGES_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['summary', 'stragglers'],
  properties: {
    summary: { type: 'string' },
    stragglers: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['app', 'image', 'issue'],
        properties: {
          app: { type: 'string' },
          image: { type: 'string' },
          issue: { type: 'string', description: 'e.g. not home-operations, :latest tag, linuxserver, deprecated' },
        },
      },
    },
  },
}

phase('Audit')

const [external, images, dbDesign] = await parallel([
  // A) External exposure audit — needs live kubectl + curl + repo. Claude subagent (tools + reliable schema).
  () => agent(
    `You are auditing a homelab Kubernetes cluster for EXTERNAL exposure. READ-ONLY — never mutate anything.

Setup: run \`${KC}\`. Repo at ${REPO}. Public services are on the \`envoy-external\` gateway (IP 192.168.1.8, domain *.iacob.co.uk) reached via a Cloudflare Tunnel. Internal-only is \`envoy-internal\` (*.iacob.uk).

Do this:
1. Enumerate every HTTPRoute whose parentRefs reference gateway \`envoy-external\`:
   \`kubectl get httproute -A -o json\` then filter for parentRefs[].name == "envoy-external". List each hostname.
2. Also read the cloudflare-tunnel config: \`kubectl get cm -n network -o yaml | grep -A2 hostname\` and repo file kubernetes/apps/network/cloudflare-tunnel/ to see which hostnames are published to the public internet.
3. For each PUBLIC hostname, probe it and record the auth posture:
   \`curl -s -o /dev/null -w "%{http_code}" --max-time 10 https://<host>\` and also try a known API/path. A 200 that returns app content with NO login wall is a finding; 401/403/redirect-to-login is fine.
4. Cross-reference: which of these apps have Authentik SSO / built-in auth, and which are wide open. Flag anything sensitive (admin UIs, file browsers, dashboards, databases, git) reachable publicly without auth.

Report findings ranked by severity. evidence must include the actual HTTP status / command output. Be precise and do NOT speculate — only report what you actually observed. If everything is properly locked down, say so with proof.`,
    { agentType: 'general-purpose', label: 'audit:external-exposure', phase: 'Audit', schema: FINDINGS_SCHEMA }
  ),

  // B) Image source audit — pure repo grep, code-shaped. Codex (token-efficient).
  () => agent(
    `Repo at ${REPO}. This homelab standard is: all media/download apps should use \`ghcr.io/home-operations/*\` images (UID 568). Audit ALL HelmReleases for image sources.

Do this (read-only, in ${REPO}):
1. Find every image reference: \`grep -rn "repository:\\|image:" kubernetes/apps/ --include="*.yaml" | grep -iv "#"\`
2. Categorize each by registry/publisher.
3. Flag as a straggler ANY app that: (a) is a media/arr/download app NOT on ghcr.io/home-operations, (b) uses a linuxserver.io / lscr.io image, (c) pins \`:latest\` or no explicit version tag, or (d) uses a clearly deprecated/abandoned image.
Do NOT flag legit non-media apps that have no home-operations equivalent (databases, postgres, immich, authentik, cloudflared, envoy, etc.) — those are expected to use their upstream images. Only flag genuine hygiene problems.

Return the straggler list. Be exhaustive across kubernetes/apps but precise about what's actually a problem vs expected.`,
    { agentType: 'codex', label: 'audit:images', phase: 'Audit', schema: IMAGES_SCHEMA }
  ),

  // C) DB backup design — analysis/design prose. Gemini (reference-mapping, large context).
  () => agent(
    `Repo at ${REPO}. Design a LOGICAL database backup solution for this Flux/Talos homelab. Read the current setup first:
- kubernetes/apps/databases/ (postgres, mariadb if any, redis, qdrant) — read the helmreleases + init configmaps to learn which DBs and users exist. Postgres hosts many app DBs (immich, vaultwarden, outline, gitea, paperless, authentik, vikunja, informate, firefly).
- kubernetes/components/volsync/ — existing PVC-level restic backups go to TrueNAS MinIO at http://192.168.1.67:9000, bucket \`volsync\`, user \`andrei\`.
- CLAUDE.md — repo conventions (bjw-s app-template, ks.yaml + app/ layout, SOPS secrets).

The problem: VolSync snapshots the filesystem and can catch DBs mid-write (inconsistent). We need logical dumps (pg_dumpall / mariadb-dump) on a schedule, uploaded to MinIO.

Produce a CONCRETE, implementable design following THIS repo's conventions:
1. A CronJob manifest shape for pg_dumpall of the postgres instance (all DBs in one consistent dump), gzipped, uploaded to a MinIO bucket (propose bucket name e.g. \`db-backups\`) with a date-stamped key + retention.
2. How it authenticates to postgres (reuse postgres-secret) and to MinIO (reuse the volsync minio creds secret — check its name).
3. Where files live in the repo (kubernetes/apps/databases/db-backup/ with ks.yaml + app/).
4. Schedule + retention recommendation.
5. Reference how davidilie/home-cluster or onedr0p/home-ops do logical DB backups if you know the pattern.

Output a clear design doc in markdown with the actual YAML manifests I can drop in. Prefer a simple, robust approach (a small postgres-client image running pg_dumpall piped to mc/aws-cli upload) over a heavy operator.`,
    { agentType: 'gemini', label: 'design:db-backup', phase: 'Audit' }
  ),
])

// Phase Verify — independent cross-vendor check of the external-exposure findings (Claude authored → codex verifies).
phase('Verify')
let verifiedExternal = external
if (external && external.findings && external.findings.length) {
  const flagged = external.findings.filter(f => ['critical', 'high', 'medium'].includes(f.severity))
  if (flagged.length) {
    const verdicts = await parallel(flagged.map(f => () =>
      agent(
        `Independently VERIFY this claimed external-exposure finding on a homelab. Run \`${KC}\` if needed and re-probe with curl. Be adversarial: try to REFUTE it.

Finding: "${f.title}" (severity ${f.severity})
Detail: ${f.detail}
Evidence claimed: ${f.evidence}

Re-run the probe yourself. Is this REAL (the endpoint truly is publicly reachable without auth and exposes something sensitive), or is it a false alarm (behind Cloudflare Access, Authentik, returns 401/403, or not actually public)? Answer with: REAL or FALSE, one line of proof (the http status you got), and the corrected severity.`,
        { agentType: 'codex', label: `verify:${f.title.slice(0, 30)}`, phase: 'Verify' }
      ).then(v => ({ finding: f.title, verdict: v }))
    ))
    verifiedExternal = { ...external, verifications: verdicts.filter(Boolean) }
  }
}

return {
  external: verifiedExternal,
  images,
  dbDesign,
}
