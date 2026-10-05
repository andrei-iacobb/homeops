# Planum deployment

Flux applies three Kustomizations from the same GitRepository revision:

1. `planum-migrations` owns the encrypted Secret and runs the additive migration Job.
2. `planum` waits for successful migrations, then waits for the Deployment rollout and `/ready` probe.
3. `planum-public` publishes the external HTTPRoute and its dedicated Service after that rollout.

The public Service selects the `planum.iacob.co.uk/public-api: v1` pod label. Older backends do not carry this label and cannot receive public requests, including during a rollback. Keep this label on the pod template only; do not add it to the Deployment's immutable selector. The routes expose `/health` and `/v1`, while `/ready` remains a pod probe.

For each release, publish the reviewed backend image first. Pin the same registry digest in the Deployment and migration Job, and update the Job name and its Flux health check to `planum-migrate-<first 12 digest characters>`. Retain completed Jobs without a TTL so Flux does not recreate migrations on every reconcile. Never run migrations from backend startup.

A terminal failed migration Job keeps the backend and public-route Kustomizations blocked. After resolving the cause, delete only that failed Job and reconcile `planum-migrations` to retry. Check its completion before reconciling the backend. Do not bypass the dependency or readiness gates.

The Secret was transferred from `planum` to `planum-migrations`. To reverse that transfer, first restore it to the app resources and let `planum` reconcile and reclaim ownership. Only then remove the migrations Kustomization. Deleting the migrations Kustomization first can prune the live Secret. Reverting to an older backend removes the public pod label and therefore stops public traffic; it must not restore public access to that backend.
