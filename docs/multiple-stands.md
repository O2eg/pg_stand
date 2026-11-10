# Multiple stands and migration endpoints

Version 0.3 supports multiple project directories on one Docker daemon. Each
project owns its credentials, storage, containers, network and active lease.
Names and published ports remain explicit; pg-stand does not allocate ports or
schedule host resources automatically. Keep one active profile per directory.
Concurrent lifecycle commands targeting the same directory fail with a
precondition error; commands for independent directories may run concurrently.

## Two independent servers

Initialize two directories and use different profiles, whose default names and
ports already differ:

```bash
pg-stand init --directory ./source
pg-stand init --directory ./target
pg-stand --project ./source -c ./source/configs/single-logical.yaml plan
pg-stand --project ./target -c ./target/configs/single.yaml plan
pg-stand --project ./source -c ./source/configs/single-logical.yaml up
pg-stand --project ./target -c ./target/configs/single.yaml up
pg-stand list
pg-stand --project ./source active
pg-stand active --name pg-stand-single
```

Configuration paths are resolved relative to the caller's current directory.
`--project` changes the storage/credential boundary, not configuration-path
resolution. Existing commands that use the project as their working directory
continue to work. Copying an unchanged profile into a second directory does not
make its Docker names or ports unique: edit them or use different profiles.

`single-logical.yaml` selects the ordinary `single` topology with:

```yaml
spec:
  postgres:
    logical_decoding:
      max_wal_senders: 10
      max_replication_slots: 10
```

The presence of this mapping enables `wal_level=logical`. Both limits default to
10 and must be positive integers. This option is valid only for `single`:
replicated profiles already manage these settings through their topology.
It creates no physical standby, publication, slot, subscription or migration
role. The migration utility owns those resources. Changes require a restart.

## A connection from target to source

Administrative connections from the host use the published ports (`55433` for
the example source and `55432` for the target). A subscription runs inside the
target container: its `127.0.0.1` is not the host or the source.

For a shared Docker route, create a network and add it to **both** configurations
before their first `up`:

```bash
docker network create migration-lab
```

```yaml
spec:
  docker:
    external_networks: [migration-lab]
```

This is an excerpt to merge into the existing `spec`, not another duplicate YAML
key. pg-stand connects its own nodes to the existing network in addition to their
private stand network. It never creates or removes external networks and rejects
networks owned by another pg-stand. The operator owns their lifetime. The source
address for the target's subscription is
`pg-stand-logical-source-primary-pg-stand-managed:5432`. In a pg-logical-repl
configuration, use that address in `source.replication`, while `source.admin`
and `target.admin` use the host-accessible published ports. Configure credentials
through the migration utility's secret-reference mechanism.

Before reusing a running project with changed network configuration, review
`plan` and apply the required restart. An idempotent `up` reconnects declared
external networks that were manually disconnected.

## Cleanup and compatibility

```bash
pg-stand --project ./source -c ./source/configs/single-logical.yaml \
  cleanup run --all --force
```

Only the source project's containers, storage and credentials are selected by
`--all`. The target's data and credentials, shared images and external network
remain available. Image/cache cleanup has daemon-wide scope over pg-stand-owned
images and requires explicit `--images`/`--cache`; referenced images are skipped.
Remove the external network separately after its users have detached.

Old profiles preserve their configuration hashes and existing storage layout.
The previous global lease is recognized only for its owning project; other
projects may start alongside it. Use the new CLI for all concurrent lifecycle
operations: older releases do not implement the new project operation lock.
Keep project paths stable because their resolved absolute path defines identity.
For interrupted startup, a conflicting lease, lost credentials, or a moved directory,
use the [recovery procedure](recovery.md). A leftover operation-lock file does not by
itself indicate a running operation.

Managed image schema 7 fixes stale PgBouncer PID files after a container restart.
Existing containers keep their original image: rebuild the managed image and use
`recreate` without `--clear-data` when applying this image fix to an old stand.
See the [managed-image upgrade steps](recovery.md#apply-a-managed-image-upgrade), including
the effect of shared image tags on other projects.

`list` enumerates active leases, including partial startups, rather than a
registry of stopped projects. `active` preserves its old response shape and
refuses an ambiguous selection. If two projects share a display name, select
by project directory. Inspect a stopped project using its configuration.

## pg_play and pg-logical-repl

Existing pg_play manifests already specify `spec.stand.project`. Separate
manifests can now own separate stands without the daemon-wide stand restriction.
The `pg_play/v1` experiment manifest still describes one stand; pg-stand does not
add a multi-stand workflow to the orchestrator. Logical experiments still require
prepared source/target databases and native migration plans.

Project credentials are generated independently. Existing roles selected by a
pg-logical-repl migration must meet its role-attribute and password-verifier
preflight requirements; separate stands do not automatically reconcile roles.
Review migration preflight before starting a controller.

This enables keeping a pg_maker baseline and candidate online, or separating the
two sides of a migration. It does not add cross-major migration support to
pg-logical-repl, whose current preflight requires matching PostgreSQL majors
15 through 18. Use separate project, workload and artifact directories for
independent experiments. Avoid concurrent benchmarks on shared hardware when
comparing performance; container limits do not isolate host I/O or page cache.

## Verification

`python tests/integration_multistand.py` creates two temporary projects and a
private test network. It starts both concurrently, checks logical initial copy
and continuing DML, exercises stop/start and reload/restart on one side, rejects
name/port collisions, and checks that cleanup preserves the other side's data,
credentials and runtime identity. It also verifies that pre-existing managed
containers were not restarted or removed. The test cleans only its own projects
and network. Set `PG_STAND_POSTGRES_VERSION` to select another supported major.
