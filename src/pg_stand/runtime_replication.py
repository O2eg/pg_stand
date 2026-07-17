"""Physical and logical replication lifecycle operations."""

from __future__ import annotations

import json
import time
from typing import Any

from docker.errors import ContainerError, DockerException

from pg_stand.errors import DockerRuntimeError
from pg_stand.runtime_common import (
    MANAGED_RESOURCE_SUFFIX,
    PGLOG,
    PGWAL,
    STORAGE_ROOT,
    TLS_CA_FILE,
    TLS_POSTGRES_CERT_FILE,
    TLS_POSTGRES_KEY_FILE,
    TLS_REPLICATOR_CERT_FILE,
    TLS_REPLICATOR_KEY_FILE,
    TLS_RUNTIME_ROOT,
    TLS_SOURCE_MOUNT,
    NodeStoragePaths,
    _conninfo_quote,
    _pgpass_escape,
    _sql_identifier,
    _sql_literal,
    logical_postgres_command,
    postgres_data_paths,
    standby_bootstrap_script,
)


class ReplicationMixin:
    def _configure_replication_access(self, primary: Any) -> None:
        replication = self.config.topology.replication
        if replication is None:
            return
        role = _sql_identifier(replication.username)
        username = _sql_literal(replication.username)
        password = _sql_literal(self._replication_password())
        sql = f"""
DO $pg_stand$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = {username}) THEN
    ALTER ROLE {role} WITH REPLICATION LOGIN PASSWORD {password};
  ELSE
    CREATE ROLE {role} WITH REPLICATION LOGIN PASSWORD {password};
  END IF;
END
$pg_stand$;
"""
        self._psql(primary, sql, synchronous_commit_off=True)

    def _remove_stale_helper(self, name: str, *, check_hash: bool = True) -> None:
        helper = self._find_owned_helper(name)
        if helper is None:
            return
        self._assert_resource_labels(
            helper,
            "standby bootstrap container",
            check_hash=check_hash,
            expected_name=name,
            expected_node="standby-bootstrap",
            expected_kind="helper",
        )
        helper.remove(force=True)

    def _prepare_standby_data(
        self,
        primary: Any,
        standby_storage: NodeStoragePaths,
        network: Any,
    ) -> None:
        replication = self.config.topology.replication
        standby = self.config.standby
        if replication is None or standby is None:
            raise DockerRuntimeError("replication topology is incomplete")
        helper_name = f"{self.config.metadata.name}-standby-bootstrap{MANAGED_RESOURCE_SUFFIX}"
        self._remove_stale_helper(helper_name)
        pgdata, data_mount = postgres_data_paths(self.config.postgres.version)
        tls_enabled = self.config.postgres.tls.enabled
        pgpass_line = ""
        connection_parts = [
            f"host={self.config.primary.container_name}",
            "port=5432",
            f"user={replication.username}",
            f"application_name={replication.application_name}",
        ]
        if tls_enabled:
            connection_parts.extend(
                [
                    "sslmode=verify-ca",
                    f"sslrootcert={TLS_CA_FILE}",
                    f"sslcert={TLS_REPLICATOR_CERT_FILE}",
                    f"sslkey={TLS_REPLICATOR_KEY_FILE}",
                ]
            )
        else:
            pgpass_line = ":".join(
                [
                    self.config.primary.container_name,
                    "5432",
                    "*",
                    replication.username,
                    _pgpass_escape(self._replication_password()),
                ]
            )
            connection_parts.extend([f"passfile={pgdata}/standby.pgpass", "sslmode=disable"])
        primary_conninfo = " ".join(connection_parts)
        environment = {
            "PGDATA": pgdata,
            "PGDATA_MOUNT": data_mount,
            "PGWAL": PGWAL,
            "PGLOG": PGLOG,
            "POSTGRES_MAJOR": str(self.config.postgres.version),
            "PRIMARY_HOST": self.config.primary.container_name,
            "REPLICATION_USER": replication.username,
            "REPLICATION_PASSWORD": self._replication_password(),
            "PGPASS_LINE": pgpass_line,
            "PRIMARY_CONNINFO": primary_conninfo,
            "PG_STAND_TLS_ENABLED": "1" if tls_enabled else "0",
            "TLS_SOURCE_MOUNT": TLS_SOURCE_MOUNT,
            "TLS_RUNTIME_ROOT": TLS_RUNTIME_ROOT,
        }
        self.output(f"BOOTSTRAP data={standby_storage.data} from={primary.name}")
        helper: Any | None = None
        exit_code = 1
        logs = ""
        try:
            helper = self.client.containers.run(
                self.config.postgres.image_name,
                command=[standby_bootstrap_script()],
                entrypoint=["/bin/sh", "-ceu"],
                name=helper_name,
                detach=True,
                environment=environment,
                volumes=self._database_binds(standby_storage),
                network=network.name,
                labels=self._labels(node="standby-bootstrap", resource_kind="helper"),
            )
            result = helper.wait(timeout=300)
            exit_code = int(result.get("StatusCode", 1))
            logs = helper.logs().decode("utf-8", errors="replace").strip()
        except (DockerException, ContainerError) as exc:
            raise DockerRuntimeError(f"standby bootstrap failed: {exc}") from exc
        finally:
            if helper is not None:
                try:
                    helper.remove(force=True)
                except DockerException:
                    pass
        if exit_code != 0:
            raise DockerRuntimeError(f"standby bootstrap container exited with {exit_code}\n{logs}")

    def _wait_for_recovery(self, standby: Any, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        last = ""
        while time.monotonic() < deadline:
            try:
                last = self._psql(standby, "select pg_catalog.pg_is_in_recovery()")
                if last.strip() == "t":
                    return
            except DockerRuntimeError as exc:
                last = str(exc)
            time.sleep(1.0)
        raise DockerRuntimeError(
            f"standby {standby.name} did not enter recovery within {timeout_seconds}s: {last}"
        )

    def _replication_state(self, primary: Any) -> list[dict[str, str]]:
        sql = """
select application_name, state, sync_state,
       coalesce(write_lag::text, ''), coalesce(flush_lag::text, ''),
       coalesce(replay_lag::text, '')
from pg_catalog.pg_stat_replication
order by application_name
"""
        output = self._psql(primary, sql)
        rows: list[dict[str, str]] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            values = line.split("|")
            if len(values) != 6:
                continue
            rows.append(
                dict(
                    zip(
                        (
                            "application_name",
                            "state",
                            "sync_state",
                            "write_lag",
                            "flush_lag",
                            "replay_lag",
                        ),
                        values,
                        strict=True,
                    )
                )
            )
        return rows

    @staticmethod
    def _pipe_rows(output: str, columns: tuple[str, ...]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            values = line.split("|")
            if len(values) != len(columns):
                continue
            rows.append(dict(zip(columns, values, strict=True)))
        return rows

    def _physical_replication_details(self, primary: Any) -> list[dict[str, Any]]:
        replication = self.config.topology.replication
        if replication is None:
            raise DockerRuntimeError("physical replication is not configured")
        application_name = _sql_literal(replication.application_name)
        output = self._psql(
            primary,
            f"""
select application_name, coalesce(client_addr::text, ''), state, sync_state,
       coalesce(sent_lsn::text, ''), coalesce(write_lsn::text, ''),
       coalesce(flush_lsn::text, ''), coalesce(replay_lsn::text, ''),
       coalesce(pg_catalog.pg_wal_lsn_diff(pg_catalog.pg_current_wal_lsn(),
                                           replay_lsn)::bigint, 0)::text,
       coalesce((extract(epoch from write_lag) * 1000)::bigint, 0)::text,
       coalesce((extract(epoch from flush_lag) * 1000)::bigint, 0)::text,
       coalesce((extract(epoch from replay_lag) * 1000)::bigint, 0)::text,
       coalesce(backend_xmin::text, '')
from pg_catalog.pg_stat_replication
where application_name = {application_name}
order by application_name
""",
        )
        rows = self._pipe_rows(
            output,
            (
                "application_name",
                "client_address",
                "state",
                "sync_state",
                "sent_lsn",
                "write_lsn",
                "flush_lsn",
                "replay_lsn",
                "replay_lag_bytes",
                "write_lag_ms",
                "flush_lag_ms",
                "replay_lag_ms",
                "backend_xmin",
            ),
        )
        numeric = {
            "replay_lag_bytes",
            "write_lag_ms",
            "flush_lag_ms",
            "replay_lag_ms",
        }
        for row in rows:
            for name in numeric:
                row[name] = int(row[name] or 0)
        return rows

    def _replication_slots(self, primary: Any) -> list[dict[str, Any]]:
        output = self._psql(
            primary,
            """
select slot_name, slot_type, database::text, active::text,
       coalesce(active_pid::text, ''), coalesce(restart_lsn::text, ''),
       coalesce(confirmed_flush_lsn::text, ''),
       coalesce(pg_catalog.pg_wal_lsn_diff(pg_catalog.pg_current_wal_lsn(),
                                           restart_lsn)::bigint, 0)::text
from pg_catalog.pg_replication_slots
order by slot_name
""",
        )
        rows = self._pipe_rows(
            output,
            (
                "slot_name",
                "slot_type",
                "database",
                "active",
                "active_pid",
                "restart_lsn",
                "confirmed_flush_lsn",
                "retained_wal_bytes",
            ),
        )
        for row in rows:
            row["active"] = row["active"] == "true"
            row["retained_wal_bytes"] = int(row["retained_wal_bytes"] or 0)
        return rows

    def _wal_receiver_details(self, standby: Any) -> list[dict[str, Any]]:
        if self.config.postgres.version <= 11:
            written_lsn = "received_lsn"
            flushed_lsn = "received_lsn"
        else:
            written_lsn = "written_lsn"
            flushed_lsn = "flushed_lsn"
        output = self._psql(
            standby,
            f"""
select status, coalesce(slot_name, ''), coalesce(receive_start_lsn::text, ''),
       coalesce({written_lsn}::text, ''), coalesce({flushed_lsn}::text, ''),
       coalesce(latest_end_lsn::text, ''),
       coalesce(last_msg_send_time::text, ''),
       coalesce(last_msg_receipt_time::text, ''),
       coalesce(latest_end_time::text, ''),
       coalesce(pg_catalog.pg_wal_lsn_diff({flushed_lsn},
                    pg_catalog.pg_last_wal_replay_lsn())::bigint, 0)::text
from pg_catalog.pg_stat_wal_receiver
""",
        )
        rows = self._pipe_rows(
            output,
            (
                "status",
                "slot_name",
                "receive_start_lsn",
                "written_lsn",
                "flushed_lsn",
                "latest_end_lsn",
                "last_message_sent_at",
                "last_message_received_at",
                "latest_end_at",
                "receive_replay_gap_bytes",
            ),
        )
        for row in rows:
            row["receive_replay_gap_bytes"] = int(row["receive_replay_gap_bytes"] or 0)
        return rows

    def _logical_replication_details(self, logical: Any) -> dict[str, Any]:
        logical_replication = self.config.topology.logical_replication
        if logical_replication is None:
            return {}
        subscription_name = _sql_literal(logical_replication.subscription_name)
        output = self._psql(
            logical,
            "select subname, coalesce(pid::text, ''), coalesce(received_lsn::text, ''), "
            "coalesce(last_msg_send_time::text, ''), "
            "coalesce(last_msg_receipt_time::text, ''), "
            "coalesce(latest_end_lsn::text, ''), coalesce(latest_end_time::text, ''), "
            "coalesce(extract(epoch from (clock_timestamp() - latest_end_time))::bigint, 0)::text "
            "from pg_catalog.pg_stat_subscription "
            f"where subname = {subscription_name} order by pid nulls last",
            database=self.config.postgres.database,
        )
        workers = self._pipe_rows(
            output,
            (
                "subscription_name",
                "pid",
                "received_lsn",
                "last_message_sent_at",
                "last_message_received_at",
                "latest_end_lsn",
                "latest_end_at",
                "message_age_seconds",
            ),
        )
        for row in workers:
            row["message_age_seconds"] = int(row["message_age_seconds"] or 0)
        pending_output = self._psql(
            logical,
            "select r.srsubstate, count(*)::text "
            "from pg_catalog.pg_subscription_rel r "
            "join pg_catalog.pg_subscription s on s.oid = r.srsubid "
            f"where s.subname = {subscription_name} group by r.srsubstate order by 1",
            database=self.config.postgres.database,
        )
        table_states = {
            row["state"]: int(row["count"])
            for row in self._pipe_rows(pending_output, ("state", "count"))
        }
        return {
            **self._logical_replication_state(logical),
            "workers": workers,
            "table_states": table_states,
        }

    def replication_status(self) -> dict[str, Any]:
        self._ping()
        result: dict[str, Any] = {
            "project": self.config.metadata.name,
            "topology": self.config.topology.mode,
            "physical": [],
            "slots": [],
        }
        primary = self._owned_container(self.config.primary, "primary")
        if primary is None:
            raise DockerRuntimeError("primary container does not exist; run up first")
        primary.reload()
        if primary.status != "running":
            raise DockerRuntimeError("primary container is not running")
        if self.config.is_replicated:
            result["physical"] = self._physical_replication_details(primary)
            result["slots"] = self._replication_slots(primary)
            standby_config = self.config.standby
            if standby_config is None:
                raise DockerRuntimeError("physical standby configuration is incomplete")
            standby = self._owned_container(standby_config, "standby")
            if standby is not None:
                standby.reload()
                if standby.status == "running":
                    result["wal_receiver"] = self._wal_receiver_details(standby)
        if self.config.has_logical_replica:
            logical_config = self.config.logical
            if logical_config is None:
                raise DockerRuntimeError("logical replica configuration is incomplete")
            logical = self._owned_container(logical_config, "logical")
            if logical is not None:
                logical.reload()
                if logical.status == "running":
                    result["logical"] = self._logical_replication_details(logical)
        return result

    def _wait_for_replication(
        self,
        primary: Any,
        timeout_seconds: float,
        *,
        expected_sync_state: str | None = None,
    ) -> dict[str, str]:
        replication = self.config.topology.replication
        if replication is None:
            raise DockerRuntimeError("replication state requested for a single-node stand")
        deadline = time.monotonic() + timeout_seconds
        last_rows: list[dict[str, str]] = []
        while time.monotonic() < deadline:
            last_rows = self._replication_state(primary)
            for row in last_rows:
                if (
                    row["application_name"] == replication.application_name
                    and row["state"] == "streaming"
                    and (expected_sync_state is None or row["sync_state"] == expected_sync_state)
                ):
                    return row
            time.sleep(1.0)
        raise DockerRuntimeError(
            "standby did not reach required streaming state; last pg_stat_replication rows: "
            + json.dumps(last_rows, sort_keys=True)
        )

    def _apply_replication_mode(self, primary: Any, timeout_seconds: float) -> dict[str, str]:
        replication = self.config.topology.replication
        if replication is None:
            raise DockerRuntimeError("replication mode requested for a single-node stand")
        self._wait_for_replication(primary, timeout_seconds)
        synchronous_names = (
            f"FIRST 1 ({replication.application_name})" if replication.mode == "sync" else ""
        )
        self._psql(
            primary,
            "alter system set synchronous_standby_names = " + _sql_literal(synchronous_names),
            synchronous_commit_off=True,
        )
        self._psql(
            primary,
            "alter system set synchronous_commit = 'on'",
            synchronous_commit_off=True,
        )
        self._psql(
            primary,
            "select pg_catalog.pg_reload_conf()",
            synchronous_commit_off=True,
        )
        expected = "sync" if replication.mode == "sync" else "async"
        return self._wait_for_replication(
            primary,
            timeout_seconds,
            expected_sync_state=expected,
        )

    def _copy_schema_to_logical(self, logical: Any, *, tolerate_existing: bool = False) -> None:
        psql_error_mode = "" if tolerate_existing else "--set=ON_ERROR_STOP=1"
        script = r"""
set -eu
dump_file="/tmp/pg_stand_schema.sql"
rm -f "$dump_file"
if [ "$PG_STAND_TLS_ENABLED" = "1" ]; then
  export PGSSLMODE=verify-ca
  export PGSSLROOTCERT="$TLS_CA_FILE"
  export PGSSLCERT="$TLS_POSTGRES_CERT_FILE"
  export PGSSLKEY="$TLS_POSTGRES_KEY_FILE"
else
  export PGPASSWORD="$POSTGRES_PASSWORD"
fi
pg_dump \
  --host="$PRIMARY_HOST" --port=5432 \
  --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
  --schema-only --no-owner --no-privileges --no-publications --no-subscriptions \
  --file="$dump_file"
psql \
  --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
  $PSQL_ERROR_MODE --file="$dump_file"
rm -f "$dump_file"
""".strip()
        self.output(
            f"COPY schema primary={self.config.primary.container_name} logical={logical.name}"
        )
        self._exec(
            logical,
            ["/bin/sh", "-ceu", script],
            environment={
                "PRIMARY_HOST": self.config.primary.container_name,
                "POSTGRES_DB": self.config.postgres.database,
                "POSTGRES_USER": self.config.postgres.superuser,
                "POSTGRES_PASSWORD": self._superuser_password(),
                "PSQL_ERROR_MODE": psql_error_mode,
                "PG_STAND_TLS_ENABLED": ("1" if self.config.postgres.tls.enabled else "0"),
                "TLS_CA_FILE": TLS_CA_FILE,
                "TLS_POSTGRES_CERT_FILE": TLS_POSTGRES_CERT_FILE,
                "TLS_POSTGRES_KEY_FILE": TLS_POSTGRES_KEY_FILE,
            },
        )

    def _published_table_names(self, primary: Any) -> set[str]:
        logical_replication = self.config.topology.logical_replication
        if logical_replication is None:
            return set()
        output = self._psql(
            primary,
            "select schemaname || '.' || tablename "
            "from pg_catalog.pg_publication_tables where pubname = "
            + _sql_literal(logical_replication.publication_name)
            + " order by schemaname, tablename",
            database=self.config.postgres.database,
        )
        return {line.strip() for line in output.splitlines() if line.strip()}

    def _logical_table_names(self, logical: Any) -> set[str]:
        output = self._psql(
            logical,
            "select n.nspname || '.' || c.relname "
            "from pg_catalog.pg_class c "
            "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
            "where c.relkind in ('r', 'p') "
            "and n.nspname not in ('pg_catalog', 'information_schema') "
            "and n.nspname !~ '^pg_toast' order by 1",
            database=self.config.postgres.database,
        )
        return {line.strip() for line in output.splitlines() if line.strip()}

    def _sync_missing_logical_schema(self, primary: Any, logical: Any) -> list[str]:
        published_tables = self._published_table_names(primary)
        missing_before = sorted(published_tables - self._logical_table_names(logical))
        if not missing_before:
            return []
        self.output(f"SYNC logical_schema missing_tables={len(missing_before)}")
        self._copy_schema_to_logical(logical, tolerate_existing=True)
        missing_after = sorted(published_tables - self._logical_table_names(logical))
        if missing_after:
            raise DockerRuntimeError(
                "cannot create missing logical-replica table schemas: " + ", ".join(missing_after)
            )
        return missing_before

    def _ensure_all_tables_publication(self, primary: Any) -> None:
        logical_replication = self.config.topology.logical_replication
        if logical_replication is None:
            raise DockerRuntimeError("logical publication requested without logical configuration")
        publication_name = logical_replication.publication_name
        state = self._psql(
            primary,
            "select puballtables::text from pg_catalog.pg_publication where pubname = "
            + _sql_literal(publication_name),
            database=self.config.postgres.database,
            synchronous_commit_off=True,
        ).strip()
        if not state:
            self._psql(
                primary,
                f"create publication {_sql_identifier(publication_name)} for all tables",
                database=self.config.postgres.database,
                synchronous_commit_off=True,
            )
        elif state != "true":
            raise DockerRuntimeError(
                f"publication {publication_name} exists but is not a FOR ALL TABLES publication"
            )

    def _logical_subscription_exists(self, logical: Any) -> bool:
        logical_replication = self.config.topology.logical_replication
        if logical_replication is None:
            return False
        output = self._psql(
            logical,
            "select count(*) from pg_catalog.pg_subscription where subname = "
            + _sql_literal(logical_replication.subscription_name),
            database=self.config.postgres.database,
        )
        return output.strip() == "1"

    def _create_or_refresh_subscription(self, logical: Any) -> None:
        logical_replication = self.config.topology.logical_replication
        if logical_replication is None:
            raise DockerRuntimeError("logical subscription requested without logical configuration")
        subscription = _sql_identifier(logical_replication.subscription_name)
        if self._logical_subscription_exists(logical):
            self._psql(
                logical,
                f"alter subscription {subscription} enable",
                database=self.config.postgres.database,
                synchronous_commit_off=True,
            )
            self._psql(
                logical,
                f"alter subscription {subscription} refresh publication with "
                f"(copy_data = {'true' if logical_replication.copy_data else 'false'})",
                database=self.config.postgres.database,
                synchronous_commit_off=True,
            )
            return

        conninfo_parts = [
            f"host={_conninfo_quote(self.config.primary.container_name)}",
            "port=5432",
            f"dbname={_conninfo_quote(self.config.postgres.database)}",
            f"user={_conninfo_quote(self.config.postgres.superuser)}",
            f"application_name={_conninfo_quote(logical_replication.subscription_name)}",
        ]
        if self.config.postgres.tls.enabled:
            conninfo_parts.extend(
                [
                    "sslmode=verify-ca",
                    f"sslrootcert={_conninfo_quote(TLS_CA_FILE)}",
                    f"sslcert={_conninfo_quote(TLS_POSTGRES_CERT_FILE)}",
                    f"sslkey={_conninfo_quote(TLS_POSTGRES_KEY_FILE)}",
                ]
            )
        else:
            conninfo_parts.append(f"password={_conninfo_quote(self._superuser_password())}")
        conninfo = " ".join(conninfo_parts)
        copy_data = "true" if logical_replication.copy_data else "false"
        sql = (
            f"create subscription {subscription} connection {_sql_literal(conninfo)} "
            f"publication {_sql_identifier(logical_replication.publication_name)} with "
            f"(copy_data = {copy_data}, create_slot = true, enabled = true, "
            f"slot_name = {_sql_literal(logical_replication.slot_name)})"
        )
        self._psql(
            logical,
            sql,
            database=self.config.postgres.database,
            synchronous_commit_off=True,
        )

    def _logical_replication_state(self, logical: Any) -> dict[str, Any]:
        logical_replication = self.config.topology.logical_replication
        if logical_replication is None:
            return {}
        subscription_name = _sql_literal(logical_replication.subscription_name)
        worker_count = self._psql(
            logical,
            "select count(*) from pg_catalog.pg_stat_subscription "
            f"where subname = {subscription_name} and pid is not null",
            database=self.config.postgres.database,
        )
        pending_tables = self._psql(
            logical,
            "select count(*) from pg_catalog.pg_subscription_rel r "
            "join pg_catalog.pg_subscription s on s.oid = r.srsubid "
            f"where s.subname = {subscription_name} and r.srsubstate <> 'r'",
            database=self.config.postgres.database,
        )
        return {
            "database": self.config.postgres.database,
            "publication_name": logical_replication.publication_name,
            "subscription_name": logical_replication.subscription_name,
            "slot_name": logical_replication.slot_name,
            "worker_count": int(worker_count),
            "pending_tables": int(pending_tables),
        }

    def _wait_for_logical_replication(
        self,
        logical: Any,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_state = self._logical_replication_state(logical)
            if last_state["worker_count"] > 0 and last_state["pending_tables"] == 0:
                return last_state
            time.sleep(1.0)
        raise DockerRuntimeError(
            "logical subscription did not become ready; last state: "
            + json.dumps(last_state, sort_keys=True)
        )

    def _configure_logical_replication(
        self,
        primary: Any,
        logical: Any,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        subscription_exists = self._logical_subscription_exists(logical)
        if not subscription_exists:
            self._copy_schema_to_logical(logical)
        self._ensure_all_tables_publication(primary)
        added_tables: list[str] = []
        if subscription_exists:
            added_tables = self._sync_missing_logical_schema(primary, logical)
        self._create_or_refresh_subscription(logical)
        state = self._wait_for_logical_replication(logical, timeout_seconds)
        state["added_tables"] = added_tables
        return state

    def _drop_logical_subscription(self, logical: Any) -> None:
        logical_replication = self.config.topology.logical_replication
        if logical_replication is None or not self._logical_subscription_exists(logical):
            return
        self.output(f"DROP subscription={logical_replication.subscription_name}")
        self._psql(
            logical,
            f"drop subscription {_sql_identifier(logical_replication.subscription_name)}",
            database=self.config.postgres.database,
            synchronous_commit_off=True,
        )

    def _clean_node_storage(self, node_name: str) -> None:
        paths = self._node_storage(node_name)
        paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.output(f"CLEAN node={node_name} storage={paths.root}")
        self._run_storage_helper(
            "find /pg-stand-storage -mindepth 1 -maxdepth 1 -exec rm -rf -- '{}' +",
            volumes={str(paths.root): {"bind": STORAGE_ROOT, "mode": "rw"}},
        )

    def logical_sync(
        self,
        *,
        full: bool = False,
        timeout_seconds: float = 180.0,
        stop_timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        if not self.config.has_logical_replica or self.config.logical is None:
            raise DockerRuntimeError("this stand has no logical replica")
        self._ping()
        self._assert_up_configuration_is_applied()
        self._ensure_image()
        primary = self._owned_container(self.config.primary, "primary")
        logical = self._owned_container(self.config.logical, "logical")
        if primary is None or logical is None:
            raise DockerRuntimeError("primary and logical containers must exist; run up first")
        primary.reload()
        logical.reload()
        if primary.status != "running" or logical.status != "running":
            raise DockerRuntimeError("primary and logical containers must be running")

        if full:
            self._drop_logical_subscription(logical)
            self.output(f"STOP node=logical container={self.config.logical.container_name}")
            logical.stop(timeout=stop_timeout_seconds)
            self.output(f"REMOVE node=logical container={self.config.logical.container_name}")
            logical.remove(force=False, v=True)
            self._clean_node_storage("logical")
            self._prepare_storage()
            network = self._find_owned_network(self.config.docker.network_name)
            if network is None:
                raise DockerRuntimeError("managed stand network does not exist; run up")
            self._assert_resource_labels(
                network,
                "network",
                check_hash=False,
                expected_name=self.config.docker.network_name,
                expected_kind="network",
            )
            logical = self._ensure_database_container(
                node_name="logical",
                node=self.config.logical,
                storage=self._node_storage("logical"),
                network=network,
                command=logical_postgres_command(self.config),
            )
            self._wait_for_postgres(logical, timeout_seconds)
            self._ensure_node_parameters(logical, "logical", timeout_seconds)
            self._ensure_diagnostic_extensions(logical)

        state = self._configure_logical_replication(primary, logical, timeout_seconds)
        state["mode"] = "full" if full else "incremental"
        return state
