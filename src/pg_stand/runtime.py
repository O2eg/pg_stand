"""Docker SDK lifecycle facade and core container operations."""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from docker.errors import APIError, DockerException

import docker
from pg_stand.config import MANAGED_RESOURCE_SUFFIX, NodeConfig, StandConfig
from pg_stand.credentials import credential_paths, credentials_status, ensure_credentials
from pg_stand.database_credentials import (
    APPLICATION_ACCOUNTS,
    APPLICATION_DATABASES,
    DEFAULT_REPLICATION_USER,
    DatabaseCredentials,
    read_database_credentials,
)
from pg_stand.errors import DockerRuntimeError
from pg_stand.runtime_common import (
    ACTIVE_LOCK_NETWORK,
    CONFIG_HASH_LABEL,
    DIAGNOSTIC_EXTENSIONS,
    DIAGNOSTIC_PRELOADS,
    INSTANCE_LABEL,
    MAJOR_LABEL,
    MANAGED_LABEL,
    NODE_LABEL,
    PGBOUNCER_SESSION_PORT,
    PGBOUNCER_TRANSACTION_PORT,
    PGLOG,
    PGWAL,
    PROJECT_LABEL,
    RESOURCE_KIND_LABEL,
    NodeStoragePaths,
    _sql_identifier,
    _sql_literal,
    diagnostic_postgres_settings,
    discover_active_stand,
    effective_postgres_parameters,
    logging_postgres_settings,
    logical_postgres_command,
    managed_dockerfile,
    postgres_data_paths,
    postgres_parameter_file,
    primary_postgres_command,
    standby_bootstrap_script,
    standby_postgres_command,
)
from pg_stand.runtime_docker import DockerResourceMixin
from pg_stand.runtime_nodes import NodeOperationsMixin
from pg_stand.runtime_observe import ObservationMixin
from pg_stand.runtime_replication import ReplicationMixin
from pg_stand.runtime_state import StateStorageMixin

__all__ = [
    "ACTIVE_LOCK_NETWORK",
    "CONFIG_HASH_LABEL",
    "INSTANCE_LABEL",
    "MANAGED_LABEL",
    "PGLOG",
    "PGWAL",
    "PROJECT_LABEL",
    "RESOURCE_KIND_LABEL",
    "StandManager",
    "diagnostic_postgres_settings",
    "discover_active_stand",
    "effective_postgres_parameters",
    "logging_postgres_settings",
    "managed_dockerfile",
    "postgres_data_paths",
    "postgres_parameter_file",
    "primary_postgres_command",
    "standby_bootstrap_script",
    "standby_postgres_command",
]


