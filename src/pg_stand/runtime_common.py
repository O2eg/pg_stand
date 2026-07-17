"""Shared constants and PostgreSQL configuration helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker
from docker.errors import (
    DockerException,
)

from pg_stand.config import MANAGED_RESOURCE_SUFFIX, StandConfig
from pg_stand.errors import DockerRuntimeError

PROJECT_LABEL = "io.pg-stand.project"
INSTANCE_LABEL = "io.pg-stand.instance"
NODE_LABEL = "io.pg-stand.node"
CONFIG_HASH_LABEL = "io.pg-stand.config-hash"
MAJOR_LABEL = "io.pg-stand.postgres-major"
MANAGED_LABEL = "io.pg-stand.managed"
RESOURCE_KIND_LABEL = "io.pg-stand.resource-kind"
ACTIVE_LOCK_NETWORK = f"pg-stand-active-lock{MANAGED_RESOURCE_SUFFIX}"
PGWAL = "/pgwal"
PGLOG = "/pglog"
STORAGE_ROOT = "/pg-stand-storage"
IMAGE_SCHEMA_LABEL = "io.pg-stand.image-schema"
IMAGE_SCHEMA_VERSION = "6"
IMAGE_BASE_LABEL = "io.pg-stand.base-image"
IMAGE_FAMILY_LABEL = "io.pg-stand.image-family"
IMAGE_MAJOR_LABEL = "io.pg-stand.postgres-major"
IMAGE_PGBOUNCER_LABEL = "io.pg-stand.pgbouncer-version"
PGBOUNCER_VERSION = "1.25.2"
PGBOUNCER_SESSION_PORT = 6432
PGBOUNCER_TRANSACTION_PORT = 6433
IMAGE_RESOURCE_KIND = "managed-image"
DIAGNOSTIC_EXTENSIONS = (
    "pg_stat_statements",
    "pg_wait_sampling",
    "pg_buffercache",
    "pg_stat_kcache",
)
DIAGNOSTIC_PRELOADS = (
    "pg_stat_statements",
    "pg_wait_sampling",
    "pg_stat_kcache",
    "auto_explain",
)
SSH_SOURCE_MOUNT = "/pg-stand-ssh-source"
TLS_SOURCE_MOUNT = "/pg-stand-tls-source"
TLS_RUNTIME_ROOT = "/pg-stand-tls"
TLS_CA_FILE = f"{TLS_RUNTIME_ROOT}/ca.crt"
TLS_SERVER_CERT_FILE = f"{TLS_RUNTIME_ROOT}/server.crt"
TLS_SERVER_KEY_FILE = f"{TLS_RUNTIME_ROOT}/server.key"
TLS_POSTGRES_CERT_FILE = f"{TLS_RUNTIME_ROOT}/postgres.crt"
TLS_POSTGRES_KEY_FILE = f"{TLS_RUNTIME_ROOT}/postgres.key"
TLS_REPLICATOR_CERT_FILE = f"{TLS_RUNTIME_ROOT}/replicator.crt"
TLS_REPLICATOR_KEY_FILE = f"{TLS_RUNTIME_ROOT}/replicator.key"
FIXED_LOGGING_PARAMETERS = frozenset(
    {
        "log_destination",
        "logging_collector",
        "log_directory",
        "log_filename",
        "log_file_mode",
        "log_truncate_on_rotation",
        "log_rotation_age",
        "log_rotation_size",
    }
)


def _resource_labels(resource: Any) -> dict[str, str]:
    attrs = resource.attrs
    return attrs.get("Labels") or (attrs.get("Config") or {}).get("Labels") or {}


def discover_active_stand(*, client: Any | None = None) -> dict[str, Any]:
    """Discover the single active pg-stand lease without reading a YAML file."""
    docker_client = client or docker.from_env()
    try:
        if not docker_client.ping():
            raise DockerRuntimeError("Docker daemon did not answer ping")
        locks = docker_client.networks.list(
            filters={
                "label": [
                    f"{MANAGED_LABEL}=true",
                    f"{RESOURCE_KIND_LABEL}=active-lock",
                ]
            }
        )
    except DockerException as exc:
        raise DockerRuntimeError(f"cannot connect to Docker daemon: {exc}") from exc
    lock = next(
        (
            item
            for item in locks
            if item.name == ACTIVE_LOCK_NETWORK and item.name.endswith(MANAGED_RESOURCE_SUFFIX)
        ),
        None,
    )
    if lock is None:
        return {"active": False, "project": None, "nodes": {}}
    labels = _resource_labels(lock)
    project = labels.get(PROJECT_LABEL)
    if not project:
        raise DockerRuntimeError("active pg-stand lock has no project ownership label")
    instance = labels.get(INSTANCE_LABEL)
    if not instance:
        raise DockerRuntimeError("active pg-stand lock has no instance ownership label")
    containers = docker_client.containers.list(
        all=True,
        filters={
            "label": [
                f"{MANAGED_LABEL}=true",
                f"{RESOURCE_KIND_LABEL}=stand-node",
                f"{PROJECT_LABEL}={project}",
                f"{INSTANCE_LABEL}={instance}",
            ]
        },
    )
    nodes: dict[str, Any] = {}
    for container in containers:
        if not container.name.endswith(MANAGED_RESOURCE_SUFFIX):
            continue
        container.reload()
        container_labels = _resource_labels(container)
        node = container_labels.get(NODE_LABEL)
        if node not in {"primary", "standby", "logical"}:
            continue
        attrs = container.attrs
        network_settings = attrs.get("NetworkSettings") or {}
        nodes[node] = {
            "container": container.name,
            "id": container.short_id,
            "status": container.status,
            "hostname": (attrs.get("Config") or {}).get("Hostname"),
            "postgres_version": container_labels.get(MAJOR_LABEL),
            "config_hash": container_labels.get(CONFIG_HASH_LABEL),
            "ip_addresses": {
                name: settings.get("IPAddress") or None
                for name, settings in (network_settings.get("Networks") or {}).items()
            },
            "ports": network_settings.get("Ports") or {},
            "limits": {
                "cpu_limit": int((attrs.get("HostConfig") or {}).get("NanoCpus", 0) or 0)
                / 1_000_000_000,
                "memory_bytes": int((attrs.get("HostConfig") or {}).get("Memory", 0) or 0),
            },
        }
    return {
        "active": True,
        "project": project,
        "instance_id": instance,
        "lock": ACTIVE_LOCK_NETWORK,
        "postgres_version": labels.get(MAJOR_LABEL),
        "config_hash": labels.get(CONFIG_HASH_LABEL),
        "nodes": nodes,
    }


@dataclass(frozen=True)
class NodeStoragePaths:
    root: Path
    data: Path
    wal: Path
    log: Path


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _pgpass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _conninfo_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def postgres_data_paths(version: int) -> tuple[str, str]:
    """Return (PGDATA, data bind target) for the official image."""
    if version >= 18:
        return f"/var/lib/postgresql/{version}/docker", "/var/lib/postgresql"
    return "/var/lib/postgresql/data", "/var/lib/postgresql/data"


def managed_dockerfile(version: int) -> tuple[str, str]:
    """Return the compatible managed Dockerfile and its image family label."""
    if version == 10:
        return "docker/Dockerfile.pg10", "pg10"
    if version <= 12:
        return "docker/Dockerfile.pg11-pg12", "pg11-pg12"
    if version <= 17:
        return "docker/Dockerfile.pg13-pg17", "pg13-pg17"
    return "docker/Dockerfile.pg18", "pg18"


def postgres_parameter_file(parameters: dict[str, str]) -> str:
    """Render a deterministic postgresql.conf fragment for declarative parameters."""
    lines = []
    for name, value in sorted(parameters.items()):
        escaped = value.replace("\\", "\\\\").replace("'", "''")
        lines.append(f"{name} = '{escaped}'")
    return "\n".join(lines) + ("\n" if lines else "")


def diagnostic_postgres_settings(version: int) -> list[tuple[str, str]]:
    settings = [
        ("shared_preload_libraries", ",".join(DIAGNOSTIC_PRELOADS)),
        ("track_activities", "on"),
        ("track_counts", "on"),
        ("track_io_timing", "on"),
        ("track_functions", "pl"),
        ("track_activity_query_size", "4096"),
        ("pg_stat_statements.max", "5000"),
        ("pg_stat_statements.track", "all"),
        ("pg_stat_statements.track_utility", "on"),
        ("pg_stat_statements.save", "on"),
        ("pg_stat_kcache.track", "all"),
        ("auto_explain.log_min_duration", "100ms"),
        ("auto_explain.log_analyze", "on"),
        ("auto_explain.log_buffers", "on"),
        ("auto_explain.log_verbose", "on"),
        ("auto_explain.log_timing", "on"),
        ("auto_explain.log_triggers", "on"),
        ("auto_explain.log_nested_statements", "off"),
        ("auto_explain.log_format", "text"),
    ]
    if version >= 13:
        settings.extend(
            [
                ("auto_explain.log_wal", "on"),
                ("pg_stat_statements.track_planning", "on"),
                ("pg_stat_kcache.track_planning", "on"),
            ]
        )
    if version >= 14:
        settings.extend(
            [
                ("compute_query_id", "on"),
                ("track_wal_io_timing", "on"),
            ]
        )
    return settings


def logging_postgres_settings(version: int) -> list[tuple[str, str]]:
    """Return the fixed CSV logging profile with major-version compatibility."""
    settings = [
        ("log_destination", "csvlog"),
        ("logging_collector", "on"),
        ("log_directory", PGLOG),
        ("log_filename", "postgresql-%Y-%m-%d_%H%M%S.log"),
        ("log_file_mode", "0640"),
        ("log_truncate_on_rotation", "on"),
        ("log_rotation_age", "1d"),
        ("log_rotation_size", "256MB"),
        ("log_min_messages", "warning"),
        ("log_min_error_statement", "error"),
        ("log_min_duration_statement", "3000"),
        ("log_duration", "off"),
        ("log_statement", "ddl"),
        ("log_checkpoints", "on"),
        ("log_connections", "" if version >= 18 else "off"),
        ("log_disconnections", "off"),
        ("log_lock_waits", "on"),
        ("deadlock_timeout", "1s"),
        ("log_temp_files", "64MB"),
        ("log_autovacuum_min_duration", "5s"),
        ("log_replication_commands", "on"),
        ("log_hostname", "off"),
        ("log_error_verbosity", "default"),
    ]
    if version >= 14:
        settings.append(("log_recovery_conflict_waits", "on"))
    return settings


def fixed_logging_postgres_settings(version: int) -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, value in logging_postgres_settings(version)
        if name in FIXED_LOGGING_PARAMETERS
    ]


def effective_postgres_parameters(config: StandConfig) -> dict[str, str]:
    """Merge version-aware diagnostic defaults with user-overridable parameters."""
    result = {
        name: value
        for name, value in logging_postgres_settings(config.postgres.version)
        if name not in FIXED_LOGGING_PARAMETERS
    }
    result.update(
        {
            name: value
            for name, value in diagnostic_postgres_settings(config.postgres.version)
            if name != "shared_preload_libraries"
        }
    )
    result.update(config.postgres.parameters)
    return result


def tls_postgres_settings(config: StandConfig) -> list[tuple[str, str]]:
    """Return command-line TLS settings which cannot be overridden by a profile."""
    if not config.postgres.tls.enabled:
        return [("ssl", "off")]
    return [
        ("ssl", "on"),
        ("ssl_ca_file", TLS_CA_FILE),
        ("ssl_cert_file", TLS_SERVER_CERT_FILE),
        ("ssl_key_file", TLS_SERVER_KEY_FILE),
    ]


def primary_postgres_command(config: StandConfig) -> list[str]:
    settings: list[tuple[str, str]] = [
        ("listen_addresses", "*"),
        ("password_encryption", "scram-sha-256"),
    ]
    settings.extend(fixed_logging_postgres_settings(config.postgres.version))
    settings.extend(tls_postgres_settings(config))
    settings.append(("shared_preload_libraries", ",".join(DIAGNOSTIC_PRELOADS)))
    replication = config.topology.replication
    if replication is not None:
        settings.extend(
            [
                ("wal_level", "logical" if config.has_logical_replica else "replica"),
                ("max_wal_senders", str(replication.max_wal_senders)),
                ("max_replication_slots", str(replication.max_replication_slots)),
            ]
        )
        if config.postgres.version >= 13:
            settings.append(("wal_keep_size", replication.wal_keep_size))
        else:
            settings.append(("wal_keep_segments", str(replication.wal_keep_segments)))
    command = ["postgres"]
    for name, value in settings:
        command.extend(["-c", f"{name}={value}"])
    return command


def standby_postgres_command(config: StandConfig) -> list[str]:
    replication = config.topology.replication
    if replication is None:
        raise DockerRuntimeError("standby command requested for a single-node stand")
    settings: list[tuple[str, str]] = [
        ("listen_addresses", "*"),
        ("hot_standby", "on"),
        ("hot_standby_feedback", "on" if replication.hot_standby_feedback else "off"),
    ]
    settings.extend(fixed_logging_postgres_settings(config.postgres.version))
    settings.extend(tls_postgres_settings(config))
    settings.append(("shared_preload_libraries", ",".join(DIAGNOSTIC_PRELOADS)))
    command = ["postgres"]
    for name, value in settings:
        command.extend(["-c", f"{name}={value}"])
    return command


def logical_postgres_command(config: StandConfig) -> list[str]:
    if not config.has_logical_replica:
        raise DockerRuntimeError("logical command requested without a logical replica")
    settings: list[tuple[str, str]] = [
        ("listen_addresses", "*"),
        ("password_encryption", "scram-sha-256"),
    ]
    settings.extend(fixed_logging_postgres_settings(config.postgres.version))
    settings.extend(tls_postgres_settings(config))
    settings.append(("shared_preload_libraries", ",".join(DIAGNOSTIC_PRELOADS)))
    command = ["postgres"]
    for name, value in settings:
        command.extend(["-c", f"{name}={value}"])
    return command


def standby_bootstrap_script() -> str:
    return r"""
