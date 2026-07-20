# pg-stand

`pg-stand` deploys reproducible PostgreSQL test stands from declarative YAML. Images,
containers, networks, and bind mounts are managed through the Docker SDK for Python; the
application does not invoke `docker` or `docker compose` commands.

## Use cases

Use `pg-stand` when a task needs a disposable but realistic PostgreSQL environment rather than a
single bare database process. Typical uses include:

- building and manually reviewing `pg_diag` reports across PostgreSQL versions and replication
  modes;
- reproducing performance, logging, locking, WAL, autovacuum, or extension-related behavior;
- testing physical and logical replication workflows without maintaining a permanent lab;
- validating certificate-only database clients, replication connections, and authentication
  failures;
- running repeatable CI integration tests with explicit CPU and memory bounds;
- DBA training, runbook validation, and experiments that need host-visible PGDATA, WAL, and CSV
  logs;
- giving AI coding agents a stable deployment primitive for recurring PostgreSQL tasks. An agent
  can reference a short profile plus one CLI command instead of repeatedly reconstructing
  Dockerfiles, networks, replication bootstrap, credentials, and cleanup instructions. This can
  reduce repeated prompt/context tokens and tool calls; the actual saving depends on the task and
  agent workflow.

The ownership boundary is deliberately narrow:

```text
                         declarative control plane
                 +-----------------------------------+
 YAML profile -->| pg-stand CLI --> Docker SDK       |
                 |   |               |               |
                 |   |               +--> managed images
                 |   +------------------> labeled containers/network
                 +-----------------------------------+
                                      |
                                      v
 project directory                    Docker Engine
 +--------------------------------+   +---------------------------+
 | .pg_stand/credentials/         |-->| read-only key mounts      |
 | .pg_stand/<stand>/<node>/      |-->| PGDATA / WAL / log mounts |
 +--------------------------------+   +---------------------------+

 Resources without both pg-stand ownership labels and the managed name suffix are out of scope.
```

Four topologies are included:

- a single PostgreSQL server;
- a primary with an asynchronous physical standby;
- a primary with a synchronous physical standby;
- a primary with a synchronous physical standby and a separate `app_01` all-tables logical
  subscriber.

Physical standbys are full cluster copies created with `pg_basebackup`. Automatic failover and
promotion are outside the current scope.

## Topologies and data flow

Single server:

```text
 host clients
     | direct PostgreSQL :55432
     | session pool      :57432 --> PgBouncer :6432 --+
     | transaction pool  :58432 --> PgBouncer :6433 --+--> PostgreSQL :5432
                                                        |       primary
                                                        |
                                password, or client certificate with TLS
                                                                |
                                   +----------------------------+-------------------+
                                   |                            |                   |
                              host PGDATA                  host WAL       host CSV/PgBouncer logs
```

Asynchronous or synchronous physical replication uses the same topology. The profile controls
whether commits wait for the standby:

```text
                    initial full copy
              +--------------------------+
              |       pg_basebackup      |
              v                          |
 +-----------+   physical WAL stream   +-----------+
 |  primary  | =======================>|  standby  |
 +-----------+                          +-----------+
       ^                                      |
       |                                      +--> read-only recovery
       |
       +-- async: commit does not wait
       +-- sync:  commit waits for configured synchronous standby
```

The full profile combines a synchronous physical copy with an independent logical subscriber:

```text
                                  physical WAL (synchronous)
                            +=================================> +-----------+
                            |                                   |  standby  |
                            |                                   +-----------+
                            |
                      +-----------+
 writes / DDL ------->|  primary  |
                      +-----------+
                            |
                            | app_01 logical WAL via publication/subscription
                            +----------------------------------> +-----------+
                                                                  | logical  |
 schema-only pg_dump on initial setup or `logical sync` --------> | replica  |
                                                                  +-----------+

 Physical standby: complete cluster copy, read-only while in recovery.
 Logical replica:   independent writable cluster; all app_01 tables only, no automatic DDL.
```

## Installation

### Install from PyPI

Install the published package in an isolated environment, then initialize a stand project:

```bash
mkdir pg-stand-project
cd pg-stand-project

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install pg-stand

pg-stand --version
pg-stand init
pg-stand -c configs/single.yaml validate
```

The distribution name and installed command are `pg-stand`; the import package is `pg_stand`.
The wheel includes the four example profiles, public JSON Schema, Dockerfiles, entrypoint, and
PgBouncer launcher. Managed-image builds therefore do not depend on a source checkout.

`pg-stand init` writes editable copies of the profiles and schema into the selected project. It
is idempotent when those files are unchanged and refuses to overwrite local edits. Use
`pg-stand init --force` only when the bundled versions should replace those edits. A different
directory can be initialized with `pg-stand init --directory PATH`.

Upgrade an existing installation with:

```bash
python -m pip install --upgrade pg-stand
pg-stand init --force
```

### Install from source

