# Reconciliation

The reconciliation Job runs after provisioning. It restarts Stalwart when server settings change, then publishes system-account app passwords.

## Restart recovery

Stalwart reads server settings and binds listeners at startup. The Job restarts it when the settings plan changes, including `provisioning.extraPlan`. System-account changes do not trigger a restart.

The Job records the plan hash in two places:

- Pod-template annotation: `stalwart.s7n.systems/settings`.
- A role without permissions in Stalwart's database: `managed:<release>/settings pending|done <hash>@<time>`.

Before a rollout, the Job writes `pending`; after readiness, it writes `done`. A retry reuses a pending stamp and waits for the same rollout. A database reset removes the marker and triggers another restart, even if the pod-template hash is unchanged.

Provisioning requires `RollingUpdate` without a partition. The Job uses a restart instead of `ReloadSettings`, which would require a reachable OIDC discovery endpoint.

## Secret ownership

The Job writes password keys and its state annotations. Use one writer for those fields. An external secret controller must not restore a previous password or remove the annotations.

With `secret.create: true`, Helm creates an empty Secret and deletes it on uninstall. With `secret.create: false`, create the Secret before reconciliation; the chart does not manage its lifecycle. Other keys are preserved.

Turning creation off on an existing release removes the Secret from Helm's manifest and can delete it. Plan an explicit ownership handover, back up the Secret first, and verify the result before restarting consumers. Changing the value alone is not a retention mechanism.

The local integration suite checks Helm upgrades and client-side and server-side `kubectl apply`. It does not test Argo CD syncs. Test hook ordering, pruning and Secret ownership in your GitOps setup.
