"""Strict declarative configuration for PostgreSQL Docker stands."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import shlex
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from pg_stand.database_credentials import DEFAULT_APPLICATION_DATABASE
from pg_stand.errors import ConfigError

API_VERSION = "pg_stand/v1"
KIND = "PostgreSQLStand"
SUPPORTED_POSTGRES_MAJORS = tuple(range(10, 19))
MANAGED_RESOURCE_SUFFIX = "-pg-stand-managed"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_PARAMETER_RE = re.compile(r"^[a-z][a-z0-9_.]*$")
_MEMORY_LIMIT_RE = re.compile(r"^[1-9][0-9]*(?:[bkmg])?$", re.IGNORECASE)
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")
MANAGED_POSTGRES_PARAMETERS = frozenset(
    {
        "data_directory",
        "hba_file",
        "hot_standby",
        "hot_standby_feedback",
        "ident_file",
        "listen_addresses",
        "log_destination",
        "log_directory",
        "log_file_mode",
        "log_filename",
        "log_rotation_age",
        "log_rotation_size",
        "log_truncate_on_rotation",
        "logging_collector",
        "max_replication_slots",
        "max_wal_senders",
        "password_encryption",
        "primary_conninfo",
        "primary_slot_name",
        "shared_preload_libraries",
        "ssl",
        "ssl_ca_file",
        "ssl_cert_file",
        "ssl_key_file",
        "synchronous_commit",
        "synchronous_standby_names",
        "wal_keep_segments",
        "wal_keep_size",
        "wal_level",
    }
)


@dataclass(frozen=True)
class MetadataConfig:
    name: str


@dataclass(frozen=True)
class TLSConfig:
    enabled: bool


@dataclass(frozen=True)
class PostgresConfig:
    version: int
    image: str | None
    base_image: str | None
    database: str
    superuser: str
    initdb_args: str
    parameters: dict[str, str]
    tls: TLSConfig

    @property
    def image_name(self) -> str:
        if self.image:
            return self._resolve_image_template(self.image, "postgres.image")
        return f"pg-stand/postgres:{self.version}{MANAGED_RESOURCE_SUFFIX}"

    @property
    def base_image_name(self) -> str:
        if self.base_image:
            return self._resolve_image_template(self.base_image, "postgres.base_image")
        if self.version in {10, 11}:
            return f"postgres:{self.version}-bullseye"
        return f"postgres:{self.version}"

    @property
    def uses_managed_image(self) -> bool:
        return self.image is None

    def _resolve_image_template(self, template: str, label: str) -> str:
        result = template.replace("{version}", str(self.version))
        if "{" in result or "}" in result:
            raise ConfigError(f"{label} supports only the {{version}} placeholder")
        return result


@dataclass(frozen=True)
class ReplicationConfig:
    mode: str
    username: str
    application_name: str
    max_wal_senders: int
    max_replication_slots: int
    wal_keep_size: str
    wal_keep_segments: int
    hot_standby_feedback: bool


@dataclass(frozen=True)
class LogicalReplicationConfig:
    publication_name: str
    subscription_name: str
    slot_name: str
    copy_data: bool


@dataclass(frozen=True)
class TopologyConfig:
    mode: str
    replication: ReplicationConfig | None
    logical_replication: LogicalReplicationConfig | None


@dataclass(frozen=True)
class DockerConfig:
    pull_policy: str
    network_name: str
    labels: dict[str, str]


@dataclass(frozen=True)
class StorageConfig:
    root_directory: str


@dataclass(frozen=True)
class NodeConfig:
    container_name: str
    published_port: int
    ssh_published_port: int
    pgbouncer_session_published_port: int
    pgbouncer_transaction_published_port: int
    bind_address: str
    shm_size: str
    cpu_limit: float
    memory_limit: str


@dataclass(frozen=True)
class StandConfig:
    source: Path
    project_directory: Path
    api_version: str
    kind: str
    metadata: MetadataConfig
    postgres: PostgresConfig
    topology: TopologyConfig
    docker: DockerConfig
    storage: StorageConfig
    primary: NodeConfig
    standby: NodeConfig | None
    logical: NodeConfig | None

    @property
    def is_replicated(self) -> bool:
        return self.topology.replication is not None

    @property
    def has_logical_replica(self) -> bool:
        return self.topology.logical_replication is not None

    @property
    def instance_id(self) -> str:
        return hashlib.sha256(str(self.project_directory).encode("utf-8")).hexdigest()

    @property
    def config_hash(self) -> str:
        document = asdict(self)
        document.pop("source", None)
        document.pop("project_directory", None)
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def with_postgres_version(self, version: int | None) -> StandConfig:
        if version is None:
            return self
        _validate_major(version)
        return replace(self, postgres=replace(self.postgres, version=version))

    def with_tls_enabled(self, enabled: bool | None) -> StandConfig:
        if enabled is None:
            return self
        return replace(
            self,
            postgres=replace(
                self.postgres,
                tls=replace(self.postgres.tls, enabled=enabled),
            ),
        )

    def public_document(self) -> dict[str, Any]:
        document = asdict(self)
        document["source"] = str(self.source)
        document["project_directory"] = str(self.project_directory)
        document["resolved_storage_root"] = str(self.storage_root)
        document["resolved_image"] = self.postgres.image_name
        document["instance_id"] = self.instance_id
        document["config_hash"] = self.config_hash
        return document

    @property
    def storage_root(self) -> Path:
        return (self.project_directory / self.storage.root_directory).resolve()


def _interpolate_environment(text: str, environment: dict[str, str]) -> str:
    def replace_match(match: re.Match[str]) -> str:
        name = match.group(1)
        fallback = match.group(2)
        if name in environment:
            return environment[name]
        if fallback is not None:
            return fallback
        raise ConfigError(f"environment variable {name} is required by the configuration")

    return _ENV_RE.sub(replace_match, text)


def _interpolate_document(value: Any, environment: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _interpolate_environment(value, environment)
    if isinstance(value, list):
        return [_interpolate_document(item, environment) for item in value]
    if isinstance(value, dict):
        return {key: _interpolate_document(item, environment) for key, item in value.items()}
    return value


def _mapping(value: Any, label: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"unknown {label} key(s): {', '.join(unknown)}")
    return value


def _required(mapping: dict[str, Any], key: str, label: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{label}.{key} is required")
    return mapping[key]


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a string")
    result = value.strip()
    if not result:
        raise ConfigError(f"{label} must not be empty")
    if "\n" in result or "\r" in result or "\x00" in result:
        raise ConfigError(f"{label} must be a single line")
    return result


def _name(value: Any, label: str) -> str:
    result = _text(value, label)
    if not _NAME_RE.fullmatch(result):
        raise ConfigError(f"{label} must match {_NAME_RE.pattern}")
    return result


def _managed_resource_name(value: Any, label: str) -> str:
    result = _name(value, label)
    if not result.endswith(MANAGED_RESOURCE_SUFFIX):
        raise ConfigError(f"{label} must end with {MANAGED_RESOURCE_SUFFIX}")
    return result


def _identifier(value: Any, label: str) -> str:
    result = _text(value, label)
    if not _IDENTIFIER_RE.fullmatch(result):
        raise ConfigError(f"{label} must be a simple PostgreSQL identifier")
    return result


def _positive_int(value: Any, label: str, minimum: int = 1) -> int:
    if isinstance(value, (bool, float)):
        raise ConfigError(f"{label} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be an integer") from exc
    if result < minimum:
        raise ConfigError(f"{label} must be at least {minimum}")
    return result


def _port(value: Any, label: str) -> int:
    result = _positive_int(value, label)
    if result > 65535:
        raise ConfigError(f"{label} must not exceed 65535")
    return result


def _validate_major(version: int) -> None:
    if version not in SUPPORTED_POSTGRES_MAJORS:
        allowed = f"{SUPPORTED_POSTGRES_MAJORS[0]}-{SUPPORTED_POSTGRES_MAJORS[-1]}"
        raise ConfigError(f"postgres.version must be in supported range {allowed}")


def _parameters(value: Any) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ConfigError("spec.postgres.parameters must be a mapping")
    mapping = value
    result: dict[str, str] = {}
    for raw_name, raw_value in mapping.items():
        name = _text(raw_name, "PostgreSQL parameter name")
        if not _PARAMETER_RE.fullmatch(name):
            raise ConfigError(f"invalid PostgreSQL parameter name: {name}")
        if name in MANAGED_POSTGRES_PARAMETERS:
            raise ConfigError(f"PostgreSQL parameter {name} is managed by pg-stand")
        if isinstance(raw_value, (dict, list)) or raw_value is None:
            raise ConfigError(f"PostgreSQL parameter {name} must be a scalar")
        text_value = str(raw_value)
        if "\n" in text_value or "\r" in text_value:
            raise ConfigError(f"PostgreSQL parameter {name} must be a single-line value")
        result[name] = text_value
    return result


def _labels(value: Any) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ConfigError("spec.docker.labels must be a mapping")
    mapping = value
    return {_text(key, "label name"): _text(item, f"label {key}") for key, item in mapping.items()}


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be true or false")
    return value


def _positive_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigError(f"{label} must be a positive number")
    return float(value)


def _memory_limit(value: Any, label: str) -> str:
    result = _text(value, label)
    if not _MEMORY_LIMIT_RE.fullmatch(result):
        raise ConfigError(f"{label} must be a positive Docker byte value such as 512m or 2g")
    return result


def _bind_address(value: Any, label: str) -> str:
    result = _text(value, label)
    try:
        ipaddress.ip_address(result)
    except ValueError as exc:
        raise ConfigError(f"{label} must be an IPv4 or IPv6 address") from exc
    return result


def _published_ports_conflict(first: NodeConfig, second: NodeConfig) -> bool:
    wildcard_addresses = {"0.0.0.0", "::"}
    addresses_conflict = (
        first.bind_address == second.bind_address
        or first.bind_address in wildcard_addresses
        or second.bind_address in wildcard_addresses
    )
    if not addresses_conflict:
        return False
    first_ports = {
        first.published_port,
        first.ssh_published_port,
        first.pgbouncer_session_published_port,
        first.pgbouncer_transaction_published_port,
    }
    second_ports = {
        second.published_port,
        second.ssh_published_port,
        second.pgbouncer_session_published_port,
        second.pgbouncer_transaction_published_port,
    }
    return bool(first_ports & second_ports)


def _relative_directory(value: Any, label: str) -> str:
    result = Path(_text(value, label))
    if result.is_absolute() or result == Path(".") or ".." in result.parts:
        raise ConfigError(f"{label} must be a relative subdirectory of the project directory")
    return str(result)


def _initdb_args(value: Any) -> str:
    result = _text(value, "postgres.initdb_args")
    try:
        tokens = shlex.split(result)
    except ValueError as exc:
        raise ConfigError(f"invalid postgres.initdb_args: {exc}") from exc
    forbidden = {"-D", "--pgdata", "-X", "--waldir"}
    for token in tokens:
        option = token.split("=", 1)[0]
        if option in forbidden:
            raise ConfigError(f"initdb option {option} is managed by pg-stand")
    return result


def _node(value: Any, label: str, *, defaults: dict[str, Any]) -> NodeConfig:
    mapping = _mapping(
        value,
        label,
        {
            "container_name",
            "published_port",
            "ssh_published_port",
            "pgbouncer_session_published_port",
            "pgbouncer_transaction_published_port",
            "bind_address",
            "shm_size",
            "cpu_limit",
            "memory_limit",
        },
    )
    node = NodeConfig(
        container_name=_managed_resource_name(
            mapping.get("container_name", defaults["container_name"]), f"{label}.container_name"
        ),
        published_port=_port(
            _required(mapping, "published_port", label), f"{label}.published_port"
        ),
        ssh_published_port=_port(
            _required(mapping, "ssh_published_port", label),
            f"{label}.ssh_published_port",
        ),
        pgbouncer_session_published_port=_port(
            _required(mapping, "pgbouncer_session_published_port", label),
            f"{label}.pgbouncer_session_published_port",
        ),
        pgbouncer_transaction_published_port=_port(
            _required(mapping, "pgbouncer_transaction_published_port", label),
            f"{label}.pgbouncer_transaction_published_port",
        ),
        bind_address=_bind_address(
            mapping.get("bind_address", "127.0.0.1"), f"{label}.bind_address"
        ),
        shm_size=_memory_limit(mapping.get("shm_size", "256m"), f"{label}.shm_size"),
        cpu_limit=_positive_number(_required(mapping, "cpu_limit", label), f"{label}.cpu_limit"),
        memory_limit=_memory_limit(
            _required(mapping, "memory_limit", label), f"{label}.memory_limit"
        ),
    )
    service_ports = {
        node.published_port,
        node.ssh_published_port,
        node.pgbouncer_session_published_port,
        node.pgbouncer_transaction_published_port,
    }
    if len(service_ports) != 4:
        raise ConfigError(f"{label} must use different PostgreSQL, SSH, and PgBouncer ports")
    return node


def load_config(
    path: str | Path,
    *,
    environment: dict[str, str] | None = None,
    postgres_version: int | None = None,
    tls_enabled: bool | None = None,
    project_directory: str | Path | None = None,
    postgres_parameters: dict[str, Any] | None = None,
) -> StandConfig:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ConfigError(f"configuration file does not exist: {source}")
    raw_text = source.read_text(encoding="utf-8")
    try:
        document = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"cannot parse YAML configuration: {exc}") from exc
    document = _interpolate_document(
        document, dict(os.environ if environment is None else environment)
    )
    root = _mapping(document, "document", {"api_version", "kind", "metadata", "spec"})
    api_version = _text(_required(root, "api_version", "document"), "api_version")
    kind = _text(_required(root, "kind", "document"), "kind")
    if api_version != API_VERSION:
        raise ConfigError(f"api_version must be {API_VERSION}")
    if kind != KIND:
        raise ConfigError(f"kind must be {KIND}")

    metadata = _mapping(_required(root, "metadata", "document"), "metadata", {"name"})
    stand_name = _name(_required(metadata, "name", "metadata"), "metadata.name")
    spec = _mapping(
        _required(root, "spec", "document"),
        "spec",
        {"postgres", "topology", "docker", "storage", "nodes"},
    )
    postgres_raw = _mapping(
        _required(spec, "postgres", "spec"),
        "spec.postgres",
        {
            "version",
            "image",
            "base_image",
            "superuser",
            "initdb_args",
            "parameters",
            "tls",
        },
    )
    version = _positive_int(_required(postgres_raw, "version", "spec.postgres"), "postgres.version")
    _validate_major(version)
    tls_raw = _mapping(postgres_raw.get("tls", {}), "spec.postgres.tls", {"enabled"})
    parameters = _parameters(postgres_raw.get("parameters", {}))
    if postgres_parameters is not None:
        parameters.update(_parameters(postgres_parameters))
    postgres = PostgresConfig(
        version=version,
        image=(
            _text(postgres_raw["image"], "postgres.image")
            if postgres_raw.get("image") is not None
            else None
        ),
        base_image=(
            _text(postgres_raw["base_image"], "postgres.base_image")
            if postgres_raw.get("base_image") is not None
            else None
        ),
        database=DEFAULT_APPLICATION_DATABASE,
        superuser=_identifier(postgres_raw.get("superuser", "postgres"), "postgres.superuser"),
        initdb_args=_initdb_args(
            postgres_raw.get("initdb_args", "--data-checksums --auth-host=scram-sha-256")
        ),
        parameters=parameters,
        tls=TLSConfig(
            enabled=_boolean(tls_raw.get("enabled", False), "postgres.tls.enabled"),
        ),
    )

    topology_raw = _mapping(
        _required(spec, "topology", "spec"),
        "spec.topology",
        {"mode", "replication", "logical_replication"},
    )
    topology_mode = _text(_required(topology_raw, "mode", "spec.topology"), "topology.mode")
    if topology_mode not in {"single", "primary_standby", "primary_standby_logical"}:
        raise ConfigError(
            "topology.mode must be single, primary_standby, or primary_standby_logical"
        )
    replication: ReplicationConfig | None = None
    logical_replication: LogicalReplicationConfig | None = None
    replication_raw = topology_raw.get("replication")
    logical_replication_raw = topology_raw.get("logical_replication")
    if topology_mode == "single":
        if replication_raw is not None:
            raise ConfigError("single topology must not define topology.replication")
        if logical_replication_raw is not None:
            raise ConfigError("single topology must not define topology.logical_replication")
    else:
        repl = _mapping(
            replication_raw,
            "spec.topology.replication",
            {
                "mode",
                "username",
                "application_name",
                "max_wal_senders",
                "max_replication_slots",
                "wal_keep_size",
                "wal_keep_segments",
                "hot_standby_feedback",
            },
        )
        replication_mode = _text(
            _required(repl, "mode", "topology.replication"), "replication.mode"
        )
        if replication_mode not in {"async", "sync"}:
            raise ConfigError("replication.mode must be async or sync")
        replication = ReplicationConfig(
            mode=replication_mode,
            username=_identifier(repl.get("username", "replicator"), "replication.username"),
            application_name=_identifier(
                repl.get("application_name", "pg_standby"), "replication.application_name"
            ),
            max_wal_senders=_positive_int(
                repl.get("max_wal_senders", 10), "replication.max_wal_senders"
            ),
            max_replication_slots=_positive_int(
                repl.get("max_replication_slots", 10), "replication.max_replication_slots"
            ),
            wal_keep_size=_text(repl.get("wal_keep_size", "256MB"), "replication.wal_keep_size"),
            wal_keep_segments=_positive_int(
                repl.get("wal_keep_segments", 64), "replication.wal_keep_segments"
            ),
            hot_standby_feedback=_boolean(
                repl.get("hot_standby_feedback", True), "replication.hot_standby_feedback"
            ),
        )
        if topology_mode == "primary_standby":
            if logical_replication_raw is not None:
                raise ConfigError(
                    "primary_standby topology must not define topology.logical_replication"
                )
        else:
            if replication_mode != "sync":
                raise ConfigError(
                    "primary_standby_logical requires synchronous physical replication"
                )
            logical_raw = _mapping(
                logical_replication_raw,
                "spec.topology.logical_replication",
                {"publication_name", "subscription_name", "slot_name", "copy_data"},
            )
            logical_replication = LogicalReplicationConfig(
                publication_name=_identifier(
                    logical_raw.get("publication_name", "pg_stand_all_tables"),
                    "logical_replication.publication_name",
                ),
                subscription_name=_identifier(
                    logical_raw.get("subscription_name", "pg_stand_all_tables_sub"),
                    "logical_replication.subscription_name",
                ),
                slot_name=_identifier(
                    logical_raw.get("slot_name", "pg_stand_all_tables_slot"),
                    "logical_replication.slot_name",
                ),
                copy_data=_boolean(
                    logical_raw.get("copy_data", True), "logical_replication.copy_data"
                ),
            )

    docker_raw = _mapping(
        spec.get("docker", {}),
        "spec.docker",
        {"pull_policy", "network_name", "labels"},
    )
    pull_policy = _text(docker_raw.get("pull_policy", "missing"), "docker.pull_policy")
    if pull_policy not in {"always", "missing", "never"}:
        raise ConfigError("docker.pull_policy must be always, missing, or never")
    docker = DockerConfig(
        pull_policy=pull_policy,
        network_name=_managed_resource_name(
            docker_raw.get("network_name", f"{stand_name}-network{MANAGED_RESOURCE_SUFFIX}"),
            "docker.network_name",
        ),
        labels=_labels(docker_raw.get("labels", {})),
    )

    storage_raw = _mapping(
        spec.get("storage", {}),
        "spec.storage",
        {"root_directory"},
    )
    storage = StorageConfig(
        root_directory=_relative_directory(
            storage_raw.get("root_directory", f".pg_stand/{stand_name}"),
            "storage.root_directory",
        )
    )

    nodes_raw = _mapping(
        _required(spec, "nodes", "spec"),
        "spec.nodes",
        {"primary", "standby", "logical"},
    )
    primary = _node(
        _required(nodes_raw, "primary", "spec.nodes"),
        "spec.nodes.primary",
        defaults={
            "container_name": f"{stand_name}-primary{MANAGED_RESOURCE_SUFFIX}",
        },
    )
    standby: NodeConfig | None = None
    logical: NodeConfig | None = None
    if topology_mode in {"primary_standby", "primary_standby_logical"}:
        standby = _node(
            _required(nodes_raw, "standby", "spec.nodes"),
            "spec.nodes.standby",
            defaults={
                "container_name": f"{stand_name}-standby{MANAGED_RESOURCE_SUFFIX}",
            },
        )
        if topology_mode == "primary_standby_logical":
            logical = _node(
                _required(nodes_raw, "logical", "spec.nodes"),
                "spec.nodes.logical",
                defaults={"container_name": f"{stand_name}-logical{MANAGED_RESOURCE_SUFFIX}"},
            )
        elif nodes_raw.get("logical") is not None:
            raise ConfigError("primary_standby topology must not define nodes.logical")
    elif nodes_raw.get("standby") is not None:
        raise ConfigError("single topology must not define nodes.standby")
    elif nodes_raw.get("logical") is not None:
        raise ConfigError("single topology must not define nodes.logical")

    configured_nodes = [primary]
    if standby is not None:
        configured_nodes.append(standby)
    if logical is not None:
        configured_nodes.append(logical)
    for index, first in enumerate(configured_nodes):
        for second in configured_nodes[index + 1 :]:
            if _published_ports_conflict(first, second):
                raise ConfigError("stand nodes must not publish the same host address and port")
            if first.container_name == second.container_name:
                raise ConfigError("stand nodes must use different container names")

    resolved_project_directory = Path(
        Path.cwd() if project_directory is None else project_directory
    ).resolve()
    storage_root = (resolved_project_directory / storage.root_directory).resolve()
    if (
        storage_root == resolved_project_directory
        or resolved_project_directory not in storage_root.parents
    ):
        raise ConfigError("storage.root_directory must resolve inside the project directory")

    config = StandConfig(
        source=source,
        project_directory=resolved_project_directory,
        api_version=api_version,
        kind=kind,
        metadata=MetadataConfig(name=stand_name),
        postgres=postgres,
        topology=TopologyConfig(
            mode=topology_mode,
            replication=replication,
            logical_replication=logical_replication,
        ),
        docker=docker,
        storage=storage,
        primary=primary,
        standby=standby,
        logical=logical,
    )
    return config.with_postgres_version(postgres_version).with_tls_enabled(tls_enabled)