```bash
git clone https://github.com/O2eg/pg_stand.git
cd pg_stand
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

mkdir -p local-stand
.venv/bin/pg-stand init --directory local-stand
cd local-stand
../.venv/bin/pg-stand -c configs/single.yaml validate
```

## Quick start

Requirements are Python 3.10+, Docker Engine, OpenSSL, `ssh-keygen`, and a command executed from
the project root. The current project directory is the storage boundary: `pg-stand` refuses to
create managed storage outside it.

```bash
pg-stand init
pg-stand -c configs/single.yaml validate
pg-stand -c configs/single.yaml storage init
pg-stand -c configs/single.yaml up
pg-stand -c configs/single.yaml status
pg-stand -c configs/single.yaml sql "select version()"
```

Ready-to-use profiles:

```text
configs/single.yaml
configs/primary-standby-async.yaml
configs/primary-standby-sync.yaml
configs/primary-standby-sync-logical.yaml
```

Set the PostgreSQL major in `spec.postgres.version` or override it for one command:

```bash
pg-stand \
  -c configs/primary-standby-async.yaml \
  --postgres-version 18 \
  up
```

By default, `pg-stand` builds `pg-stand/postgres:<version>-pg-stand-managed` on top of an
official PostgreSQL image. Both the fixed tag suffix and mandatory image ownership labels are
required before cleanup can see or remove an image. Override the base through
`spec.postgres.base_image`; a template such as
`registry/image:{version}` is allowed. Set `spec.postgres.image` to use a complete custom image
and disable the managed build.

## PostgreSQL versions and diagnostic images

PostgreSQL majors 10 through 18 are supported. A single universal Dockerfile is deliberately
avoided; the runtime selects a compatible image family automatically.

| PostgreSQL | Dockerfile | Version-specific behavior |
|---|---|---|
| 10 | bundled `Dockerfile.pg10` | Legacy WAL settings; execution kernel statistics |
| 11–12 | bundled `Dockerfile.pg11-pg12` | Execution kernel statistics |
| 13–17 | bundled `Dockerfile.pg13-pg17` | Execution and planning kernel statistics |
| 18 | bundled `Dockerfile.pg18` | Planning statistics and the new official-image layout |

Every managed image installs PgBouncer 1.25.2, `iproute2`, `lshw`, OpenSSH server, `procps`,
`sysstat`, `util-linux`, plus the `pg_wait_sampling` and `pg_stat_kcache` packages. Every node
runs independent PgBouncer processes for session pooling on container port 6432 and transaction
pooling on container port 6433. Every application
database from `app_01` through `app_10` automatically gets `pg_stat_statements`,
`pg_wait_sampling`, `pg_buffercache`, and `pg_stat_kcache`. The `auto_explain` module is also
loaded through
`shared_preload_libraries`. Planning tracking for `pg_stat_statements` and `pg_stat_kcache` is
enabled only on PostgreSQL 13 and newer.

The diagnostic profile maximizes statistics collection within each supported major:

- `track_activities`, `track_counts`, and `track_io_timing` are enabled;
- procedural-language functions are tracked and activity query text is increased to 4096 bytes;
- `pg_stat_statements` keeps 5000 entries, tracks all and utility statements, and saves data;
- core `compute_query_id` and WAL I/O timing are enabled on PostgreSQL 14 and newer;
- `auto_explain` captures statements from 100 ms with analyze, buffers, verbose output, timing,
  triggers, and text format; WAL details are enabled on PostgreSQL 13 and newer.

These aggressive test settings, especially `auto_explain.log_analyze` with per-node timing at
100 ms, add measurable overhead. They are intentional for diagnostic stands and are not a
production tuning recommendation.

## Fixed CSV logging

All nodes use a fixed CSV logging layout. `log_destination`, `logging_collector`, the
host-mounted log directory, filename, file mode, and daily/256 MiB rotation settings cannot be
overridden. The remaining values are diagnostic defaults: statements lasting at least 3 seconds
and all DDL are logged, as are warning/error statements, checkpoints, lock waits after a
1-second `deadlock_timeout`, temporary files from 64 MiB, autovacuum work from 5 seconds, and
replication commands. PostgreSQL 14 and newer also log long recovery-conflict waits.

Operators may override thresholds and statistics settings through `spec.postgres.parameters`.
This includes `log_min_duration_statement`, `log_statement`, `log_min_messages`,
`deadlock_timeout`, `log_temp_files`, `log_autovacuum_min_duration`, `auto_explain.*`,
`track_*`, `pg_stat_statements.*`, `pg_stat_kcache.*`, and `compute_query_id` when supported by
the selected PostgreSQL major. Defaults and overrides live in the managed include file, so
reloadable changes work through `apply --reload`.

The profile is version-aware: PostgreSQL 18 uses the empty-list form of its string-valued
`log_connections`, while older majors use boolean `off`. Parameters unavailable on old majors
are never passed to them. The `logs` command reads the active CSV file recorded by
`current_logfiles`.