set -eu
install -d -m 0700 -o postgres -g postgres "$PGDATA_MOUNT"
install -d -m 0700 -o postgres -g postgres "$PGDATA"
install -d -m 0700 -o postgres -g postgres "$PGWAL"
install -d -m 0750 -o postgres -g postgres "$PGLOG"
if [ "$PG_STAND_TLS_ENABLED" = "1" ]; then
  install -d -m 0700 -o postgres -g postgres "$TLS_RUNTIME_ROOT"
  install -m 0644 -o postgres -g postgres "$TLS_SOURCE_MOUNT/ca.crt" \
    "$TLS_RUNTIME_ROOT/ca.crt"
  install -m 0644 -o postgres -g postgres "$TLS_SOURCE_MOUNT/replicator.crt" \
    "$TLS_RUNTIME_ROOT/replicator.crt"
  install -m 0600 -o postgres -g postgres "$TLS_SOURCE_MOUNT/replicator.key" \
    "$TLS_RUNTIME_ROOT/replicator.key"
fi
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  if find "$PGDATA" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "standby data directory is not empty and has no PG_VERSION" >&2
    exit 2
  fi
  if find "$PGWAL" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "standby WAL directory is not empty for a new cluster" >&2
    exit 2
  fi
  if [ "$PG_STAND_TLS_ENABLED" = "1" ]; then
    gosu postgres env \
      PGSSLMODE=verify-ca \
      PGSSLROOTCERT="$TLS_RUNTIME_ROOT/ca.crt" \
      PGSSLCERT="$TLS_RUNTIME_ROOT/replicator.crt" \
      PGSSLKEY="$TLS_RUNTIME_ROOT/replicator.key" \
      pg_basebackup -h "$PRIMARY_HOST" -p 5432 -U "$REPLICATION_USER" \
      -D "$PGDATA" --waldir="$PGWAL" -Fp -Xs -P --checkpoint=fast
  else
    gosu postgres env PGPASSWORD="$REPLICATION_PASSWORD" pg_basebackup \
      -h "$PRIMARY_HOST" -p 5432 -U "$REPLICATION_USER" \
      -D "$PGDATA" --waldir="$PGWAL" -Fp -Xs -P --checkpoint=fast
  fi
