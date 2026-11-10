"""Per-node operations, host connections, SQL, and PostgreSQL CSV logs."""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode

from pg_stand.config import NodeConfig
from pg_stand.credentials import credential_paths
from pg_stand.errors import DockerRuntimeError
from pg_stand.locking import serialized
from pg_stand.runtime_common import (
    PGLOG,
    postgres_data_paths,
    standby_postgres_command,
)


class NodeOperationsMixin:
    def _node_by_name(self, node_name: str) -> NodeConfig | None:
        if node_name == "primary":
            return self.config.primary
        if node_name == "standby":
            return self.config.standby
        if node_name == "logical":
            return self.config.logical
        return None

    @staticmethod
    def _host_connection_address(node: NodeConfig) -> str:
        if node.bind_address == "0.0.0.0":
            return "127.0.0.1"
        if node.bind_address == "::":
            return "::1"
        return node.bind_address

    def node_inspect(self, node_name: str) -> dict[str, Any]:
        self._ping()
        node = self._node_by_name(node_name)
        if node is None:
            raise DockerRuntimeError(f"node {node_name} is not configured")
        container = self._owned_container(node, node_name)
        if container is None:
            return {
                "project": self.config.metadata.name,
                "node": node_name,
                "container": node.container_name,
                "status": "absent",
                "desired": asdict(node),
            }
        details = self._container_runtime_details(container, node, include_usage=True)
        attrs = container.attrs
        details["mounts"] = [
            {
                "type": item.get("Type"),
                "source": item.get("Source"),
                "destination": item.get("Destination"),
                "mode": item.get("Mode"),
                "read_write": item.get("RW"),
            }
            for item in attrs.get("Mounts") or []
        ]
        if container.status == "running":
            output = self._psql(
                container,
                "select current_setting('server_version') || '|' || "
                "pg_catalog.pg_postmaster_start_time()::text || '|' || "
                "pg_catalog.pg_is_in_recovery()::text",
            )
            server_version, started_at, recovery = output.split("|", 2)
            details["postgres"] = {
                "server_version": server_version,
                "started_at": started_at,
                "in_recovery": recovery == "t",
                "database": self.config.postgres.database,
                "extensions": self._psql(
                    container,
                    "select extname from pg_catalog.pg_extension order by extname",
                    database=self.config.postgres.database,
                ).splitlines(),
            }
        return {"project": self.config.metadata.name, "node": node_name, **details}

    @serialized
    def node_restart(
        self,
        node_name: str,
        *,
        timeout_seconds: float = 180.0,
        stop_timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        self._ping()
        self._assert_current_stand_is_active()
        self._assert_up_configuration_is_applied()
        node = self._node_by_name(node_name)
        if node is None:
            raise DockerRuntimeError(f"node {node_name} is not configured")
        container = self._owned_container(node, node_name)
        if container is None:
            raise DockerRuntimeError(f"container {node.container_name} does not exist")
        self.output(f"RESTART node={node_name} container={node.container_name}")
        container.restart(timeout=stop_timeout_seconds)
        self._wait_for_postgres(container, timeout_seconds)
        if node_name == "standby":
            self._wait_for_recovery(container, timeout_seconds)
        replication: dict[str, Any] | None = None
        if self.config.is_replicated and node_name in {"primary", "standby"}:
            primary = self._owned_container(self.config.primary, "primary")
            if primary is None:
                raise DockerRuntimeError("primary container does not exist; run up first")
            if node_name == "primary":
                self._wait_for_postgres(primary, timeout_seconds)
            replication = self._apply_replication_mode(primary, timeout_seconds)
        logical_state: dict[str, Any] | None = None
        if self.config.has_logical_replica and node_name in {"primary", "logical"}:
            logical_config = self.config.logical
            if logical_config is None:
                raise DockerRuntimeError("logical replica configuration is incomplete")
            logical = self._owned_container(logical_config, "logical")
            if logical is None:
                raise DockerRuntimeError("logical container does not exist; run up first")
            logical_state = self._wait_for_logical_replication(logical, timeout_seconds)
        return {
            "project": self.config.metadata.name,
            "node": node_name,
            "status": "running",
            "replication": replication,
            "logical_replication": logical_state,
        }

    @serialized
    def standby_reinit(
        self,
        *,
        timeout_seconds: float = 180.0,
        stop_timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        self._ping()
        self._assert_current_stand_is_active()
        self._assert_up_configuration_is_applied()
        standby_config = self.config.standby
        replication = self.config.topology.replication
        if standby_config is None or replication is None:
            raise DockerRuntimeError("this stand has no physical standby")
        primary = self._owned_container(self.config.primary, "primary")
        standby = self._owned_container(standby_config, "standby")
        if primary is None or standby is None:
            raise DockerRuntimeError("primary and standby containers must exist; run up first")
        primary.reload()
        if primary.status != "running":
            raise DockerRuntimeError("primary container is not running")
        if replication.mode == "sync":
            self._psql(
                primary,
                "alter system set synchronous_standby_names = ''",
                synchronous_commit_off=True,
            )
            self._psql(
                primary,
                "select pg_catalog.pg_reload_conf()",
                synchronous_commit_off=True,
            )
        standby.reload()
        if standby.status not in {"created", "exited", "dead"}:
            self.output(f"STOP node=standby container={standby_config.container_name}")
            standby.stop(timeout=stop_timeout_seconds)
        self.output(f"REMOVE node=standby container={standby_config.container_name}")
        standby.remove(force=False, v=True)
        self._clean_node_storage("standby")
        self._prepare_storage()
        network = self._find_owned_network(self.config.docker.network_name)
        if network is None:
            raise DockerRuntimeError("managed stand network does not exist; run up")
        self._prepare_standby_data(primary, self._node_storage("standby"), network)
        standby = self._ensure_database_container(
            node_name="standby",
            node=standby_config,
            storage=self._node_storage("standby"),
            network=network,
            command=standby_postgres_command(self.config),
        )
        self._wait_for_postgres(standby, timeout_seconds)
        self._ensure_node_parameters(standby, "standby", timeout_seconds)
        self._wait_for_recovery(standby, timeout_seconds)
        state = self._apply_replication_mode(primary, timeout_seconds)
        return {
            "project": self.config.metadata.name,
            "node": "standby",
            "reinitialized": True,
            "replication": state,
        }

    def connection_info(
        self,
        node_name: str,
        *,
        database: str | None = None,
        show_password: bool = False,
    ) -> dict[str, Any]:
        node = self._node_by_name(node_name)
        if node is None:
            raise DockerRuntimeError(f"node {node_name} is not configured")
        tls_enabled = self.config.postgres.tls.enabled
        password = (
            None if tls_enabled else (self._superuser_password() if show_password else "<redacted>")
        )
        user = quote(self.config.postgres.superuser, safe="")
        encoded_password = quote(password or "", safe="")
        dbname = database or self.config.postgres.database
        host = self._host_connection_address(node)
        uri_host = f"[{host}]" if ":" in host else host
        tls_directory = credential_paths(self.config.project_directory).tls
        sslmode = "verify-full" if host in {"127.0.0.1", "localhost", "::1"} else "verify-ca"
        tls = {
            "enabled": tls_enabled,
            "sslmode": sslmode if tls_enabled else "disable",
            "sslrootcert": str(tls_directory / "ca.crt") if tls_enabled else None,
            "sslcert": str(tls_directory / "postgres.crt") if tls_enabled else None,
            "sslkey": str(tls_directory / "postgres.key") if tls_enabled else None,
        }
        if tls_enabled:
            query = urlencode({key: value for key, value in tls.items() if key != "enabled"})
            dsn = (
                f"postgresql://{user}@{uri_host}:{node.published_port}/"
                f"{quote(dbname, safe='')}?{query}"
            )
        else:
            dsn = (
                f"postgresql://{user}:{encoded_password}@{uri_host}:{node.published_port}/"
                f"{quote(dbname, safe='')}"
            )
        return {
            "node": node_name,
            "host": host,
            "port": node.published_port,
            "database": dbname,
            "user": self.config.postgres.superuser,
            "password": password,
            "tls": tls,
            "dsn": dsn,
        }

    def run_psql(self, node_name: str, *, database: str | None = None) -> int:
        executable = shutil.which("psql")
        if executable is None:
            raise DockerRuntimeError("psql is not installed on the host or is not in PATH")
        info = self.connection_info(node_name, database=database, show_password=True)
        environment = os.environ.copy()
        tls = info["tls"]
        if tls["enabled"]:
            environment.pop("PGPASSWORD", None)
            environment["PGSSLMODE"] = tls["sslmode"]
            environment["PGSSLROOTCERT"] = tls["sslrootcert"]
            environment["PGSSLCERT"] = tls["sslcert"]
            environment["PGSSLKEY"] = tls["sslkey"]
        else:
            environment["PGPASSWORD"] = self._superuser_password()
        return subprocess.run(
            [
                executable,
                "--host",
                info["host"],
                "--port",
                str(info["port"]),
                "--username",
                info["user"],
                "--dbname",
                info["database"],
            ],
            env=environment,
            check=False,
        ).returncode

    def run_ssh(self, node_name: str) -> int:
        executable = shutil.which("ssh")
        if executable is None:
            raise DockerRuntimeError("ssh is not installed on the host or is not in PATH")
        node = self._node_by_name(node_name)
        if node is None:
            raise DockerRuntimeError(f"node {node_name} is not configured")
        key = credential_paths(self.config.project_directory).ssh_private_key
        if not key.is_file():
            raise DockerRuntimeError(f"test SSH private key does not exist: {key}")
        host = self._host_connection_address(node)
        return subprocess.run(
            [
                executable,
                "-i",
                str(key),
                "-p",
                str(node.ssh_published_port),
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                f"root@{host}",
            ],
            check=False,
        ).returncode

    def logs(self, node_name: str, *, follow: bool = False, tail: int = 200) -> None:
        node = self._node_by_name(node_name)
        if node is None:
            raise DockerRuntimeError(f"node {node_name} is not configured")
        container = self._owned_container(node, node_name)
        if container is None:
            raise DockerRuntimeError(f"container {node.container_name} does not exist")
        container.reload()
        if container.status != "running":
            raise DockerRuntimeError(f"container {node.container_name} is not running")
        script = r"""
set -eu
latest=""
if [ -r "$PGDATA/current_logfiles" ]; then
  latest="$(awk '$1 == "csvlog" { print $2; exit }' "$PGDATA/current_logfiles")"
fi
if [ -z "$latest" ] || [ ! -r "$latest" ]; then
  latest="$(ls -1t "$PGLOG"/postgresql-*.csv 2>/dev/null | head -n 1 || true)"
fi
if [ -z "$latest" ]; then
  echo "no PostgreSQL CSV log files found in $PGLOG" >&2
  exit 3
fi
if [ "$FOLLOW" = "1" ]; then
  exec tail -n "$TAIL" -F "$latest"
fi
exec tail -n "$TAIL" "$latest"
""".strip()
        pgdata, _ = postgres_data_paths(self.config.postgres.version)
        result = container.exec_run(
            ["/bin/sh", "-ceu", script],
            environment={
                "PGDATA": pgdata,
                "PGLOG": PGLOG,
                "TAIL": str(tail),
                "FOLLOW": "1" if follow else "0",
            },
            stream=follow,
        )
        if follow:
            for chunk in result.output:
                print(chunk.decode("utf-8", errors="replace"), end="")
        else:
            output = (result.output or b"").decode("utf-8", errors="replace")
            if result.exit_code != 0:
                raise DockerRuntimeError(
                    f"cannot read PostgreSQL logs from {container.name}: {output.strip()}"
                )
            print(output, end="")

    def _csv_log_records(self, node_name: str) -> list[dict[str, Any]]:
        node = self._node_by_name(node_name)
        if node is None:
            raise DockerRuntimeError(f"node {node_name} is not configured")
        container = self._owned_container(node, node_name)
        if container is None:
            raise DockerRuntimeError(f"container {node.container_name} does not exist")
        container.reload()
        if container.status != "running":
            raise DockerRuntimeError(f"container {node.container_name} is not running")
        script = r"""
set -eu
found=0
for file in "$PGLOG"/postgresql-*.csv; do
  if [ -f "$file" ]; then
    found=1
    cat "$file"
  fi
done
if [ "$found" -eq 0 ]; then
  exit 3
fi
""".strip()
        result = container.exec_run(
            ["/bin/sh", "-ceu", script],
            environment={"PGLOG": PGLOG},
        )
        output = (result.output or b"").decode("utf-8", errors="replace")
        if result.exit_code != 0:
            raise DockerRuntimeError(f"no PostgreSQL CSV log files found for {node_name}")
        records: list[dict[str, Any]] = []
        for row in csv.reader(io.StringIO(output)):
            if len(row) < 14:
                continue
            records.append(
                {
                    "node": node_name,
                    "log_time": row[0],
                    "user": row[1],
                    "database": row[2],
                    "process_id": row[3],
                    "connection_from": row[4],
                    "session_id": row[5],
                    "command_tag": row[7],
                    "severity": row[11],
                    "sqlstate": row[12],
                    "message": row[13],
                    "detail": row[14] if len(row) > 14 else "",
                    "hint": row[15] if len(row) > 15 else "",
                    "context": row[18] if len(row) > 18 else "",
                    "query": row[19] if len(row) > 19 else "",
                    "application_name": row[22] if len(row) > 22 else "",
                    "backend_type": row[23] if len(row) > 23 else "",
                }
            )
        return records

    @staticmethod
    def _log_timestamp(value: str) -> datetime | None:
        text = value.strip()
        if text.endswith(" UTC"):
            text = text[:-4] + "+00:00"
        elif len(text) >= 3 and text[-3] in "+-" and text[-2:].isdigit():
            # PostgreSQL can emit a whole-hour offset such as +00. Python 3.10
            # requires the equivalent ISO 8601 form with explicit minutes.
            text += ":00"
        try:
            parsed = datetime.fromisoformat(text.replace(" ", "T", 1))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def log_records(
        self,
        node_name: str,
        *,
        tail: int = 200,
        since: datetime | None = None,
        until: datetime | None = None,
        severity: str | None = None,
        backend_type: str | None = None,
        contains: str | None = None,
    ) -> list[dict[str, Any]]:
        severity_order = {
            "DEBUG5": 0,
            "DEBUG4": 1,
            "DEBUG3": 2,
            "DEBUG2": 3,
            "DEBUG1": 4,
            "INFO": 5,
            "NOTICE": 6,
            "WARNING": 7,
            "ERROR": 8,
            "LOG": 9,
            "FATAL": 10,
            "PANIC": 11,
        }
        if severity is not None and severity.upper() not in severity_order:
            raise DockerRuntimeError(f"unknown PostgreSQL severity: {severity}")
        minimum_severity = severity_order[severity.upper()] if severity else None
        selected = self._configured_node_names() if node_name == "all" else [node_name]
        records: list[dict[str, Any]] = []
        for selected_node in selected:
            records.extend(self._csv_log_records(selected_node))
        needle = contains.casefold() if contains else None
        filtered: list[dict[str, Any]] = []
        for record in records:
            timestamp = self._log_timestamp(record["log_time"])
            if since is not None and (timestamp is None or timestamp < since):
                continue
            if until is not None and (timestamp is None or timestamp > until):
                continue
            if (
                minimum_severity is not None
                and severity_order.get(record["severity"], 0) < minimum_severity
            ):
                continue
            if (
                backend_type is not None
                and record["backend_type"].casefold() != backend_type.casefold()
            ):
                continue
            if needle is not None:
                haystack = "\n".join(
                    str(record[key]) for key in ("message", "detail", "hint", "context", "query")
                ).casefold()
                if needle not in haystack:
                    continue
            record["_timestamp"] = timestamp or datetime.min.replace(tzinfo=timezone.utc)
            filtered.append(record)
        filtered.sort(key=lambda item: (item["_timestamp"], item["node"], item["session_id"]))
        if tail >= 0:
            filtered = filtered[-tail:] if tail else []
        for record in filtered:
            record.pop("_timestamp", None)
        return filtered

    def sql(self, node_name: str, statement: str, *, database: str | None = None) -> str:
        node = self._node_by_name(node_name)
        if node is None:
            raise DockerRuntimeError(f"node {node_name} is not configured")
        container = self._owned_container(node, node_name)
        if container is None:
            raise DockerRuntimeError(f"container {node.container_name} does not exist")
        return self._psql(
            container,
            statement,
            database=database or self.config.postgres.database,
        )