Managed builds validate image-schema, family, major, and base-image labels. Changing
`base_image` therefore cannot silently reuse an incompatible local image with the same tag.
The default PostgreSQL 10 base is `postgres:10-bullseye`; other versions use
`postgres:<version>`.

## Certificate-only TLS

TLS is disabled by default. No separate TLS topology files are needed. Enable it persistently in
any existing profile:

```yaml
spec:
  postgres:
    tls:
      enabled: true
```

Alternatively, override the profile for a command with the global `--tls` flag. The option must
precede the subcommand and must be repeated for every lifecycle or observation command that
addresses that TLS stand:

```bash
pg-stand --tls -c configs/single.yaml up
pg-stand --tls -c configs/single.yaml health
pg-stand --tls -c configs/single.yaml status
pg-stand --tls -c configs/single.yaml down

pg-stand --tls -c configs/primary-standby-sync-logical.yaml up
pg-stand --tls -c configs/primary-standby-sync-logical.yaml replication status
pg-stand --tls -c configs/primary-standby-sync-logical.yaml down
```

`--no-tls` explicitly overrides a profile that enables TLS. Because TLS changes container
identity and PostgreSQL startup settings, do not mix TLS and non-TLS invocations for one active
stand. For regular repeated use, setting `spec.postgres.tls.enabled: true` in the chosen profile
is less error-prone than repeating the CLI override.

In TLS mode every TCP connection must use TLS and present a client certificate signed by the
locally generated test CA. Plain TCP, TLS without a client certificate, and password-only TCP
authentication are rejected by the managed `pg_hba.conf`. Local Unix-domain socket access inside
a container remains available for lifecycle and recovery operations.

No database password, SSH or TLS private key, public key, or certificate is stored in the
repository. On the first `up`, pg-stand atomically creates all three credential sets under the
git-ignored `.pg_stand/credentials/` directory. `server.crt` identifies PostgreSQL, `postgres.crt`
authenticates the configured superuser, and `replicator.crt` authenticates the configured
physical replication role. The CA private key exists only while certificates are being issued
and is not retained.

```text
 first `up`
     |
     v
 credentials present and complete? -- yes --> reuse existing files
     |
     no
     v
 +-----------------------------+
 | generate in temporary dirs   |
 | DB: 12 distinct passwords    |
 | SSH: Ed25519 keypair         |
 | TLS: CA + server + clients   |
 +------------------------------+
     |
     +--> atomic publish --> .pg_stand/credentials/
                              |-- database/passwords.json (mode 0600)
                              |-- ssh/pg_stand_test[.pub]
                              `-- tls/{ca,server,postgres,replicator}.*
                                      |
                                      | read-only bind mount
                                      v
                               container entrypoint
                                      |
                                      +--> /root/.ssh/authorized_keys
                                      `--> /pg-stand-tls (private keys mode 0600)
```

A partially present credential set is rejected rather than silently rotating database
passwords, the CA, or the SSH identity of an existing cluster. Certificate identity maps allow
the generated certificate common names to work with custom declarative role names. These
credentials are for disposable local stands and must never be reused in production.

The managed physical and logical replication paths also use certificates. `pg_basebackup`,
`primary_conninfo`, schema transfer, and subscription connection strings use `verify-ca` because
container DNS names vary by profile. Host examples use `verify-full`; the server certificate SAN
covers `localhost`, `127.0.0.1`, and `::1`.

## Host storage

PostgreSQL data does not use Docker volumes. Each node gets three bind-mounted directories
under `spec.storage.root_directory`, which must resolve inside the project directory:

```text
.pg_stand/
├── credentials/             # generated once, shared by the single active stand
│   ├── database/
│   ├── ssh/
│   └── tls/
└── <stand>/
    ├── primary/
    │   ├── data/
    │   ├── wal/
    │   └── log/
    ├── standby/             # physical-replication profiles only
    │   ├── data/
    │   ├── wal/
    │   └── log/
    └── logical/             # primary-standby-sync-logical only
        ├── data/
        ├── wal/
        └── log/
```

- `data` contains `PGDATA`;
- `wal` is assigned through `initdb --waldir` or `pg_basebackup --waldir`, with `pg_wal`
  pointing to that separate mount;
- `log` is the absolute `log_directory` used by the PostgreSQL logging collector.

For PostgreSQL 17 and older, `data` is mounted at `/var/lib/postgresql/data`. PostgreSQL 18
and newer mount `/var/lib/postgresql`, with `PGDATA` in a version-specific subdirectory as
required by the official image. No anonymous data volume is created.

Storage has dedicated commands:

```bash
pg-stand -c CONFIG storage init
pg-stand -c CONFIG storage status
pg-stand -c CONFIG storage usage
pg-stand -c CONFIG storage clean
```

`storage status` reports absolute host paths, bytes, file counts, and the major read from
`PG_VERSION`. `storage usage` adds per-node totals and host filesystem capacity. `storage clean`
permanently clears data, WAL, and logs, and refuses to run while stand containers exist. A safe
explicit sequence is:

