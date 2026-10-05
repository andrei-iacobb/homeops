# Planum deployment

Flux applies three Kustomizations from the same GitRepository revision:

1. `planum-migrations` owns the encrypted Secret and runs the additive migration Job.
2. `planum` waits for successful migrations, then waits for the Deployment rollout and `/ready` probe.
3. `planum-public` publishes the external HTTPRoute and its dedicated Service after that rollout.

The public Service selects the `planum.iacob.co.uk/public-api: better-auth-v1` pod label. Older backends do not carry this label and cannot receive public requests, including during a rollback. Keep this label on the pod template only; do not add it to the Deployment's immutable selector. The routes expose `/health` and `/v1`, while `/ready` remains a pod probe.

For each release, publish the reviewed backend image first. Pin the same registry digest in the Deployment and migration Job, and update the Job name and its Flux health check to `planum-migrate-<first 12 digest characters>`. Retain completed Jobs without a TTL so Flux does not recreate migrations on every reconcile. Never run migrations from backend startup.

A terminal failed migration Job keeps the backend and public-route Kustomizations blocked. After resolving the cause, delete only that failed Job and reconcile `planum-migrations` to retry. Check its completion before reconciling the backend. Do not bypass the dependency or readiness gates.

The Secret was transferred from `planum` to `planum-migrations`. To reverse that transfer, first restore it to the app resources and let `planum` reconcile and reclaim ownership. Only then remove the migrations Kustomization. Deleting the migrations Kustomization first can prune the live Secret. Reverting to an older backend removes the public pod label and therefore stops public traffic; it must not restore public access to that backend.

## First Better Auth cutover

Source revision: `2474535503dc26476922ab4654182cc13b0be85b`. The image is published in Forgejo and pinned identically in the backend and migration Job. Both use the existing `git-iacob-pull-secret`, already used by other Forgejo-backed applications in this namespace. Confirm it exists before rollout.

`BETTER_AUTH_SECRET` explicitly references the existing encrypted `planum-secret` key `JWT_SECRET`. This retains stable secret material without renaming SOPS-authenticated paths or revealing/decrypting ciphertext. The old environment key can remain unused by the new application. Both containers also set `BETTER_AUTH_URL=https://planum.iacob.co.uk` and `REGISTRATION_ENABLED=false`.

Before publishing this homeops change, inspect live Flux state and pause reconciliation of `planum`, `planum-migrations`, and `planum-public`. Scale ALL old Planum replicas to zero and wait for their pods/in-flight requests to stop, including internal access. This must happen before the FIRST credential import. Keep old auth writes frozen until the new backend starts; a normal rolling cutover can silently import a stale password.

Publish the manifest change, reconcile the source and confirm the new migration Job/image/env are rendered. Resume `planum-migrations` only, await successful Job completion, then resume `planum` and await the healthy Better Auth rollout. Resume `planum-public` last. Do not resume the backend with a stale Git revision. The changed public selector excludes every old custom-auth pod, including the prior readiness version. Do not change the Deployment immutable selector.

Verify live signup remains closed, existing login works, legacy JWTs fail, logout/password rotation revokes sessions, and cross-account access fails. Temporarily enable registration only if an initial/test account is needed, keeping all replicas consistent; restore closed registration immediately after creating the required account. All devices sign in again. Live Mac-to-phone and phone-to-Mac edits remain acceptance gates.

If import/rollout fails, retain the paused backend and stopped old replicas until the cause is resolved. Do not re-enable a publicly accessible old backend with stale password hashes after Better Auth password changes. Prefer a forward fix.

Do not use a full Git revert of this cutover as an application rollback: it would restore the old public Service selector as well as the old image and could publicly expose custom auth. Preserve the `better-auth-v1` public selector and keep routing closed while resolving a failed release.

## Registration control

From your authenticated homeops checkout, run:

```sh
mise run planum:registration on
mise run planum:registration off
mise run planum:registration status
```

The command commits `REGISTRATION_ENABLED` in `app/deployment.yaml`, pushes main, reconciles Flux and waits for the backend rollout. It uses a temporary clean clone, preserving your local edits. Opening registration lets people create accounts with their own email and password. Closing it blocks new accounts; existing accounts can still sign in. It requires your existing Git push and Kubernetes access. A concurrent main update rejects the push; rerun rather than force-pushing.

You can also change the `REGISTRATION_ENABLED` YAML value to `"true"` or `"false"` and publish normally. The backend reads it at startup, so a rollout is required. A direct pod environment edit is overwritten by Flux; keep the chosen state in Git. The migration Job's registration setting does not control the serving backend.
