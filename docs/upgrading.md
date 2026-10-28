# Upgrading

## Chart 0.2.1: Stalwart 1.0.1

- Metric names changed. Prometheus metrics now start with `stalwart_` and use unit and `_total` suffixes. Update your dashboards and alerts.
- `OTEL_*` environment variables no longer work. Configure OpenTelemetry with the `serviceName`, `serviceNamespace` and `resourceAttributes` settings instead.
- The chart's settings are unchanged.

## Stalwart 1.0

This chart targets Stalwart 1.0. It does not retain compatibility with older Stalwart releases.

Before upgrading a 0.16 installation:

1. Back up the database, blob store, configuration and Kubernetes Secrets. Test restoration.
2. Follow the upstream [migration instructions](https://github.com/stalwartlabs/stalwart-migrate) for the data and settings.
3. Rewrite custom provisioning plans for the 1.0 schema. See the upstream [schema changes](https://github.com/stalwartlabs/stalwart-migrate/blob/main/SCHEMA_CHANGES.md).
4. Review external-directory authentication. System accounts use Stalwart-generated app passwords; their consumers must adopt the resulting Secret values.
5. Test sign-in, sending and receiving mail before directing production traffic to the new installation.

Helm rollback does not revert the database or settings written by provisioning. Returning to an older application version requires its matching backup and configuration.

See [reconciliation](reconciliation.md) before changing system-account Secret ownership.