```bash
pg-stand -c CONFIG down
pg-stand -c CONFIG storage clean
```

## Stand lifecycle

Global options (`-c`, `--postgres-version`, `--tls`/`--no-tls`, and `--json`) precede the command.

```bash
pg-stand init [--directory PATH] [--force]
pg-stand -c CONFIG validate
pg-stand -c CONFIG show
pg-stand -c CONFIG up [--timeout 180]
pg-stand active
pg-stand -c CONFIG status
pg-stand -c CONFIG resources [--node primary|standby|logical]
pg-stand -c CONFIG health
pg-stand -c CONFIG wait --timeout 180
pg-stand -c CONFIG watch --interval 2
pg-stand -c CONFIG replication status
pg-stand -c CONFIG replication watch --interval 2
pg-stand -c CONFIG plan
pg-stand -c CONFIG apply --dry-run
pg-stand -c CONFIG apply --reload
pg-stand -c CONFIG apply --restart
pg-stand -c CONFIG stop
pg-stand -c CONFIG restart
pg-stand -c CONFIG recreate [--clear-data]
pg-stand -c CONFIG node inspect primary
pg-stand -c CONFIG node restart standby
pg-stand -c CONFIG standby reinit --force
pg-stand -c CONFIG logs primary|standby|logical|all [-f]
pg-stand -c CONFIG events --since 30m
pg-stand -c CONFIG doctor
pg-stand -c CONFIG capabilities
pg-stand -c CONFIG dsn --node primary
pg-stand -c CONFIG psql --node primary
pg-stand -c CONFIG ssh --node primary
pg-stand -c CONFIG image list|inspect|build
pg-stand -c CONFIG cleanup status
pg-stand -c CONFIG cleanup run --all --force
pg-stand -c CONFIG sql "select ..." [--node standby] [--database app_01]
pg-stand -c CONFIG logical sync [--full]
pg-stand -c CONFIG down [--clear-data]
```

`stop` preserves containers and all mounted directories. `down` removes containers and the
managed network while retaining data. `recreate` rebuilds containers around the existing
cluster.

`recreate --clear-data` stops and removes all stand containers, clears each node's data, WAL,
and log directories, and performs a fresh `up`. `down --clear-data` performs the same cleanup
without starting the stand again.

`up` is idempotent. Existing resources are reused only when ownership labels and the resolved
configuration match. Unowned resources are never overwritten. A major-version mismatch in
`PG_VERSION` is blocked because it requires `pg_upgrade` or an explicit
`recreate --clear-data`.

Only one stand may be active per Docker daemon. `up` atomically acquires the fixed
`pg-stand-active-lock-pg-stand-managed` network; a second profile is rejected until the active
stand is stopped or removed. `stop` and `down` release the lock. A failed partial startup keeps
the lock when it has already started a database node, so another stand cannot start around the
failure. Destructive storage cleanup retains the lock until mounted data and credentials are no
longer being changed, which prevents a concurrent `up` from racing with deletion.

Persistent container and network names must end with `-pg-stand-managed`. Every resource also
has mandatory project, project-directory instance, managed, resource-kind, node, and
configuration labels. Discovery uses
Docker label filters and exact configured names, so lifecycle operations never enumerate or
mutate unrelated containers. A foreign resource occupying a configured name only makes Docker
creation fail; it is not stopped or removed.

Each node declares four loopback ports: `published_port` for PostgreSQL,
`ssh_published_port` for SSH, `pgbouncer_session_published_port` for session pooling, and
`pgbouncer_transaction_published_port` for transaction pooling. The single-node profile uses
`55432`, `56432`, `57432`, and `58432`, respectively. The
`cpu_limit` and `memory_limit` fields are also required per node. Included profiles limit every
container to one CPU and 1 GiB RAM. `shm_size` is configured separately and does not replace
the RAM limit.

`status` reports the active-project lease, configuration drift, container hostname and image,
container-network IPv4/IPv6 addresses, gateways, published host addresses, desired limits, and
actual Docker limits. `resources` adds one-shot CPU, memory, and PID usage and can be limited to
one node.

`active` is the only command that does not require `--config`. It discovers the fixed active
lease and lists only containers carrying the required pg-stand ownership labels and managed
name suffix. This is useful when the operator does not remember which profile is running.

`health` verifies the active lease, desired/applied configuration, container and PostgreSQL
readiness, real SQL queries through both PgBouncer instances, node roles, TLS policy and
certificate authentication, diagnostic extensions and
preloads, writable log storage, free space, physical streaming/synchronous state and lag, and
logical workers/table synchronization. Its
exit codes are `0` for healthy, `1` for degraded, and `2` for unavailable. Thresholds are
configurable with `--max-physical-lag-bytes`, `--max-physical-lag-seconds`,
`--max-logical-lag-seconds`, and `--min-free-bytes`. `wait` repeats the same checks until they
pass or its timeout expires.

