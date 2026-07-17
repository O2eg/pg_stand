"""Stand status, resources, health, capabilities, and event observation."""

from __future__ import annotations

import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from pg_stand.config import NodeConfig
from pg_stand.database_credentials import APPLICATION_ACCOUNTS, APPLICATION_DATABASES
from pg_stand.errors import DockerRuntimeError
from pg_stand.runtime_common import (
    CONFIG_HASH_LABEL,
    DIAGNOSTIC_EXTENSIONS,
    DIAGNOSTIC_PRELOADS,
    INSTANCE_LABEL,
    MANAGED_LABEL,
    MANAGED_RESOURCE_SUFFIX,
    NODE_LABEL,
    PGBOUNCER_SESSION_PORT,
    PGBOUNCER_TRANSACTION_PORT,
    PGBOUNCER_VERSION,
    PGLOG,
    PROJECT_LABEL,
    TLS_CA_FILE,
    TLS_POSTGRES_CERT_FILE,
    TLS_POSTGRES_KEY_FILE,
    _resource_labels,
    _sql_literal,
)


class ObservationMixin:
    def _node_pgbouncer_health_checks(self, container: Any, node_name: str) -> list[dict[str, Any]]:
        checks = []
        expected = f"{self.config.postgres.database}|{self.config.postgres.superuser}"
        for mode, port in (
            ("session", PGBOUNCER_SESSION_PORT),
            ("transaction", PGBOUNCER_TRANSACTION_PORT),
        ):
            result = self._pgbouncer_probe(container, port)
            output = (result.output or b"").decode("utf-8", errors="replace").strip()
            plaintext_rejected = None
            if self.config.postgres.tls.enabled:
                plaintext_rejected = (
                    self._pgbouncer_probe(container, port, plaintext=True).exit_code != 0
                )
            okay = result.exit_code == 0 and output == expected and plaintext_rejected is not False
            checks.append(
                {
                    "id": f"node.{node_name}.pgbouncer.{mode}",
                    "status": "healthy" if okay else "unavailable",
                    "summary": (
                        f"{mode} pooling accepts database queries"
                        if okay
                        else f"{mode} pooling query or TLS policy failed"
                    ),
                    "container_port": port,
                    "query_result": output,
                    "plaintext_rejected": plaintext_rejected,
                }
            )
        return checks

    def _node_tls_health_check(self, container: Any, node_name: str) -> dict[str, Any]:
        expected = self.config.postgres.tls.enabled
        actual = self._psql(container, "show ssl").strip() == "on"
        if not expected:
            return {
                "id": f"node.{node_name}.tls",
                "status": "healthy" if not actual else "degraded",
                "summary": "TLS is disabled" if not actual else "TLS is unexpectedly enabled",
                "expected": False,
                "actual": actual,
            }
        tls_result = container.exec_run(
            [
                "psql",
                "--host",
                "127.0.0.1",
                "--username",
                self.config.postgres.superuser,
                "--dbname",
                self.config.postgres.database,
                "--tuples-only",
                "--no-align",
                "--command",
                "select ssl::text from pg_catalog.pg_stat_ssl "
                "where pid = pg_catalog.pg_backend_pid()",
            ],
            environment={
                "PGSSLMODE": "verify-ca",
                "PGSSLROOTCERT": TLS_CA_FILE,
                "PGSSLCERT": TLS_POSTGRES_CERT_FILE,
                "PGSSLKEY": TLS_POSTGRES_KEY_FILE,
            },
        )
        tls_output = (tls_result.output or b"").decode("utf-8", errors="replace").strip()
        plain_result = container.exec_run(
            [
                "psql",
                "--host",
                "127.0.0.1",
                "--username",
                self.config.postgres.superuser,
                "--dbname",
                self.config.postgres.database,
                "--command",
                "select 1",
            ],
            environment={
                "PGSSLMODE": "disable",
                "PGPASSWORD": self._superuser_password(),
            },
        )
        certificate_ok = tls_result.exit_code == 0 and tls_output in {"t", "true"}
        plaintext_rejected = plain_result.exit_code != 0
        okay = actual and certificate_ok and plaintext_rejected
        return {
            "id": f"node.{node_name}.tls",
            "status": "healthy" if okay else "unavailable",
            "summary": (
                "certificate-authenticated TLS works and plaintext TCP is rejected"
                if okay
                else "TLS policy or certificate-authenticated TCP probe failed"
            ),
            "expected": True,
            "actual": actual,
            "certificate_probe": certificate_ok,
            "plaintext_rejected": plaintext_rejected,
        }

    @staticmethod
    def _container_usage(container: Any) -> dict[str, Any]:
        stats = container.stats(stream=False)
        memory = stats.get("memory_stats") or {}
        memory_details = memory.get("stats") or {}
        memory_cache = int(memory_details.get("inactive_file", memory_details.get("cache", 0)) or 0)
        memory_usage = int(memory.get("usage", 0) or 0)
        cpu = stats.get("cpu_stats") or {}
        previous_cpu = stats.get("precpu_stats") or {}
        cpu_usage = cpu.get("cpu_usage") or {}
        previous_usage = previous_cpu.get("cpu_usage") or {}
        cpu_delta = int(cpu_usage.get("total_usage", 0) or 0) - int(
            previous_usage.get("total_usage", 0) or 0
        )
        system_delta = int(cpu.get("system_cpu_usage", 0) or 0) - int(
            previous_cpu.get("system_cpu_usage", 0) or 0
        )
        online_cpus = int(cpu.get("online_cpus", 0) or len(cpu_usage.get("percpu_usage") or []))
        cpu_percent = 0.0
        if cpu_delta > 0 and system_delta > 0 and online_cpus > 0:
            cpu_percent = cpu_delta / system_delta * online_cpus * 100.0
        return {
            "cpu_percent": round(cpu_percent, 3),
            "memory_usage_bytes": max(0, memory_usage - memory_cache),
            "memory_limit_bytes": int(memory.get("limit", 0) or 0),
            "pids": int((stats.get("pids_stats") or {}).get("current", 0) or 0),
        }

    def _container_runtime_details(
        self,
        container: Any,
        node: NodeConfig,
        *,
        include_usage: bool,
    ) -> dict[str, Any]:
        container.reload()
        attrs = container.attrs
        host_config = attrs.get("HostConfig") or {}
        config = attrs.get("Config") or {}
        network_settings = attrs.get("NetworkSettings") or {}
        networks = {}
        for name, settings in (network_settings.get("Networks") or {}).items():
            networks[name] = {
                "ip_address": settings.get("IPAddress") or None,
                "global_ipv6_address": settings.get("GlobalIPv6Address") or None,
                "gateway": settings.get("Gateway") or None,
                "mac_address": settings.get("MacAddress") or None,
            }
        ports = {}
        for container_port, bindings in (network_settings.get("Ports") or {}).items():
            ports[container_port] = [
                {
                    "host_ip": binding.get("HostIp"),
                    "host_port": int(binding["HostPort"]),
                }
                for binding in (bindings or [])
            ]
        result: dict[str, Any] = {
            "container": container.name,
            "id": container.short_id,
            "status": container.status,
            "hostname": config.get("Hostname") or None,
            "image": config.get("Image") or self.config.postgres.image_name,
            "networks": networks,
            "ports": ports,
            "desired": {
                "cpu_limit": node.cpu_limit,
                "memory_limit": node.memory_limit,
                "shm_size": node.shm_size,
                "postgresql_address": f"{node.bind_address}:{node.published_port}",
                "ssh_address": f"{node.bind_address}:{node.ssh_published_port}",
                "pgbouncer_session_address": (
                    f"{node.bind_address}:{node.pgbouncer_session_published_port}"
                ),
                "pgbouncer_transaction_address": (
                    f"{node.bind_address}:{node.pgbouncer_transaction_published_port}"
                ),
            },
            "actual_limits": {
                "cpu_limit": int(host_config.get("NanoCpus", 0) or 0) / 1_000_000_000,
                "memory_bytes": int(host_config.get("Memory", 0) or 0),
                "shm_size_bytes": int(host_config.get("ShmSize", 0) or 0),
            },
            "creation_config_hash": (config.get("Labels") or {}).get(CONFIG_HASH_LABEL),
        }
        if include_usage and container.status == "running":
            result["usage"] = self._container_usage(container)
        return result

    def resources(self, node_name: str | None = None) -> dict[str, Any]:
        self._ping()
        selected = self._configured_nodes()
        if node_name is not None:
            selected = [(name, node) for name, node in selected if name == node_name]
            if not selected:
                raise DockerRuntimeError(f"node {node_name} is not configured")
        result: dict[str, Any] = {
            "project": self.config.metadata.name,
            "active": False,
            "nodes": {},
        }
        lock = self._find_active_lock()
        if lock is not None:
            labels = lock.attrs.get("Labels") or {}
            result["active"] = (
                labels.get(PROJECT_LABEL) == self.config.metadata.name
                and labels.get(INSTANCE_LABEL) == self.config.instance_id
            )
            result["active_project"] = labels.get(PROJECT_LABEL)
        for name, node in selected:
            container = self._owned_container(node, name)
            result["nodes"][name] = (
                self._container_runtime_details(container, node, include_usage=True)
                if container is not None
                else {
                    "container": node.container_name,
                    "status": "absent",
                    "desired": asdict(node),
                }
            )
        return result

    def status(self) -> dict[str, Any]:
        self._ping()
        applied_state = self._read_applied_state()
        active_lock = self._find_active_lock()
        active_labels = active_lock.attrs.get("Labels") if active_lock is not None else {}
        result: dict[str, Any] = {
            "project": self.config.metadata.name,
            "active": (
                (active_labels or {}).get(PROJECT_LABEL) == self.config.metadata.name
                and (active_labels or {}).get(INSTANCE_LABEL) == self.config.instance_id
            ),
            "active_project": (active_labels or {}).get(PROJECT_LABEL),
            "topology": self.config.topology.mode,
            "postgres_version": self.config.postgres.version,
            "image": self.config.postgres.image_name,
            "storage_root": str(self._storage_root()),
            "databases": [
                {"database": database, "owner": owner} for database, owner in APPLICATION_ACCOUNTS
            ],
            "nodes": {},
            "replication": [],
            "configuration": {
                "desired_hash": self.config.config_hash,
                "applied_hash": (
                    applied_state.get("config_hash") if applied_state is not None else None
                ),
                "drift": applied_state is not None
                and applied_state.get("config_hash") != self.config.config_hash,
            },
        }
        for node_name, node in self._configured_nodes():
            entry: dict[str, Any]
            container = self._owned_container(node, node_name)
            if container is None:
                entry = {
                    "container": node.container_name,
                    "status": "absent",
                    "desired": asdict(node),
                }
            else:
                entry = self._container_runtime_details(
                    container,
                    node,
                    include_usage=False,
                )
                if container.status == "running":
                    recovery = self._psql(
                        container,
                        "select pg_catalog.pg_is_in_recovery()",
                    )
                    entry["in_recovery"] = recovery.strip() == "t"
            result["nodes"][node_name] = entry
        primary_entry = result["nodes"].get("primary") or {}
        if primary_entry.get("status") == "running" and self.config.is_replicated:
            primary = self._owned_container(self.config.primary, "primary")
            if primary is None:
                raise DockerRuntimeError("primary container disappeared during status collection")
            result["replication"] = self._replication_state(primary)
        logical_entry = result["nodes"].get("logical") or {}
        if logical_entry.get("status") == "running" and self.config.has_logical_replica:
            logical_config = self.config.logical
            if logical_config is None:
                raise DockerRuntimeError("logical replica configuration is incomplete")
            logical = self._owned_container(logical_config, "logical")
            if logical is None:
                raise DockerRuntimeError("logical container disappeared during status collection")
            result["logical_replication"] = self._logical_replication_state(logical)
        return result

    @staticmethod
    def _health_level(checks: list[dict[str, Any]]) -> str:
        states = {item["status"] for item in checks}
        if "unavailable" in states:
            return "unavailable"
        if "degraded" in states:
            return "degraded"
        return "healthy"

    def _node_health_checks(
        self,
        node_name: str,
        node: NodeConfig,
        *,
        min_free_bytes: int,
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        container = self._owned_container(node, node_name)
        if container is None:
            return [
                {
                    "id": f"node.{node_name}.container",
                    "status": "unavailable",
                    "summary": f"{node_name} container is absent",
                }
            ]
        container.reload()
        checks.append(
            {
                "id": f"node.{node_name}.container",
                "status": "healthy" if container.status == "running" else "unavailable",
                "summary": f"container state is {container.status}",
            }
        )
        if container.status != "running":
            return checks
        ready = container.exec_run(
            [
                "pg_isready",
                "--username",
                self.config.postgres.superuser,
                "--dbname",
                self.config.postgres.database,
            ]
        )
        ready_output = (ready.output or b"").decode("utf-8", errors="replace").strip()
        checks.append(
            {
                "id": f"node.{node_name}.postgres",
                "status": "healthy" if ready.exit_code == 0 else "unavailable",
                "summary": ready_output or "pg_isready returned no output",
            }
        )
        if ready.exit_code != 0:
            return checks
        try:
            checks.append(self._node_tls_health_check(container, node_name))
            checks.extend(self._node_pgbouncer_health_checks(container, node_name))
            recovery = self._psql(container, "select pg_catalog.pg_is_in_recovery()").strip()
            expected_recovery = node_name == "standby"
            actual_recovery = recovery == "t"
            checks.append(
                {
                    "id": f"node.{node_name}.role",
                    "status": "healthy" if actual_recovery == expected_recovery else "unavailable",
                    "summary": ("recovery standby" if actual_recovery else "writable primary"),
                    "expected": "recovery standby" if expected_recovery else "writable primary",
                }
            )
            inventory_output = self._psql(
                container,
                "select d.datname || '|' || r.rolname from pg_catalog.pg_database d "
                "join pg_catalog.pg_roles r on r.oid = d.datdba "
                "where d.datname ~ '^app_[0-9][0-9]$' order by d.datname",
            )
            expected_inventory = {f"{database}|{owner}" for database, owner in APPLICATION_ACCOUNTS}
            actual_inventory = set(inventory_output.splitlines())
            inventory_okay = actual_inventory == expected_inventory
            checks.append(
                {
                    "id": f"node.{node_name}.databases",
                    "status": "healthy" if inventory_okay else "degraded",
                    "summary": "all application databases and owners are present"
                    if inventory_okay
                    else "application database or owner inventory differs",
                    "expected": sorted(expected_inventory),
                    "actual": sorted(actual_inventory),
                }
            )
            missing_by_database: dict[str, list[str]] = {}
            for database in APPLICATION_DATABASES:
                extension_output = self._psql(
                    container,
                    "select extname from pg_catalog.pg_extension where extname = any (array["
                    + ",".join(_sql_literal(name) for name in DIAGNOSTIC_EXTENSIONS)
                    + "]) order by extname",
                    database=database,
                )
                installed = set(extension_output.splitlines())
                missing = sorted(set(DIAGNOSTIC_EXTENSIONS) - installed)
                if missing:
                    missing_by_database[database] = missing
            checks.append(
                {
                    "id": f"node.{node_name}.extensions",
                    "status": "healthy" if not missing_by_database else "degraded",
                    "summary": "all diagnostic extensions are installed in all 10 databases"
                    if not missing_by_database
                    else "diagnostic extensions are missing from application databases",
                    "missing_by_database": missing_by_database,
                }
            )
            preload_output = self._psql(
                container,
                "show shared_preload_libraries",
                database=self.config.postgres.database,
            )
            preloads = [item.strip() for item in preload_output.split(",") if item.strip()]
            checks.append(
                {
                    "id": f"node.{node_name}.preloads",
                    "status": "healthy" if preloads == list(DIAGNOSTIC_PRELOADS) else "degraded",
                    "summary": "diagnostic preload list is correct"
                    if preloads == list(DIAGNOSTIC_PRELOADS)
                    else "diagnostic preload list differs from the managed profile",
                    "actual": preloads,
                }
            )
        except DockerRuntimeError as exc:
            checks.append(
                {
                    "id": f"node.{node_name}.sql",
                    "status": "unavailable",
                    "summary": str(exc),
                }
            )
        storage_output = self._exec(
            container,
            [
                "/bin/sh",
                "-ceu",
                'test -w "$PGLOG"; df -PB1 "$PGLOG" | awk \'NR == 2 {print $4}\'',
            ],
            environment={"PGLOG": PGLOG},
            check=False,
        )
        try:
            free_bytes = int(storage_output.splitlines()[-1])
        except (IndexError, ValueError):
            free_bytes = -1
        checks.append(
            {
                "id": f"node.{node_name}.storage",
                "status": "healthy"
                if free_bytes >= min_free_bytes
                else ("degraded" if free_bytes >= 0 else "unavailable"),
                "summary": f"{free_bytes} bytes free"
                if free_bytes >= 0
                else "log storage is not writable",
                "free_bytes": free_bytes if free_bytes >= 0 else None,
                "minimum_free_bytes": min_free_bytes,
            }
        )
        return checks

    def health(
        self,
        *,
        max_physical_lag_bytes: int = 104_857_600,
        max_physical_lag_seconds: float = 30.0,
        max_logical_lag_seconds: float = 30.0,
        min_free_bytes: int = 268_435_456,
    ) -> dict[str, Any]:
        self._ping()
        checks: list[dict[str, Any]] = []
        lock = self._find_active_lock()
        lock_labels = _resource_labels(lock) if lock is not None else {}
        lock_project = lock_labels.get(PROJECT_LABEL)
        lock_owned = (
            lock_project == self.config.metadata.name
            and lock_labels.get(INSTANCE_LABEL) == self.config.instance_id
        )
        checks.append(
            {
                "id": "stand.active_lock",
                "status": "healthy" if lock_owned else "unavailable",
                "summary": (
                    "this stand owns the active lease"
                    if lock_owned
                    else f"active lease belongs to {lock_project or 'no project'}"
                ),
            }
        )
        state = self._read_applied_state()
        drift = state is not None and state.get("config_hash") != self.config.config_hash
        checks.append(
            {
                "id": "stand.configuration",
                "status": "degraded" if drift or state is None else "healthy",
                "summary": "configuration drift detected"
                if drift
                else (
                    "no applied configuration state" if state is None else "configuration matches"
                ),
            }
        )
        for node_name, node in self._configured_nodes():
            checks.extend(
                self._node_health_checks(
                    node_name,
                    node,
                    min_free_bytes=min_free_bytes,
                )
            )

        try:
            replication = self.replication_status() if self.config.is_replicated else None
            if replication is not None:
                expected = self.config.topology.replication
                if expected is None:
                    raise DockerRuntimeError("replication configuration is incomplete")
                physical = next(
                    (
                        row
                        for row in replication["physical"]
                        if row["application_name"] == expected.application_name
                    ),
                    None,
                )
                if physical is None:
                    checks.append(
                        {
                            "id": "replication.physical",
                            "status": "unavailable",
                            "summary": "configured physical standby is not connected",
                        }
                    )
                else:
                    expected_sync = "sync" if expected.mode == "sync" else "async"
                    lag_bytes = physical["replay_lag_bytes"]
                    lag_ms = physical["replay_lag_ms"]
                    okay = (
                        physical["state"] == "streaming"
                        and physical["sync_state"] == expected_sync
                        and lag_bytes <= max_physical_lag_bytes
                        and lag_ms <= max_physical_lag_seconds * 1000
                    )
                    checks.append(
                        {
                            "id": "replication.physical",
                            "status": "healthy" if okay else "degraded",
                            "summary": (
                                f"state={physical['state']} sync_state={physical['sync_state']} "
                                f"lag_bytes={lag_bytes} replay_lag_ms={lag_ms}"
                            ),
                            "details": physical,
                        }
                    )
                if self.config.has_logical_replica:
                    logical = replication.get("logical") or {}
                    workers = logical.get("workers") or []
                    pending = int(logical.get("pending_tables", 0) or 0)
                    max_age = max(
                        (int(item.get("message_age_seconds", 0)) for item in workers),
                        default=0,
                    )
                    logical_okay = (
                        bool(workers) and pending == 0 and max_age <= max_logical_lag_seconds
                    )
                    checks.append(
                        {
                            "id": "replication.logical",
                            "status": "healthy" if logical_okay else "degraded",
                            "summary": (
                                f"workers={len(workers)} pending_tables={pending} "
                                f"message_age_seconds={max_age}"
                            ),
                            "details": logical,
                        }
                    )
        except DockerRuntimeError as exc:
            checks.append(
                {
                    "id": "replication.query",
                    "status": "unavailable",
                    "summary": str(exc),
                }
            )
        level = self._health_level(checks)
        return {
            "project": self.config.metadata.name,
            "status": level,
            "exit_code": {"healthy": 0, "degraded": 1, "unavailable": 2}[level],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "checks": checks,
        }

    def wait_until_healthy(
        self,
        *,
        timeout_seconds: float,
        interval_seconds: float = 1.0,
        **health_options: Any,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last: dict[str, Any] = {}
        while True:
            last = self.health(**health_options)
            if last["status"] == "healthy":
                last["timed_out"] = False
                return last
            if time.monotonic() >= deadline:
                last["timed_out"] = True
                last["exit_code"] = 2
                return last
            time.sleep(max(0.1, interval_seconds))

    def capabilities(self) -> dict[str, Any]:
        self._ping()
        version = self.config.postgres.version
        result: dict[str, Any] = {
            "project": self.config.metadata.name,
            "postgres_version": version,
            "pgbouncer_version": PGBOUNCER_VERSION,
            "image": self.config.postgres.image_name,
            "features": {
                "physical_replication": self.config.is_replicated,
                "logical_replication": self.config.has_logical_replica,
                "planning_statistics": version >= 13,
                "auto_explain_wal": version >= 13,
                "compute_query_id": version >= 14,
                "track_wal_io_timing": version >= 14,
                "modern_pgdata_layout": version >= 18,
                "certificate_tls": self.config.postgres.tls.enabled,
                "pgbouncer_session_pooling": True,
                "pgbouncer_transaction_pooling": True,
            },
            "required_extensions": list(DIAGNOSTIC_EXTENSIONS),
            "required_preloads": list(DIAGNOSTIC_PRELOADS),
            "databases": [
                {"database": database, "owner": owner} for database, owner in APPLICATION_ACCOUNTS
            ],
            "nodes": {},
        }
        for node_name, node in self._configured_nodes():
            container = self._owned_container(node, node_name)
            entry: dict[str, Any] = {"running": False}
            if container is not None:
                container.reload()
                entry["running"] = container.status == "running"
                if container.status == "running":
                    entry["pgbouncer_version"] = self._exec(container, ["pgbouncer", "--version"])
                    entry["server_version"] = self._psql(container, "show server_version")
                    entry["installed_extensions"] = self._psql(
                        container,
                        "select extname from pg_catalog.pg_extension order by extname",
                        database=self.config.postgres.database,
                    ).splitlines()
                    entry["shared_preload_libraries"] = self._psql(
                        container,
                        "show shared_preload_libraries",
                        database=self.config.postgres.database,
                    ).split(",")
            result["nodes"][node_name] = entry
        return result

    def docker_events(
        self,
        *,
        since_epoch: int,
        until_epoch: int | None,
        count: int,
    ) -> Any:
        self._ping()
        events = self.client.events(
            decode=True,
            since=since_epoch,
            until=until_epoch,
            filters={
                "label": [
                    f"{MANAGED_LABEL}=true",
                    f"{PROJECT_LABEL}={self.config.metadata.name}",
                    f"{INSTANCE_LABEL}={self.config.instance_id}",
                ],
                "type": ["container", "network"],
            },
        )
        emitted = 0
        for event in events:
            actor = event.get("Actor") or {}
            attributes = actor.get("Attributes") or {}
            if attributes.get(MANAGED_LABEL) != "true":
                continue
            if attributes.get(INSTANCE_LABEL) != self.config.instance_id:
                continue
            name = attributes.get("name") or attributes.get("container") or ""
            if name and not name.endswith(MANAGED_RESOURCE_SUFFIX):
                continue
            yield {
                "time": datetime.fromtimestamp(int(event.get("time", 0)), timezone.utc).isoformat(),
                "type": event.get("Type"),
                "action": event.get("Action") or event.get("status"),
                "id": event.get("id") or actor.get("ID"),
                "project": attributes.get(PROJECT_LABEL),
                "node": attributes.get(NODE_LABEL),
                "name": name or None,
            }
            emitted += 1
            if count > 0 and emitted >= count:
                break

    def overview(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "status": self.status(),
            "resources": self.resources(),
        }
        if self.config.is_replicated:
            result["replication"] = self.replication_status()
        return result