fi

if [ -n "$PGPASS_LINE" ]; then
  printf '%s\n' "$PGPASS_LINE" > "$PGDATA/standby.pgpass"
  chmod 0600 "$PGDATA/standby.pgpass"
fi
if [ "$POSTGRES_MAJOR" -ge 12 ]; then
  printf "primary_conninfo = '%s'\n" "$PRIMARY_CONNINFO" \
    > "$PGDATA/pg_stand_replication.conf"
  if ! grep -Fqx "include_if_exists = 'pg_stand_replication.conf'" "$PGDATA/postgresql.conf"; then
    printf "%s\n" "include_if_exists = 'pg_stand_replication.conf'" \
      >> "$PGDATA/postgresql.conf"
  fi
  touch "$PGDATA/standby.signal"
else
  {
    printf "%s\n" "standby_mode = 'on'"
    printf "primary_conninfo = '%s'\n" "$PRIMARY_CONNINFO"
    printf "%s\n" "recovery_target_timeline = 'latest'"
  } > "$PGDATA/recovery.conf"
  chmod 0600 "$PGDATA/recovery.conf"
fi
chown postgres:postgres "$PGDATA"
chown postgres:postgres "$PGWAL" "$PGLOG"
if [ -f "$PGDATA/standby.pgpass" ]; then
  chown postgres:postgres "$PGDATA/standby.pgpass"
fi
if [ "$POSTGRES_MAJOR" -ge 12 ]; then
  chown postgres:postgres "$PGDATA/pg_stand_replication.conf" "$PGDATA/standby.signal"
else
  chown postgres:postgres "$PGDATA/recovery.conf"
fi
""".strip()