`watch` emits combined status, resource usage, and replication samples. `replication status`
adds LSN positions, byte/time lag, WAL receiver state, replication slots and retained WAL,
logical workers, and logical table states. Both watch commands support `--interval`, `--count`,
and `--json-lines`, making them suitable for terminal use and lightweight automation.

To change CPU, RAM, ports, shared memory, image, or restart-only PostgreSQL parameters, edit the
YAML and run `apply --restart`. The command removes and recreates every configured container in
safe topology order without clearing the bind-mounted data, then waits for physical and logical
replication readiness. It also refreshes immutable creation labels.

For changes limited to reloadable `spec.postgres.parameters`, use `apply --reload`. The command
writes the same managed `pg_stand_parameters.conf` fragment to every node and calls
`pg_reload_conf()` everywhere. It rejects `postmaster` parameters and any container/resource
drift with an explicit request to use `--restart`. An applied-state record under the stand
storage root prevents a plain `up` from silently ignoring edited YAML.

`plan` compares the desired YAML with that record and classifies the next action as `none`,
`up`, `reload`, `restart`, or `blocked`. It also reports changed paths and PostgreSQL parameter
contexts. `apply --dry-run` performs the same read-only analysis, optionally validating an
explicit `--reload` or `--restart` choice. Neither command creates images, containers, files,
or PostgreSQL settings.

`node inspect` reports mounts, limits, usage, PostgreSQL start time, role, version, and installed
extensions for one node. `node restart` restores the expected physical/logical readiness after
the selected container restarts. `standby reinit --force` temporarily removes synchronous
requirements when needed, destroys only standby storage, takes a new `pg_basebackup`, and
restores the configured replication mode.

`doctor` is a read-only preflight for Docker capacity, the configured image, host ports, storage
boundary and permissions, disk space, credential generators, and generated credential state.
`capabilities` explains
which version-dependent statistics and replication features are available. `image list` sees
only images with pg-stand ownership labels and `pg-stand/postgres:*-pg-stand-managed` tags;
`image inspect` validates the resolved image, and `image build` forces a managed-image rebuild.

`cleanup status` combines PGDATA/WAL/log byte and file counts, generated credential sizes,
Docker container writable-layer sizes, and managed image sizes. `cleanup run` requires `--force`
plus an explicit selection of
`--containers`, `--storage`, `--images`, or `--cache`; `--all` also selects the generated
credentials as a fifth cleanup scope. Image removal
requires both ownership labels and the managed tag suffix and never uses force. The cache scope
is intentionally limited to dangling images with pg-stand labels. Docker BuildKit cache is
daemon-global and cannot safely be attributed to this application, so pg-stand never performs a
global builder-cache prune. Removing a managed image also asks Docker to reclaim its
now-unreferenced private layers. `cleanup run --all --force` also removes the shared generated
credentials after all managed stand nodes have stopped; the next `up` creates a new identity.

## Observation, logs, and connections

Use `dsn` to obtain redacted connection metadata. Passwords are shown only when explicitly
requested. The `psql` and `ssh` commands start host clients without putting the database password
on their command line:

```bash
pg-stand -c configs/single.yaml dsn --node primary
pg-stand -c configs/single.yaml dsn --node primary --show-password
pg-stand -c configs/single.yaml psql --node primary --database app_01
pg-stand -c configs/single.yaml ssh --node primary

pg-stand --tls -c configs/single.yaml dsn --node primary
pg-stand --tls -c configs/single.yaml psql --node primary --database app_01
```

The TLS-aware `dsn` output includes `sslmode`, CA, client certificate, and client key paths,
reports the password as null, and does not place it in the URI. The `psql` command exports the
equivalent libpq environment automatically.

PgBouncer is available on every node. In the single profile, port 57432 preserves a server
connection for the client session, while port 58432 returns it after each transaction. Both
poolers route `postgres` and all ten application databases to the PostgreSQL instance in the
same container. Without TLS they use SCRAM authentication and the generated database passwords:

```bash
PGPASSWORD="$(jq -r '.superuser.password' .pg_stand/credentials/database/passwords.json)" \
  psql -h 127.0.0.1 -p 57432 -U postgres -d app_01 -c 'select current_user'

PGPASSWORD="$(jq -r '.superuser.password' .pg_stand/credentials/database/passwords.json)" \
  psql -h 127.0.0.1 -p 58432 -U postgres -d app_01 -c 'select current_user'
```

When `--tls` or `spec.postgres.tls.enabled: true` is selected, both PgBouncer listeners become
certificate-only just like the direct PostgreSQL listener. Plain TCP and TLS clients without a
valid generated client certificate are rejected:

```bash
psql "host=127.0.0.1 port=57432 dbname=app_01 user=postgres \
sslmode=verify-full sslrootcert=$PWD/.pg_stand/credentials/tls/ca.crt \
sslcert=$PWD/.pg_stand/credentials/tls/postgres.crt \
sslkey=$PWD/.pg_stand/credentials/tls/postgres.key"

psql "host=127.0.0.1 port=58432 dbname=app_01 user=postgres \
sslmode=verify-full sslrootcert=$PWD/.pg_stand/credentials/tls/ca.crt \
sslcert=$PWD/.pg_stand/credentials/tls/postgres.crt \
sslkey=$PWD/.pg_stand/credentials/tls/postgres.key"
```

