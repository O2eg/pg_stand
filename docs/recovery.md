# Recovery and upgrades

Use the original project directory and profile when recovering a stand. Its resolved
absolute path defines ownership; the profile selects resource names and storage paths.
Repeat any `--pg-version` and `--tls`/`--no-tls` overrides used for the original startup.
The examples below assume those settings are already recorded in `configs/single.yaml`
and that commands run from that project's root. Substitute the original profile for
replicated stands.

## Inspect an interrupted startup

After the Docker daemon is available again, inspect the project before changing it:

```bash
pg-stand --version
pg-stand -c configs/single.yaml validate
pg-stand list
pg-stand --project . active
pg-stand -c configs/single.yaml status
pg-stand -c configs/single.yaml plan
pg-stand -c configs/single.yaml doctor
```

Run these inspection commands individually: a nonzero result is expected when a stand
is incomplete or a prerequisite is missing. `list` reports leases, including partial
startups; an entry does not prove that all nodes are healthy. Conversely, a stopped
project may have data and containers without an active lease. `status` shows runtime
state and drift, `plan` explains the next action, and `doctor` checks prerequisites.

Inspect PostgreSQL logs if a node reached database startup:

```bash
pg-stand -c configs/single.yaml logs primary --tail 100
```

If the node failed before PostgreSQL created its CSV log, inspect the container's
startup log with the Docker CLI using the exact name from `status`. Treat captured
logs and Docker inspection output as potentially sensitive.

Resolve the reported cause first: for example, free required disk space, restore access
to an image registry, or recreate a missing caller-owned external network with its
intended configuration. With the same profile, complete credentials, and compatible
existing storage, retry the idempotent startup and check readiness:

```bash
pg-stand -c configs/single.yaml up
pg-stand -c configs/single.yaml health
```

If `up` reports configuration drift, restore the original configuration or review `plan`
and use its required `apply --reload` or `apply --restart` action. Do not retry a blocked
plan unchanged. An interrupted apply can leave partially updated nodes; inspect each
configured node before choosing another mutation. A reviewed `recreate` without
`--clear-data` rebuilds containers around the existing storage when container recreation
is needed. It does not repair corrupt or incompatible PostgreSQL data.

## Locks, leases, and conflicts

The project operation lock and the Docker active lease have different lifetimes:

- `.pg_stand-operation.lock` is a local file protected by a process-held POSIX lock.
  Process exit releases the lock; the file may remain. Its presence does not mean the
  project is busy. If a command reports an active operation, wait for or investigate
  the process. Do not unlink the file to bypass a live lock.
- The active lease is a labeled Docker network. It may survive a host restart or
  partial startup. Resume the owning profile with `up`, or release its lease through
  `stop`/`down` using that profile. Do not remove the network as a substitute for
  stopping the owner.

If another profile owns the same project's lease, select its original configuration
and remove its containers while keeping data before switching profiles:

```bash
pg-stand -c configs/primary-standby-sync.yaml down
pg-stand -c configs/single.yaml plan
```

This example applies only when `primary-standby-sync.yaml` is the reported owner.
`down` uses the resource names in the supplied configuration; it cannot infer an old
profile after you overwrite or rename it. Recover that configuration from your backup
first. For stands that must remain online concurrently, use independent project
directories with unique names and disjoint ports, as described in
[multiple stands](multiple-stands.md).

For a conflict with another project's name, port, or bind mount, change the new project's
configuration or stop the conflicting workload through its owner. Do not remove foreign
resources to make a plan pass. Restoring an external network is the caller's responsibility;
pg-stand only attaches to it.

## Missing credentials or a moved project

A partially present database, SSH, or TLS credential set is rejected. Stop the affected nodes
with the original profile before replacing credentials or restoring database files. Restore
the complete matching credential set from a trusted backup, preserving file ownership and modes. Do not
delete the remaining credentials to force regeneration against existing data: this can
change passwords, certificate trust, or SSH identity. The generated CA private key is not
retained, so missing certificates cannot be reissued from it. If the original credentials
cannot be recovered, there is no automatic credential-repair command; preserve the data
and use a deliberate database recovery procedure, or discard the disposable stand.

Moving the project changes its instance identity. Existing containers still reference
the old bind-mount paths. Restore the original resolved absolute path and use the original
configuration to operate or stop those resources. A copied directory is not automatically
adopted by `--project`, and editing Docker labels is not a supported migration procedure.
To move permanently, first stop/remove the old resources through their original project
and plan a database transfer to a separately initialized project.

For data you need to retain, keep the profile, version/TLS overrides, complete credentials,
and a consistent backup of the configured storage root together. The storage root includes
separate data and WAL directories plus the applied-state record; copying PGDATA alone is
insufficient. Do not copy live database files as an ordinary directory backup. Use a
PostgreSQL backup procedure, or stop all configured nodes before taking a cold filesystem
copy that preserves ownership and permissions. pg-stand does not provide backup/restore
automation. See [PostgreSQL file-system backups](https://www.postgresql.org/docs/current/backup-file.html)
for the consistency requirements.

## Preserve data or deliberately discard it

| Operation | What it retains or removes |
|---|---|
| `stop` | Stops configured nodes and releases the lease; retains containers, mounted storage, and credentials |
| `down` | Removes configured containers and the private network, releases the lease; retains storage and credentials |
| `recreate` / `apply --restart` | Replaces containers while retaining mounted data and credentials; startup can still fail if prerequisites are unresolved |
| `down --clear-data` | Removes containers and clears the selected profile's data, WAL, and logs; retains credentials |
| `recreate --clear-data` | Clears that storage and initializes fresh databases; retains credentials |
| `cleanup run --all --force` | Removes the selected profile's containers/storage and the project's shared credentials; does not select images/cache or caller-owned external networks |

All profiles in one project share credentials, including profiles whose databases are
currently stopped. Before `cleanup --all`, decide whether any of those databases must
remain usable with the existing credentials. Use `down` without data-clearing options
when the intent is to stop and investigate. After an intentional destructive cleanup,
inspect `list` and `status` before deleting leftover host directories.

## Apply a managed-image upgrade

Updating the Python package leaves existing project YAML and containers unchanged. Review
new templates separately as described in the [installation instructions](../README.md#installation).
To apply a managed-image fix, including image schema 7's PgBouncer restart fix, schedule
downtime and run the following with the original profile and PostgreSQL major:

```bash
pg-stand -c configs/single.yaml image build
pg-stand -c configs/single.yaml recreate
pg-stand -c configs/single.yaml health
```

This preserves data but recreates all nodes configured by the profile. A plain `up` or
container restart does not replace an existing container's image. Managed image tags are
shared across projects for the same major: rebuilding a tag can also affect other projects
when they next create containers. Coordinate the rebuild accordingly. For custom images,
rebuild and tag them outside pg-stand, update `spec.postgres.image`, then review `plan` and
apply the required restart.

Changing the PostgreSQL major is a database migration, not an image-fix upgrade. Existing
`PG_VERSION` mismatches are blocked. Use a planned PostgreSQL upgrade/transfer procedure;
`--clear-data` means discarding the old databases, not upgrading them.