class _StandManagerCore:
    def __init__(
        self,
        config: StandConfig,
        *,
        client: Any | None = None,
        output: Callable[[str], None] = print,
    ) -> None:
        self.config = config
        self.client = client or docker.from_env()
        self.output = output

    def _database_accounts(self) -> DatabaseCredentials:
        replication = self.config.topology.replication
        replication_username = (
            replication.username if replication is not None else DEFAULT_REPLICATION_USER
        )
        return read_database_credentials(
            credential_paths(self.config.project_directory).database,
            expected_superuser=self.config.postgres.superuser,
            expected_replication_user=replication_username,
        )

    def _superuser_password(self) -> str:
        return self._database_accounts().superuser_password

    def _replication_password(self) -> str:
        return self._database_accounts().replication_password

    def _labels(
        self,
        *,
        node: str | None = None,
        resource_kind: str | None = None,
    ) -> dict[str, str]:
        if resource_kind is None:
            resource_kind = "stand-node" if node in {"primary", "standby", "logical"} else "helper"
            if node is None:
                resource_kind = "network"
        labels = {
            **self.config.docker.labels,
            PROJECT_LABEL: self.config.metadata.name,
            INSTANCE_LABEL: self.config.instance_id,
            CONFIG_HASH_LABEL: self.config.config_hash,
            MAJOR_LABEL: str(self.config.postgres.version),
            MANAGED_LABEL: "true",
            RESOURCE_KIND_LABEL: resource_kind,
        }
        if node:
            labels[NODE_LABEL] = node
        return labels

    def _managed_filters(
        self,
        *,
        resource_kind: str,
        project: str | None,
    ) -> dict[str, list[str]]:
        labels = [
            f"{MANAGED_LABEL}=true",
            f"{RESOURCE_KIND_LABEL}={resource_kind}",
        ]
        if project is not None:
            labels.append(f"{PROJECT_LABEL}={project}")
            labels.append(f"{INSTANCE_LABEL}={self.config.instance_id}")
        return {"label": labels}

    def _find_owned_container(self, name: str) -> Any | None:
        if not name.endswith(MANAGED_RESOURCE_SUFFIX):
            raise DockerRuntimeError(
                f"refusing to inspect container without {MANAGED_RESOURCE_SUFFIX} suffix: {name}"
            )
        containers = self.client.containers.list(
            all=True,
            filters=self._managed_filters(
                resource_kind="stand-node",
                project=self.config.metadata.name,
            ),
        )
        return next((container for container in containers if container.name == name), None)

    def _find_owned_helper(self, name: str) -> Any | None:
        if not name.endswith(MANAGED_RESOURCE_SUFFIX):
            raise DockerRuntimeError(
                f"refusing to inspect helper without {MANAGED_RESOURCE_SUFFIX} suffix: {name}"
            )
        containers = self.client.containers.list(
            all=True,
            filters=self._managed_filters(
                resource_kind="helper",
                project=self.config.metadata.name,
            ),
        )
        return next((container for container in containers if container.name == name), None)

    def _find_owned_network(self, name: str) -> Any | None:
        if not name.endswith(MANAGED_RESOURCE_SUFFIX):
            raise DockerRuntimeError(
                f"refusing to inspect network without {MANAGED_RESOURCE_SUFFIX} suffix: {name}"
            )
        networks = self.client.networks.list(
            filters=self._managed_filters(
                resource_kind="network",
                project=self.config.metadata.name,
            )
        )
        return next((network for network in networks if network.name == name), None)

    def _find_active_lock(self) -> Any | None:
        networks = self.client.networks.list(
            filters=self._managed_filters(resource_kind="active-lock", project=None)
        )
        return next(
            (network for network in networks if network.name == ACTIVE_LOCK_NETWORK),
            None,
        )

    def _acquire_active_lock(self) -> bool:
        lock = self._find_active_lock()
        if lock is not None:
            labels = lock.attrs.get("Labels") or {}
            owner = labels.get(PROJECT_LABEL)
            if owner != self.config.metadata.name:
                raise DockerRuntimeError(
                    f"stand {owner or '<unknown>'} is already active; stop or remove it first"
                )
            self._assert_resource_labels(
                lock,
                "active stand lock",
                check_hash=False,
                expected_name=ACTIVE_LOCK_NETWORK,
                expected_kind="active-lock",
            )
            return False
        try:
            self.client.networks.create(
                ACTIVE_LOCK_NETWORK,
                driver="bridge",
                labels=self._labels(resource_kind="active-lock"),
                check_duplicate=True,
            )
        except APIError as exc:
            raise DockerRuntimeError(
                "cannot acquire the global active-stand lock; another pg-stand process "
                f"may be starting a stand: {exc}"
            ) from exc
        return True

    def _release_active_lock(self) -> None:
        lock = self._find_active_lock()
        if lock is None:
            return
        labels = lock.attrs.get("Labels") or {}
        if labels.get(PROJECT_LABEL) != self.config.metadata.name:
            return
        self._assert_resource_labels(
            lock,
            "active stand lock",
            check_hash=False,
            expected_name=ACTIVE_LOCK_NETWORK,
            expected_kind="active-lock",
        )
        self.output(f"RELEASE active_lock={ACTIVE_LOCK_NETWORK}")
        lock.remove()

    def _has_running_owned_nodes(self) -> bool:
        containers = self.client.containers.list(
            all=False,
            filters=self._managed_filters(
                resource_kind="stand-node",
                project=self.config.metadata.name,
            ),
        )
        return bool(containers)

    def _configured_nodes(self) -> list[tuple[str, NodeConfig]]:
        result = [("primary", self.config.primary)]
        if self.config.standby is not None:
            result.append(("standby", self.config.standby))
        if self.config.logical is not None:
            result.append(("logical", self.config.logical))
        return result

    def _ping(self) -> None:
        try:
            if not self.client.ping():
                raise DockerRuntimeError("Docker daemon did not answer ping")
        except DockerException as exc:
            raise DockerRuntimeError(f"cannot connect to Docker daemon: {exc}") from exc

    def _ensure_network(self) -> Any:
        name = self.config.docker.network_name
        network = self._find_owned_network(name)
        if network is not None:
            self._assert_resource_labels(
                network,
                "network",
                check_hash=False,
                expected_name=name,
                expected_kind="network",
            )
            return network
        self.output(f"CREATE network={name}")
        return self.client.networks.create(
            name,
            driver="bridge",
            labels=self._labels(resource_kind="network"),
            check_duplicate=True,
        )

    def _assert_resource_labels(
        self,
        resource: Any,
        label: str,
        *,
        check_hash: bool = True,
        expected_name: str | None = None,
        expected_node: str | None = None,
        expected_kind: str | None = None,
    ) -> None:
        attrs = resource.attrs
        labels = attrs.get("Labels")
        if labels is None:
            labels = (attrs.get("Config") or {}).get("Labels")
        labels = labels or {}
        resource_name = getattr(resource, "name", "<unknown>")
        if expected_name is not None and (
            resource_name != expected_name or not expected_name.endswith(MANAGED_RESOURCE_SUFFIX)
        ):
            raise DockerRuntimeError(
                f"refusing to manage {label} with unexpected name {resource_name}"
            )
        if (
            labels.get(PROJECT_LABEL) != self.config.metadata.name
            or labels.get(INSTANCE_LABEL) != self.config.instance_id
            or labels.get(MANAGED_LABEL) != "true"
        ):
            raise DockerRuntimeError(
                f"existing {label} {getattr(resource, 'name', '<unknown>')} is not owned by "
                f"this pg-stand project instance {self.config.metadata.name}"
            )
        if expected_node is not None and labels.get(NODE_LABEL) != expected_node:
            raise DockerRuntimeError(
                f"existing {label} {resource_name} has an unexpected node ownership label"
            )
        if expected_kind is not None and labels.get(RESOURCE_KIND_LABEL) != expected_kind:
            raise DockerRuntimeError(
                f"existing {label} {resource_name} has an unexpected resource-kind label"
            )
        if check_hash and labels.get(CONFIG_HASH_LABEL) != self.config.config_hash:
            raise DockerRuntimeError(
                f"existing {label} was created from a different configuration; "
                "run down and then up to recreate containers"
            )

    def _container_environment(self) -> dict[str, str]:
        postgres = self.config.postgres
        pgdata, _ = postgres_data_paths(postgres.version)
        replication = self.config.topology.replication
        return {
            "POSTGRES_DB": postgres.database,
            "POSTGRES_USER": postgres.superuser,
            "POSTGRES_PASSWORD": self._superuser_password(),
            "POSTGRES_INITDB_ARGS": f"{postgres.initdb_args} --waldir={PGWAL}",
            "PGDATA": pgdata,
            "PG_STAND_TLS_ENABLED": "1" if postgres.tls.enabled else "0",
            "PG_STAND_REPLICATION_USER": replication.username if replication else "",
        }

    def _ensure_database_container(
        self,
        *,
        node_name: str,
        node: NodeConfig,
        storage: NodeStoragePaths,
        network: Any,
        command: list[str],
    ) -> Any:
        container = self._owned_container(node, node_name)
        if container is not None:
            container.reload()
            if container.status != "running":
                self.output(f"START node={node_name} container={node.container_name}")
                container.start()
            return container
        self.output(f"CREATE node={node_name} container={node.container_name}")
        try:
            return self.client.containers.run(
                self.config.postgres.image_name,
                command=command,
                name=node.container_name,
                detach=True,
                environment=self._container_environment(),
                volumes=self._database_binds(storage),
                ports={
                    "5432/tcp": (node.bind_address, node.published_port),
                    "22/tcp": (node.bind_address, node.ssh_published_port),
                    f"{PGBOUNCER_SESSION_PORT}/tcp": (
                        node.bind_address,
                        node.pgbouncer_session_published_port,
                    ),
                    f"{PGBOUNCER_TRANSACTION_PORT}/tcp": (
                        node.bind_address,
                        node.pgbouncer_transaction_published_port,
                    ),
                },
                network=network.name,
                labels=self._labels(node=node_name, resource_kind="stand-node"),
                hostname=node.container_name,
                shm_size=node.shm_size,
                nano_cpus=int(node.cpu_limit * 1_000_000_000),
                mem_limit=node.memory_limit,
                restart_policy={"Name": "unless-stopped"},
            )
        except APIError as exc:
            raise DockerRuntimeError(
                f"cannot create {node_name} container {node.container_name}: {exc}"
            ) from exc

    def _exec(
        self,
        container: Any,
        command: list[str],
        *,
        environment: dict[str, str] | None = None,
        check: bool = True,
    ) -> str:
        result = container.exec_run(command, environment=environment)
        output = (result.output or b"").decode("utf-8", errors="replace").strip()
        if check and result.exit_code != 0:
            raise DockerRuntimeError(
                f"command failed in {container.name} with exit {result.exit_code}: "
                f"{shlex.join(command)}\n{output}"
            )
        return output

    def _psql(
        self,
        container: Any,
        sql: str,
        *,
        database: str = "postgres",
        synchronous_commit_off: bool = False,
    ) -> str:
        environment = {"PGPASSWORD": self._superuser_password()}
        if synchronous_commit_off:
            environment["PGOPTIONS"] = "-c synchronous_commit=off"
        return self._exec(
            container,
            [
                "psql",
                "--set",
                "ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
                "--username",
                self.config.postgres.superuser,
                "--dbname",
                database,
                "--command",
                sql,
            ],
            environment=environment,
        )

    def _wait_for_postgres(self, container: Any, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_output = ""
        while time.monotonic() < deadline:
            container.reload()
            if container.status in {"exited", "dead"}:
                logs = container.logs(tail=100).decode("utf-8", errors="replace")
                raise DockerRuntimeError(
                    f"container {container.name} stopped during startup\n{logs}"
                )
            result = container.exec_run(
                [
                    "/bin/sh",
                    "-ceu",
                    """process=$(cat /proc/1/comm)
if [ "$process" != "postgres" ]; then
  echo "container PID 1 is $process, waiting for final postgres process"
  exit 1
fi
exec pg_isready --username "$1" --dbname postgres
""",
                    "pg-stand-readiness",
                    self.config.postgres.superuser,
                ]
            )
            last_output = (result.output or b"").decode("utf-8", errors="replace").strip()
            if result.exit_code == 0:
                return
            time.sleep(1.0)
        raise DockerRuntimeError(
            f"PostgreSQL in {container.name} did not become ready within {timeout_seconds}s: "
            f"{last_output}"
        )

    def _pgbouncer_probe(self, container: Any, port: int, *, plaintext: bool = False) -> Any:
        environment = {
            "PGSSLMODE": "disable",
            "PGPASSWORD": self._superuser_password(),
        }
        if self.config.postgres.tls.enabled and not plaintext:
            environment = {
                "PGSSLMODE": "verify-ca",
                "PGSSLROOTCERT": "/pg-stand-tls/ca.crt",
                "PGSSLCERT": "/pg-stand-tls/postgres.crt",
                "PGSSLKEY": "/pg-stand-tls/postgres.key",
            }
        return container.exec_run(
            [
                "psql",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--username",
                self.config.postgres.superuser,
                "--dbname",
                self.config.postgres.database,
                "--tuples-only",
                "--no-align",
                "--set",
                "ON_ERROR_STOP=1",
                "--command",
                "select current_database() || '|' || current_user",
            ],
            environment=environment,
        )

    def _wait_for_pgbouncers(self, container: Any, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_output: dict[str, str] = {}
        services = {
            "session": PGBOUNCER_SESSION_PORT,
            "transaction": PGBOUNCER_TRANSACTION_PORT,
        }
        expected = f"{self.config.postgres.database}|{self.config.postgres.superuser}"
        while time.monotonic() < deadline:
            ready = True
            for mode, port in services.items():
                result = self._pgbouncer_probe(container, port)
                output = (result.output or b"").decode("utf-8", errors="replace").strip()
                last_output[mode] = output
                if result.exit_code != 0 or output != expected:
                    ready = False
            if ready:
                return
            time.sleep(1.0)
        details = "; ".join(f"{mode}: {output}" for mode, output in last_output.items())
        raise DockerRuntimeError(
            f"PgBouncer in {container.name} did not become ready within {timeout_seconds}s: "
            f"{details}"
        )

    def _ensure_diagnostic_extensions(self, container: Any) -> None:
        self._exec(
            container,
            [
                "/bin/sh",
                "-ceu",
                'test -f "$(pg_config --pkglibdir)/auto_explain.so"',
            ],
        )
        available = set(
            self._psql(
                container,
                "select name from pg_catalog.pg_available_extensions "
                "where name = any (array["
                + ",".join(_sql_literal(name) for name in DIAGNOSTIC_EXTENSIONS)
                + "]) order by name",
                database=self.config.postgres.database,
                synchronous_commit_off=True,
            ).splitlines()
        )
        missing = sorted(set(DIAGNOSTIC_EXTENSIONS) - available)
        if missing:
            raise DockerRuntimeError(
                f"image {self.config.postgres.image_name} lacks diagnostic extensions: "
                + ", ".join(missing)
            )
        for database in APPLICATION_DATABASES:
            for extension in DIAGNOSTIC_EXTENSIONS:
                self._psql(
                    container,
                    f"create extension if not exists {_sql_identifier(extension)}",
                    database=database,
                    synchronous_commit_off=True,
                )
        preload_value = self._psql(
            container,
            "show shared_preload_libraries",
            database=self.config.postgres.database,
        )
        actual_preloads = [item.strip() for item in preload_value.split(",") if item.strip()]
        if actual_preloads != list(DIAGNOSTIC_PRELOADS):
            raise DockerRuntimeError(
                "unexpected shared_preload_libraries order: " + ",".join(actual_preloads)
            )

    def _ensure_superuser_password(self, container: Any) -> None:
        self._psql(
            container,
            f"alter role {_sql_identifier(self.config.postgres.superuser)} "
            f"with password {_sql_literal(self._superuser_password())}",
            synchronous_commit_off=True,
        )

    def _ensure_application_databases(self, container: Any) -> None:
        credentials = self._database_accounts()
        for account in credentials.applications:
            role = _sql_identifier(account.owner)
            role_name = _sql_literal(account.owner)
            password = _sql_literal(account.password)
            self._psql(
                container,
                f"""
DO $pg_stand$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = {role_name}) THEN
    ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
      PASSWORD {password};
  ELSE
    CREATE ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
      PASSWORD {password};
  END IF;
END
$pg_stand$;
""",
                synchronous_commit_off=True,
            )
            exists = self._psql(
                container,
                "select count(*) from pg_catalog.pg_database where datname = "
                + _sql_literal(account.database),
                synchronous_commit_off=True,
            ).strip()
            if exists == "0":
                self._psql(
                    container,
                    f"create database {_sql_identifier(account.database)} owner {role}",
                    synchronous_commit_off=True,
                )
            else:
                self._psql(
                    container,
                    f"alter database {_sql_identifier(account.database)} owner to {role}",
                    synchronous_commit_off=True,
                )

    def up(self, *, timeout_seconds: float = 180.0) -> dict[str, Any]:
        self._ping()
        self._assert_up_configuration_is_applied()
        return self._activate(timeout_seconds=timeout_seconds)

    def _activate(self, *, timeout_seconds: float) -> dict[str, Any]:
        acquired_lock = self._acquire_active_lock()
        try:
            result = self._up_locked(timeout_seconds=timeout_seconds)
            self._record_applied_state()
            return result
        except Exception:
            if acquired_lock and not self._has_running_owned_nodes():
                self._release_active_lock()
            raise

    def _up_locked(self, *, timeout_seconds: float) -> dict[str, Any]:
        credential_state = credentials_status(self.config.project_directory)
        credentials_ready = all(
            credential_state[name]["state"] == "ready" for name in ("ssh", "tls", "database")
        )
        if not credentials_ready and self._has_running_owned_nodes():
            raise DockerRuntimeError(
                "credentials are missing or incomplete while stand containers are running; "
                "restore .pg_stand/credentials or recreate the stand with cleared data"
            )
        replication = self.config.topology.replication
        ensure_credentials(
            self.config.project_directory,
            superuser=self.config.postgres.superuser,
            replication_username=(
                replication.username if replication is not None else DEFAULT_REPLICATION_USER
            ),
            output=self.output,
        )
        self._ensure_image()
        self._prepare_storage()
        network = self._ensure_network()
        primary = self._ensure_database_container(
            node_name="primary",
            node=self.config.primary,
            storage=self._node_storage("primary"),
            network=network,
            command=primary_postgres_command(self.config),
        )
        self._wait_for_postgres(primary, timeout_seconds)
        self._ensure_superuser_password(primary)
        self._ensure_application_databases(primary)
        if self._ensure_node_parameters(primary, "primary", timeout_seconds):
            self.output("APPLY node=primary postgres_parameters=restart")
        self._ensure_diagnostic_extensions(primary)
        self._wait_for_pgbouncers(primary, timeout_seconds)
        result: dict[str, Any] = {
            "project": self.config.metadata.name,
            "postgres_version": self.config.postgres.version,
            "image": self.config.postgres.image_name,
            "topology": self.config.topology.mode,
            "databases": [
                {"database": database, "owner": owner} for database, owner in APPLICATION_ACCOUNTS
            ],
            "primary": asdict(self.config.primary),
        }
        if not self.config.is_replicated:
            primary_port = self.config.primary.published_port
            self.output(f"READY project={self.config.metadata.name} primary_port={primary_port}")
            return result

        self._configure_replication_access(primary)
        standby = self.config.standby
        if standby is None:
            raise DockerRuntimeError("physical standby configuration is incomplete")
        standby_storage = self._node_storage("standby")
        self._prepare_standby_data(primary, standby_storage, network)
        standby_container = self._ensure_database_container(
            node_name="standby",
            node=standby,
            storage=standby_storage,
            network=network,
            command=standby_postgres_command(self.config),
        )
        self._wait_for_postgres(standby_container, timeout_seconds)
        self._ensure_node_parameters(standby_container, "standby", timeout_seconds)
        self._wait_for_recovery(standby_container, timeout_seconds)
        self._wait_for_pgbouncers(standby_container, timeout_seconds)
        replication_state = self._apply_replication_mode(primary, timeout_seconds)
        result["standby"] = asdict(standby)
        result["replication"] = replication_state
        if self.config.has_logical_replica:
            logical = self.config.logical
            if logical is None:
                raise DockerRuntimeError("logical replica configuration is incomplete")
            logical_container = self._ensure_database_container(
                node_name="logical",
                node=logical,
                storage=self._node_storage("logical"),
                network=network,
                command=logical_postgres_command(self.config),
            )
            self._wait_for_postgres(logical_container, timeout_seconds)
            self._ensure_superuser_password(logical_container)
            self._ensure_application_databases(logical_container)
            self._ensure_node_parameters(logical_container, "logical", timeout_seconds)
            self._ensure_diagnostic_extensions(logical_container)
            self._wait_for_pgbouncers(logical_container, timeout_seconds)
            logical_state = self._configure_logical_replication(
                primary,
                logical_container,
                timeout_seconds,
            )
            result["logical"] = asdict(logical)
            result["logical_replication"] = logical_state
        replication = self.config.topology.replication
        if replication is None:
            raise DockerRuntimeError("replication configuration is incomplete")
        replication_mode = replication.mode
        primary_port = self.config.primary.published_port
        self.output(
            f"READY project={self.config.metadata.name} mode={replication_mode} "
            f"primary_port={primary_port} standby_port={standby.published_port}"
            + (
                f" logical_port={self.config.logical.published_port}"
                if self.config.logical is not None
                else ""
            )
        )
        return result

    def _owned_container(
        self,
        node: NodeConfig,
        node_name: str,
        *,
        check_hash: bool = False,
    ) -> Any | None:
        container = self._find_owned_container(node.container_name)
        if container is None:
            return None
        self._assert_resource_labels(
            container,
            f"{node_name} container",
            check_hash=check_hash,
            expected_name=node.container_name,
            expected_node=node_name,
            expected_kind="stand-node",
        )
        return container

    def _assert_current_stand_is_active(self) -> None:
        lock = self._find_active_lock()
        if lock is None:
            raise DockerRuntimeError("no active pg-stand stand; run up first")
        labels = lock.attrs.get("Labels") or {}
        if labels.get(PROJECT_LABEL) != self.config.metadata.name:
            raise DockerRuntimeError(
                f"stand {labels.get(PROJECT_LABEL) or '<unknown>'} is active instead of "
                f"{self.config.metadata.name}"
            )
        self._assert_resource_labels(
            lock,
            "active stand lock",
            check_hash=False,
            expected_name=ACTIVE_LOCK_NETWORK,
            expected_kind="active-lock",
        )

    def _parameter_contexts(self, container: Any, names: set[str]) -> dict[str, str]:
        if not names:
            return {}
        values = ",".join(_sql_literal(name) for name in sorted(names))
        output = self._psql(
            container,
            "select name || '|' || context from pg_catalog.pg_settings "
            f"where name in ({values}) order by name",
        )
        contexts = dict(line.split("|", 1) for line in output.splitlines() if "|" in line)
        missing = sorted(names - contexts.keys())
        if missing:
            raise DockerRuntimeError(
                "PostgreSQL parameters are unavailable on this major: " + ", ".join(missing)
            )
        return contexts

    def apply(
        self,
        *,
        mode: str,
        timeout_seconds: float = 180.0,
        stop_timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        self._ping()
        state = self._read_applied_state()
        if state is None:
            raise DockerRuntimeError("no applied configuration state; run up first")
        if state.get("identity_signature") != self._identity_signature():
            raise DockerRuntimeError(
                "stand identity fields changed; use the old configuration to run down before "
                "deploying the new identity"
            )
        if mode == "restart":
            self.down(clear_data=False, timeout_seconds=stop_timeout_seconds)
            result = self._activate(timeout_seconds=timeout_seconds)
            return {"mode": mode, "project": self.config.metadata.name, "stand": result}
        if mode != "reload":
            raise DockerRuntimeError(f"unknown apply mode: {mode}")
        if state.get("non_parameter_signature") != self._configuration_signature(
            include_parameters=False
        ):
            raise DockerRuntimeError(
                "the configuration includes container, topology, image, or resource changes; "
                "use apply --restart"
            )
        self._assert_current_stand_is_active()
        previous_parameters = state.get("parameters") or {}
        if not isinstance(previous_parameters, dict):
            raise DockerRuntimeError("applied parameter state is invalid; use apply --restart")
        changed_names = {
            name
            for name in set(previous_parameters) | set(self.config.postgres.parameters)
            if previous_parameters.get(name) != self.config.postgres.parameters.get(name)
        }
        primary = self._owned_container(self.config.primary, "primary")
        if primary is None:
            raise DockerRuntimeError("primary container does not exist; run up first")
        contexts = self._parameter_contexts(primary, changed_names)
        restart_parameters = sorted(
            name for name, context in contexts.items() if context == "postmaster"
        )
        internal_parameters = sorted(
            name for name, context in contexts.items() if context == "internal"
        )
        if internal_parameters:
            raise DockerRuntimeError(
                "read-only PostgreSQL parameters cannot be applied: "
                + ", ".join(internal_parameters)
            )
        if restart_parameters:
            raise DockerRuntimeError(
                "PostgreSQL parameters require restart: " + ", ".join(restart_parameters)
            )
        applied_nodes = []
        for name, node in self._configured_nodes():
            container = self._owned_container(node, name)
            if container is None:
                raise DockerRuntimeError(f"{name} container does not exist; run up first")
            container.reload()
            if container.status != "running":
                raise DockerRuntimeError(f"{name} container is not running")
            file_changed = self._sync_parameter_file(name)
            reloaded = self._psql(container, "select pg_catalog.pg_reload_conf()") == "t"
            applied_nodes.append({"node": name, "file_changed": file_changed, "reloaded": reloaded})
        self._record_applied_state()
        return {
            "mode": mode,
            "project": self.config.metadata.name,
            "changed_parameters": sorted(changed_names),
            "nodes": applied_nodes,
        }

    def stop(self, *, timeout_seconds: int = 30) -> None:
        self._ping()
        for node_name, node in self._configured_nodes():
            container = self._owned_container(node, node_name, check_hash=False)
            if container is None:
                continue
            container.reload()
            if container.status not in {"created", "exited", "dead"}:
                self.output(f"STOP node={node_name} container={node.container_name}")
                container.stop(timeout=timeout_seconds)
        self._release_active_lock()

    def down(
        self,
        *,
        clear_data: bool = False,
        timeout_seconds: int = 30,
        release_lock: bool = True,
    ) -> None:
        self._ping()
        if clear_data:
            self._acquire_active_lock()
        helper_name = f"{self.config.metadata.name}-standby-bootstrap{MANAGED_RESOURCE_SUFFIX}"
        self._remove_stale_helper(helper_name, check_hash=False)
        nodes = list(reversed(self._configured_nodes()))
        for node_name, node in nodes:
            container = self._owned_container(node, node_name, check_hash=False)
            if container is None:
                continue
            container.reload()
            if container.status not in {"created", "exited", "dead"}:
                self.output(f"STOP node={node_name} container={node.container_name}")
                container.stop(timeout=timeout_seconds)
            self.output(f"REMOVE node={node_name} container={node.container_name}")
            container.remove(force=False, v=True)
        network = self._find_owned_network(self.config.docker.network_name)
        if network is not None:
            self._assert_resource_labels(
                network,
                "network",
                check_hash=False,
                expected_name=self.config.docker.network_name,
                expected_kind="network",
            )
            self.output(f"REMOVE network={network.name}")
            network.remove()
        if clear_data:
            self.storage_clean(require_absent_containers=True)
        if release_lock:
            self._release_active_lock()

    def recreate(
        self,
        *,
        clear_data: bool = False,
        timeout_seconds: float = 180.0,
        stop_timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        self.down(clear_data=clear_data, timeout_seconds=stop_timeout_seconds)
        return self.up(timeout_seconds=timeout_seconds)


class StandManager(
    DockerResourceMixin,
    ReplicationMixin,
    ObservationMixin,
    NodeOperationsMixin,
    StateStorageMixin,
    _StandManagerCore,
):
    """Public stand manager composed from focused runtime operation mixins."""