Connect directly with a certificate-authenticated host `psql`:

```bash
psql "host=127.0.0.1 port=55432 dbname=app_01 user=postgres \
sslmode=verify-full sslrootcert=$PWD/.pg_stand/credentials/tls/ca.crt \
sslcert=$PWD/.pg_stand/credentials/tls/postgres.crt \
sslkey=$PWD/.pg_stand/credentials/tls/postgres.key"
```

Initialize a small pgbench database and run a deliberately moderate two-client workload through
the same certificate-only endpoint:

```bash
pgbench -i -s 1 \
  "host=127.0.0.1 port=55432 dbname=app_01 user=postgres \
  sslmode=verify-full sslrootcert=$PWD/.pg_stand/credentials/tls/ca.crt \
  sslcert=$PWD/.pg_stand/credentials/tls/postgres.crt \
  sslkey=$PWD/.pg_stand/credentials/tls/postgres.key"

pgbench -c 2 -j 1 -T 30 -P 5 \
  "host=127.0.0.1 port=55432 dbname=app_01 user=postgres \
  sslmode=verify-full sslrootcert=$PWD/.pg_stand/credentials/tls/ca.crt \
  sslcert=$PWD/.pg_stand/credentials/tls/postgres.crt \
sslkey=$PWD/.pg_stand/credentials/tls/postgres.key"

# Run the same moderate workload through transaction pooling.
pgbench -c 2 -j 1 -T 30 -P 5 \
  "host=127.0.0.1 port=58432 dbname=app_01 user=postgres \
  sslmode=verify-full sslrootcert=$PWD/.pg_stand/credentials/tls/ca.crt \
  sslcert=$PWD/.pg_stand/credentials/tls/postgres.crt \
  sslkey=$PWD/.pg_stand/credentials/tls/postgres.key"
```

The default one-node `logs` form preserves raw CSV output and supports `--follow`. Structured
mode can merge every node by timestamp and filter records by time, minimum severity, exact
backend type, or a case-insensitive message/query substring:

```bash
pg-stand -c configs/single.yaml logs primary --tail 100
pg-stand -c configs/single.yaml logs primary --follow
pg-stand -c configs/primary-standby-sync-logical.yaml logs all \
  --since 30m --severity warning --format pretty
pg-stand --json -c configs/primary-standby-sync-logical.yaml logs all \
  --backend-type autovacuum --contains "automatic vacuum"
```

`events` requests Docker events with both the managed and current-project labels, then verifies
the managed name suffix before displaying an event. It cannot expose or act on unrelated Docker
resources:

```bash
pg-stand -c configs/single.yaml events --since 1h
pg-stand -c configs/single.yaml events --follow --json-lines
```

## Direct Docker command examples

The application itself uses the Docker SDK and never shells out to the Docker CLI. Operators can
still use normal Docker commands for ad-hoc inspection. Always retain the pg-stand label filters
shown below so commands do not accidentally include unrelated containers.

List every pg-stand node and then only nodes from the single profile:

```bash
docker ps -a \
  --filter label=io.pg-stand.managed=true \
  --filter label=io.pg-stand.resource-kind=stand-node

docker ps -a \
  --filter label=io.pg-stand.managed=true \
  --filter label=io.pg-stand.project=pg-stand-single
```

List and inspect managed images and compare their layer accounting with Docker's disk report:

```bash
docker image ls \
  --filter label=io.pg-stand.managed=true \
  --filter label=io.pg-stand.resource-kind=managed-image

docker image inspect pg-stand/postgres:17-pg-stand-managed

docker system df -v
```

Use `pg-stand cleanup`, rather than `docker image rm` or `docker builder prune`, for removal. The
CLI checks both image labels and the managed suffix and deliberately avoids daemon-global build
cache cleanup.

Inspect the primary container, its mounts, IP addresses, published ports, and resource limits:

```bash
docker inspect pg-stand-single-primary-pg-stand-managed

docker inspect \
  --format '{{json .Mounts}}' \
  pg-stand-single-primary-pg-stand-managed

docker inspect \
  --format '{{json .NetworkSettings.Networks}} {{json .NetworkSettings.Ports}}' \
  pg-stand-single-primary-pg-stand-managed

docker inspect \
  --format 'cpus={{.HostConfig.NanoCpus}} memory={{.HostConfig.Memory}} shm={{.HostConfig.ShmSize}}' \
  pg-stand-single-primary-pg-stand-managed
```

Connect through the published host port or run `psql` inside the container:

```bash
PGPASSWORD="$(jq -r '.superuser.password' .pg_stand/credentials/database/passwords.json)" \
  psql -h 127.0.0.1 -p 55432 -U postgres -d app_01

docker exec -it pg-stand-single-primary-pg-stand-managed \
  psql -U postgres -d app_01

docker exec pg-stand-single-primary-pg-stand-managed \
  psql -U postgres -d app_01 -c 'select version(), pg_is_in_recovery();'

docker exec \
  -e PGPASSWORD="$(jq -r '.superuser.password' .pg_stand/credentials/database/passwords.json)" \
  pg-stand-single-primary-pg-stand-managed \
  psql -h 127.0.0.1 -p 6432 -U postgres -d app_01 -c 'select current_user;'

docker exec \
  -e PGPASSWORD="$(jq -r '.superuser.password' .pg_stand/credentials/database/passwords.json)" \
  pg-stand-single-primary-pg-stand-managed \
  psql -h 127.0.0.1 -p 6433 -U postgres -d app_01 -c 'select current_user;'
```

The `docker exec` examples use PostgreSQL's local Unix socket. For a TLS profile, use the host
`psql`/`pgbench` certificate examples above when validating the externally published TCP policy.

Inspect physical and logical replication directly:

```bash
docker exec pg-stand-sync-logical-primary-pg-stand-managed \
  psql -U postgres -d app_01 -x -c 'select * from pg_stat_replication;'

docker exec pg-stand-sync-logical-primary-pg-stand-managed \
  psql -U postgres -d app_01 -x -c 'select * from pg_replication_slots;'

docker exec pg-stand-sync-logical-subscriber-pg-stand-managed \
  psql -U postgres -d app_01 -x -c 'select * from pg_stat_subscription;'
```

Observe live container resources and the isolated Docker network:

```bash
docker stats pg-stand-single-primary-pg-stand-managed

docker network inspect pg-stand-single-network-pg-stand-managed
```

The PostgreSQL CSV files and `pgbouncer-session.log`/`pgbouncer-transaction.log` are
bind-mounted under each node's log directory. They are not the same as the container stdout
shown by `docker logs`. Both views can be useful:

```bash
docker logs --tail 100 pg-stand-single-primary-pg-stand-managed

docker exec pg-stand-single-primary-pg-stand-managed \
  sh -c 'cat "$PGDATA/current_logfiles"; ls -lh /pglog'

docker exec pg-stand-single-primary-pg-stand-managed \
  sh -c 'file=$(awk '\''$1 == "csvlog" {print $2; exit}'\'' "$PGDATA/current_logfiles"); tail -n 100 "$file"'
```

Use lifecycle operations through `pg-stand`, not `docker stop`, `docker rm`, or
`docker network rm`. The CLI verifies ownership, restores replication readiness, maintains the
single-active-stand lease, and preserves the declared storage semantics.

## Replication

On the first `up` of a physical-replication profile, `pg-stand`:

1. starts the primary and creates or updates the `REPLICATION` login role;
2. applies the managed password or certificate-only network policy in `pg_hba.conf`;
3. runs a full `pg_basebackup` into the standby data and WAL bind mounts;
4. creates the version-compatible recovery configuration and starts the standby;
5. waits for `streaming` in `pg_stat_replication`;
6. activates the requested asynchronous or synchronous mode.

The synchronous profile sets `FIRST 1 (<application_name>)` and becomes ready only after
`sync_state = 'sync'`. The asynchronous profile clears `synchronous_standby_names` and waits
for `sync_state = 'async'`.

The `primary-standby-sync-logical.yaml` profile also starts an independent PostgreSQL
subscriber for `app_01`. Its `FOR ALL TABLES` publication and subscription both exist only in
that database; `app_02` through `app_10` are not logically streamed. Initial `up` transfers the
`app_01` schema with
`pg_dump --schema-only`; PostgreSQL then copies subscription data. Logical replication does
not automatically transfer later DDL or sequence state.

After creating tables on the primary, run:

```bash
pg-stand -c configs/primary-standby-sync-logical.yaml logical sync
```

The incremental command discovers tables missing from the subscriber, transfers their DDL,
and runs `ALTER SUBSCRIPTION ... REFRESH PUBLICATION WITH (copy_data = true)`. The `--full`
variant drops the subscription and slot, clears only the logical node's storage, and fully
reseeds it. The primary and synchronous physical standby remain online.

## Test accounts, generated credentials, and SSH

Every initialized primary and logical-subscriber cluster contains the following databases and
owners. A physical standby receives the same complete cluster through `pg_basebackup`:

```text
app_01 -> user_app_01
app_02 -> user_app_02
...
app_10 -> user_app_10
```

The ten owner passwords, the `postgres` password, and the `replicator` password are generated
once with a cryptographic random generator. All 12 values are distinct and live only in
`.pg_stand/credentials/database/passwords.json`, mode `0600`. The file records account names so
that a later profile cannot accidentally reuse it with different superuser or replication role
names. It is reused across stand recreations and removed only by
`cleanup run --all --force`; the next `up` then creates a new set.

For example, connect as the owner of `app_03` in non-TLS mode:

```bash
PGPASSWORD="$(jq -r '.applications[] | select(.database == "app_03") | .password' \
  .pg_stand/credentials/database/passwords.json)" \
  psql -h 127.0.0.1 -p 55432 -U user_app_03 -d app_03
```

The generated SSH public key is mounted read-only and installed in
`/root/.ssh/authorized_keys`; password-based SSH login is disabled. Connect to the primary of
the single-node profile with:

```bash
ssh -i .pg_stand/credentials/ssh/pg_stand_test -p 56432 root@127.0.0.1
```

All generated credentials are local and ignored by Git. Never reuse them outside an isolated
test stand. Custom YAML files can still use `${NAME}` and `${NAME:-default}` interpolation for
non-secret configuration. Password fields are rejected by the strict YAML parser, so `show`
cannot expose database secrets.

## Configuration format

The API version is `pg_stand/v1`. The strict JSON Schema is
`schema/pg_stand-v1.schema.json`; unknown fields are errors.

- `spec.postgres` defines the major, image, superuser, initdb arguments, optional TLS
  mode, and server parameters.
- `spec.topology` selects `single`, `primary_standby`, or `primary_standby_logical` and
  contains physical and logical replication settings.
- `spec.docker` defines pull policy, the isolated bridge network, and additional labels.
- `spec.storage` defines the relative host-storage root inside the project.
- `spec.nodes` defines container names, PostgreSQL, SSH, session-pooler and transaction-pooler
  ports, bind address, CPU and RAM limits, and `/dev/shm` size.

Parameters controlled by `pg-stand`, including data paths, TLS certificate paths and enablement,
the fixed CSV log layout and rotation, replication settings, `shared_preload_libraries`, and
recovery connection settings, cannot be overridden through `spec.postgres.parameters`.
Diagnostic thresholds and statistics settings are overridable. The tool also owns the
`initdb --pgdata` and `--waldir` paths.

## Orchestrator integration

The normal lifecycle CLI remains human-oriented. Authors of `pg_play`-compatible
orchestrators can use the separate [versioned machine contract](docs/pg_play-integration.md).

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/pytest
PG_STAND_BIN=.venv/bin/pg-stand sh tests/integration_tls.sh
PG_STAND_POSTGRES_VERSION=10 PG_STAND_BIN=.venv/bin/pg-stand sh tests/integration_tls.sh
PG_STAND_POSTGRES_VERSION=18 PG_STAND_BIN=.venv/bin/pg-stand sh tests/integration_tls.sh
```

The Docker integration test performs real certificate-only single-node and
synchronous-physical-plus-logical deployments, negative authentication checks, a short
certificate-authenticated pgbench run, successful SQL plus plaintext-rejection checks on both
PgBouncer pool modes, replication of a newly added `app_01` table, and a
negative check that an `app_02` table reaches only the physical standby. The GitHub Actions
workflow runs it after the Python unit-test matrix.

Runtime code is split by responsibility: `runtime_state` owns applied state and bind-mounted
storage, `runtime_docker` owns images/cleanup/preflight, `runtime_replication` owns physical and
logical replication, `runtime_observe` owns health/status/resources/events, and `runtime_nodes`
owns node actions, connections, SQL, and CSV logs. `runtime.StandManager` composes these focused
mixins and remains the stable public API. `credentials` and `database_credentials` atomically
generate, report, validate, and clean the ignored database/SSH/TLS credential sets. CLI argument
construction lives in `cli_parser` while `cli` handles execution and output. The `assets` module
owns packaged Docker build inputs and safe project initialization, keeping installed-wheel
operation independent of the source repository layout.

Build and validate the same artifacts used by the release workflow with:

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
```

A tag matching the package version, for example `v0.2.0`, runs unit and Docker integration
tests, builds the wheel and source distribution, smoke-tests the installed wheel, and publishes
through PyPI Trusted Publishing. The GitHub `pypi` environment and a PyPI trusted publisher for
`O2eg/pg_stand` and `.github/workflows/publish.yml` must be configured before the first release.

## License

`pg_stand` is distributed under the
[MIT License](https://github.com/O2eg/pg_stand/blob/main/LICENSE). The license file is included
in both wheel and source distributions.

Primary references: [official PostgreSQL image PGDATA layout](https://github.com/docker-library/docs/blob/master/postgres/README.md#pgdata), [`initdb --waldir`](https://www.postgresql.org/docs/current/app-initdb.html), [`pg_basebackup --waldir`](https://www.postgresql.org/docs/current/app-pgbasebackup.html), [PostgreSQL TLS](https://www.postgresql.org/docs/current/ssl-tcp.html), [certificate authentication](https://www.postgresql.org/docs/current/auth-cert.html), [libpq TLS parameters](https://www.postgresql.org/docs/current/libpq-ssl.html), [PgBouncer configuration](https://www.pgbouncer.org/config), [PgBouncer 1.25.2](https://www.pgbouncer.org/2026/05/pgbouncer-1-25-2), and [logical replication restrictions](https://www.postgresql.org/docs/current/logical-replication-restrictions.html).
