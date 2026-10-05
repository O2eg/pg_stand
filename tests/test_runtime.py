from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from docker.errors import ImageNotFound

from pg_stand.config import load_config
from pg_stand.credentials import clean_credentials, credentials_status, ensure_credentials
from pg_stand.database_credentials import APPLICATION_DATABASES, read_database_credentials
from pg_stand.errors import DockerRuntimeError
from pg_stand.runtime import (
    ACTIVE_LOCK_NETWORK,
    CONFIG_HASH_LABEL,
    INSTANCE_LABEL,
    MANAGED_LABEL,
    PGLOG,
    PGWAL,
    PROJECT_LABEL,
    RESOURCE_KIND_LABEL,
    StandManager,
    diagnostic_postgres_settings,
    discover_active_stand,
    effective_postgres_parameters,
    logging_postgres_settings,
    managed_dockerfile,
    postgres_data_paths,
    postgres_parameter_file,
    primary_postgres_command,
    standby_bootstrap_script,
    standby_postgres_command,
)
from pg_stand.runtime_common import (
    DIAGNOSTIC_EXTENSIONS,
    DIAGNOSTIC_PRELOADS,
    IMAGE_BASE_LABEL,
    IMAGE_FAMILY_LABEL,
    IMAGE_MAJOR_LABEL,
    IMAGE_PGBOUNCER_LABEL,
    IMAGE_RESOURCE_KIND,
    IMAGE_SCHEMA_LABEL,
    IMAGE_SCHEMA_VERSION,
    PGBOUNCER_VERSION,
)

ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = ROOT / "src" / "pg_stand"


def test_diagnostic_extensions_cover_existing_and_future_databases(tmp_path: Path) -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", project_directory=tmp_path)
    manager = StandManager(config, client=object())
    container = object()
    databases = ["app_01", "custom\ndb", "postgres", "template1"]

    def psql(_container, sql, **kwargs):
        if "pg_available_extensions" in sql:
            return "\n".join(DIAGNOSTIC_EXTENSIONS)
        if "pg_catalog.pg_database" in sql:
            assert "datallowconn" in sql and "datname <> 'template0'" in sql
            return json.dumps(databases)
        if sql == "show shared_preload_libraries":
            return ",".join(DIAGNOSTIC_PRELOADS)
        return ""

    manager._exec = MagicMock()
    manager._psql = MagicMock(side_effect=psql)
    manager._ensure_diagnostic_extensions(container)
    installed = {
        (call.kwargs["database"], call.args[1])
        for call in manager._psql.call_args_list
        if call.args[1].startswith("create extension")
    }
    assert installed == {
        (database, f'create extension if not exists "{extension}"')
        for database in databases
        for extension in DIAGNOSTIC_EXTENSIONS
    }


@pytest.fixture(scope="session")
def credential_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = tmp_path_factory.mktemp("credential-template")
    return ensure_credentials(project, output=lambda _: None).root


def copy_credential_template(template: Path, project: Path) -> None:
    destination = project / ".pg_stand" / "credentials"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, destination)


class _Resource:
    def __init__(self, attrs: dict, name: str = "resource") -> None:
        self.attrs = attrs
        self.name = name


def test_storage_paths_and_mounts_stay_inside_project(tmp_path: Path) -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "primary-standby-async.yaml",
        environment={},
        project_directory=tmp_path,
    )
    manager = StandManager(config, client=object())

    primary = manager._node_storage("primary")
    assert primary.root == tmp_path / ".pg_stand" / "pg-stand-async" / "primary"
    _, data_mount = postgres_data_paths(config.postgres.version)
    assert manager._storage_binds(primary) == {
        str(primary.data): {"bind": data_mount, "mode": "rw"},
        str(primary.wal): {"bind": PGWAL, "mode": "rw"},
        str(primary.log): {"bind": PGLOG, "mode": "rw"},
    }


def test_storage_mounts_cannot_escape_through_node_symlinks(tmp_path: Path) -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "single.yaml",
        environment={},
        project_directory=tmp_path,
    )
    external = tmp_path / "external"
    primary_root = config.storage_root / "primary"
    primary_root.mkdir(parents=True)
    external.mkdir()
    (primary_root / "data").symlink_to(external, target_is_directory=True)
    manager = StandManager(config, client=object())

    with pytest.raises(DockerRuntimeError, match="must not be a symlink"):
        manager._node_storage("primary")


def test_database_mounts_generated_credentials_read_only() -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "single.yaml",
        environment={},
        project_directory=ROOT,
        tls_enabled=True,
    )
    manager = StandManager(config, client=object())

    binds = manager._database_binds(manager._node_storage("primary"))

    assert binds[str((ROOT / ".pg_stand" / "credentials" / "ssh").resolve())] == {
        "bind": "/pg-stand-ssh-source",
        "mode": "ro",
    }
    assert binds[str((ROOT / ".pg_stand" / "credentials" / "tls").resolve())] == {
        "bind": "/pg-stand-tls-source",
        "mode": "ro",
    }


def test_credentials_are_generated_once_outside_the_repository_tree(tmp_path: Path) -> None:
    messages: list[str] = []

    paths = ensure_credentials(tmp_path, output=messages.append)
    first_status = credentials_status(tmp_path)
    ensure_credentials(tmp_path, output=messages.append)

    assert paths.root == tmp_path / ".pg_stand" / "credentials"
    assert first_status["ssh"]["state"] == "ready"
    assert first_status["tls"]["state"] == "ready"
    assert first_status["database"]["state"] == "ready"
    assert len(first_status["database"]["applications"]) == 10
    assert first_status["ssh"]["files"]["pg_stand_test"]["mode"] == "0o600"
    assert first_status["tls"]["files"]["server.key"]["mode"] == "0o600"
    assert first_status["database"]["files"]["passwords.json"]["mode"] == "0o600"
    assert not (paths.tls / "ca.key").exists()
    credentials = read_database_credentials(paths.database)
    passwords = [credentials.superuser_password, credentials.replication_password]
    passwords.extend(account.password for account in credentials.applications)
    assert tuple(account.database for account in credentials.applications) == APPLICATION_DATABASES
    assert len(set(passwords)) == 12
    assert len(messages) == 3
    assert clean_credentials(tmp_path) is True
    assert clean_credentials(tmp_path) is False


def test_credentials_reject_unexpected_entries(tmp_path: Path, credential_template: Path) -> None:
    copy_credential_template(credential_template, tmp_path)
    paths = ensure_credentials(tmp_path, output=lambda _: None)
    (paths.ssh / "unexpected.key").write_text("not managed", encoding="utf-8")

    assert credentials_status(tmp_path)["ssh"]["state"] == "invalid"
    with pytest.raises(DockerRuntimeError, match="unexpected entries"):
        ensure_credentials(tmp_path, output=lambda _: None)


def test_credentials_reject_mismatched_certificate_keys(
    tmp_path: Path, credential_template: Path
) -> None:
    copy_credential_template(credential_template, tmp_path)
    paths = ensure_credentials(tmp_path, output=lambda _: None)
    (paths.tls / "postgres.key").write_text("not a private key\n", encoding="utf-8")

    status = credentials_status(tmp_path)

    assert status["tls"]["state"] == "invalid"
    assert "credential command failed" in status["tls"]["validation_error"]
    with pytest.raises(DockerRuntimeError, match="credential command failed"):
        ensure_credentials(tmp_path, output=lambda _: None)


def test_database_credentials_reject_duplicate_passwords(
    tmp_path: Path, credential_template: Path
) -> None:
    copy_credential_template(credential_template, tmp_path)
    paths = ensure_credentials(tmp_path, output=lambda _: None)
    document = json.loads(paths.database_passwords.read_text(encoding="utf-8"))
    document["applications"][1]["password"] = document["applications"][0]["password"]
    paths.database_passwords.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    status = credentials_status(tmp_path)

    assert status["database"]["state"] == "invalid"
    assert "distinct strong passwords" in status["database"]["validation_error"]
    with pytest.raises(DockerRuntimeError, match="distinct strong passwords"):
        ensure_credentials(tmp_path, output=lambda _: None)


def test_credentials_cannot_escape_through_a_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external"
    (project / ".pg_stand").mkdir(parents=True)
    external.mkdir()
    (project / ".pg_stand" / "credentials").symlink_to(external, target_is_directory=True)

    with pytest.raises(DockerRuntimeError, match="escaped the project directory"):
        ensure_credentials(project, output=lambda _: None)


def test_container_has_postgres_ssh_and_resource_limits(
    tmp_path: Path, credential_template: Path
) -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "single.yaml",
        environment={},
        project_directory=tmp_path,
    )
    client = MagicMock()
    client.containers.list.return_value = []
    manager = StandManager(config, client=client)
    copy_credential_template(credential_template, tmp_path)
    ensure_credentials(tmp_path, output=lambda _: None)

    manager._ensure_database_container(
        node_name="primary",
        node=config.primary,
        storage=manager._node_storage("primary"),
        network=SimpleNamespace(name="pg-stand-test-network"),
        command=["postgres"],
    )

    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["ports"] == {
        "5432/tcp": ("127.0.0.1", 55432),
        "22/tcp": ("127.0.0.1", 56432),
        "6432/tcp": ("127.0.0.1", 57432),
        "6433/tcp": ("127.0.0.1", 58432),
    }
    assert kwargs["nano_cpus"] == 1_000_000_000
    assert kwargs["mem_limit"] == "1g"
    assert kwargs["hostname"] == "pg-stand-single-primary-pg-stand-managed"
    assert kwargs["volumes"][str(tmp_path / ".pg_stand" / "credentials" / "ssh")] == {
        "bind": "/pg-stand-ssh-source",
        "mode": "ro",
    }
    client.containers.list.assert_called_once_with(
        all=True,
        filters={
            "label": [
                "io.pg-stand.managed=true",
                "io.pg-stand.resource-kind=stand-node",
                "io.pg-stand.project=pg-stand-single",
                f"io.pg-stand.instance={config.instance_id}",
            ]
        },
    )


def test_readiness_waits_for_final_postgres_pid_one() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    manager = StandManager(config, client=object())
    container = MagicMock(name="primary", status="running")
    container.name = "primary"
    container.exec_run.side_effect = [
        SimpleNamespace(
            exit_code=1,
            output=b"container PID 1 is docker-entrypoint.sh, waiting for final postgres process",
        ),
        SimpleNamespace(exit_code=0, output=b"/var/run/postgresql:5432 - accepting connections"),
    ]

    with patch("pg_stand.runtime.time.sleep"):
        manager._wait_for_postgres(container, timeout_seconds=5)

    assert container.exec_run.call_count == 2
    command = container.exec_run.call_args.args[0]
    assert command[:2] == ["/bin/sh", "-ceu"]
    assert "/proc/1/comm" in command[2]


@pytest.mark.parametrize(
    ("pid_one", "children", "child_processes", "ready"),
    [
        ("postgres", "", {}, True),
        ("docker-init", "7 ", {"7": "postgres"}, True),
        ("docker-init", "7 84 ", {"7": "bash", "84": "postgres"}, False),
        ("docker-init", "", {}, False),
        ("bash", "84 ", {"84": "postgres"}, False),
    ],
)
def test_readiness_checks_final_server_behind_docker_init(
    tmp_path: Path, pid_one: str, children: str, child_processes: dict[str, str], ready: bool
) -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    manager = StandManager(config, client=object())
    container = MagicMock(status="running")
    container.exec_run.return_value = SimpleNamespace(exit_code=0, output=b"ready")
    manager._wait_for_postgres(container, timeout_seconds=5)
    command = container.exec_run.call_args.args[0]

    proc = tmp_path / "proc"
    (proc / "1/task/1").mkdir(parents=True)
    (proc / "1/comm").write_text(pid_one + "\n")
    (proc / "1/task/1/children").write_text(children)
    for pid, process in child_processes.items():
        (proc / pid).mkdir()
        (proc / pid / "comm").write_text(process + "\n")
    probe = tmp_path / "pg_isready"
    probe.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    probe.chmod(0o755)
    script = command[2].replace("/proc/", "./proc/").replace("exec pg_isready", "exec ./pg_isready")
    result = subprocess.run(
        [*command[:2], script, *command[3:]], cwd=tmp_path, text=True, capture_output=True
    )
    assert (result.returncode == 0) is ready, result.stderr
    if ready:
        assert result.stdout.splitlines() == ["--username", "postgres", "--dbname", "postgres"]
    else:
        assert "waiting for final postgres process" in result.stdout


def test_active_lock_rejects_a_second_project() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    client = MagicMock()
    client.networks.list.return_value = [
        _Resource(
            {
                "Labels": {
                    PROJECT_LABEL: "another-pg-stand-project",
                    MANAGED_LABEL: "true",
                    RESOURCE_KIND_LABEL: "active-lock",
                }
            },
            name=ACTIVE_LOCK_NETWORK,
        )
    ]
    manager = StandManager(config, client=client)

    with pytest.raises(DockerRuntimeError, match="already active"):
        manager._acquire_active_lock()

    client.networks.create.assert_not_called()
    client.networks.list.assert_called_once_with(
        filters={
            "label": [
                "io.pg-stand.managed=true",
                "io.pg-stand.resource-kind=active-lock",
            ]
        }
    )


def test_active_lock_is_created_with_exclusive_managed_identity() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    client = MagicMock()
    client.networks.list.return_value = []
    manager = StandManager(config, client=client)

    assert manager._acquire_active_lock() is True

    kwargs = client.networks.create.call_args.kwargs
    assert client.networks.create.call_args.args == (ACTIVE_LOCK_NETWORK,)
    assert kwargs["check_duplicate"] is True
    assert kwargs["labels"][PROJECT_LABEL] == "pg-stand-single"
    assert kwargs["labels"][INSTANCE_LABEL] == config.instance_id
    assert kwargs["labels"][MANAGED_LABEL] == "true"
    assert kwargs["labels"][RESOURCE_KIND_LABEL] == "active-lock"


@pytest.mark.parametrize("applied", [False, True])
def test_plan_reports_foreign_active_stand_before_claiming_it_can_apply(
    tmp_path: Path,
    applied: bool,
) -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", project_directory=tmp_path)
    client = MagicMock()
    client.networks.list.return_value = [
        _Resource(
            {
                "Labels": {
                    PROJECT_LABEL: "another-stand",
                    MANAGED_LABEL: "true",
                    RESOURCE_KIND_LABEL: "active-lock",
                }
            },
            name=ACTIVE_LOCK_NETWORK,
        ),
    ]
    manager = StandManager(config, client=client)
    if applied:
        manager._record_applied_state()
    blocked = manager.plan()
    assert blocked["required_action"] == "blocked"
    assert blocked["can_apply"] is False
    assert "another-stand" in blocked["reason"]
    client.networks.create.assert_not_called()
    client.containers.run.assert_not_called()
    client.networks.list.return_value = []
    available = manager.plan()
    assert available["required_action"] == ("none" if applied else "up")
    assert available["plan_hash"] != blocked["plan_hash"]


@pytest.mark.parametrize(
    ("version", "pgdata", "data_mount"),
    [
        (17, "/var/lib/postgresql/data", "/var/lib/postgresql/data"),
        (18, "/var/lib/postgresql/18/docker", "/var/lib/postgresql"),
    ],
)
def test_postgres_data_paths(version: int, pgdata: str, data_mount: str) -> None:
    assert postgres_data_paths(version) == (pgdata, data_mount)


@pytest.mark.parametrize(
    ("version", "dockerfile", "family"),
    [
        (10, "docker/Dockerfile.pg10", "pg10"),
        (11, "docker/Dockerfile.pg11-pg12", "pg11-pg12"),
        (12, "docker/Dockerfile.pg11-pg12", "pg11-pg12"),
        (13, "docker/Dockerfile.pg13-pg17", "pg13-pg17"),
        (17, "docker/Dockerfile.pg13-pg17", "pg13-pg17"),
        (18, "docker/Dockerfile.pg18", "pg18"),
    ],
)
def test_managed_dockerfile_matrix(version: int, dockerfile: str, family: str) -> None:
    assert managed_dockerfile(version) == (dockerfile, family)


def test_compatible_managed_image_is_reused_without_rebuild() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    _dockerfile, family = managed_dockerfile(config.postgres.version)
    image = MagicMock()
    image.attrs = {
        "Config": {
            "Labels": {
                IMAGE_SCHEMA_LABEL: IMAGE_SCHEMA_VERSION,
                IMAGE_BASE_LABEL: config.postgres.base_image_name,
                IMAGE_FAMILY_LABEL: family,
                IMAGE_MAJOR_LABEL: str(config.postgres.version),
                IMAGE_PGBOUNCER_LABEL: PGBOUNCER_VERSION,
                MANAGED_LABEL: "true",
                RESOURCE_KIND_LABEL: IMAGE_RESOURCE_KIND,
            }
        }
    }
    client = MagicMock()
    client.images.get.return_value = image
    manager = StandManager(config, client=client)

    manager._ensure_image()

    client.images.get.assert_called_once_with(config.postgres.image_name)
    client.images.build.assert_not_called()


def test_planning_statistics_are_enabled_only_when_supported() -> None:
    pg12 = dict(diagnostic_postgres_settings(12))
    pg13 = dict(diagnostic_postgres_settings(13))
    pg14 = dict(diagnostic_postgres_settings(14))

    assert "pg_stat_statements.track_planning" not in pg12
    assert "pg_stat_kcache.track_planning" not in pg12
    assert "auto_explain.log_wal" not in pg12
    assert pg13["pg_stat_statements.track_planning"] == "on"
    assert pg13["pg_stat_kcache.track_planning"] == "on"
    assert pg13["auto_explain.log_wal"] == "on"
    assert "compute_query_id" not in pg13
    assert pg14["compute_query_id"] == "on"
    assert pg14["track_wal_io_timing"] == "on"
    assert pg14["auto_explain.log_min_duration"] == "100ms"
    assert pg14["auto_explain.log_timing"] == "on"
    assert pg14["auto_explain.log_format"] == "text"
    assert pg14["track_activity_query_size"] == "4096"
    assert pg14["pg_stat_statements.max"] == "5000"


def test_fixed_csv_logging_profile_is_version_aware() -> None:
    pg17 = dict(logging_postgres_settings(17))
    pg18 = dict(logging_postgres_settings(18))

    expected = {
        "log_destination": "csvlog",
        "logging_collector": "on",
        "log_file_mode": "0640",
        "log_rotation_size": "256MB",
        "log_min_duration_statement": "3000",
        "log_statement": "ddl",
        "log_lock_waits": "on",
        "deadlock_timeout": "1s",
        "log_temp_files": "64MB",
        "log_autovacuum_min_duration": "5s",
    }
    assert expected.items() <= pg17.items()
    assert expected.items() <= pg18.items()
    assert pg17["log_connections"] == "off"
    assert pg18["log_connections"] == ""
    assert pg17["log_replication_commands"] == "on"
    assert pg17["log_recovery_conflict_waits"] == "on"
    assert "log_recovery_conflict_waits" not in dict(logging_postgres_settings(13))


def test_declarative_parameters_are_written_to_a_separate_config_file() -> None:
    assert postgres_parameter_file({"work_mem": "16MB", "application_name": "O'Reilly\\test"}) == (
        "application_name = 'O''Reilly\\\\test'\nwork_mem = '16MB'\n"
    )


def test_existing_cluster_superuser_password_is_reapplied(
    tmp_path: Path, credential_template: Path
) -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "single.yaml", environment={}, project_directory=tmp_path
    )
    copy_credential_template(credential_template, tmp_path)
    paths = ensure_credentials(tmp_path, output=lambda _: None)
    password = read_database_credentials(paths.database).superuser_password
    manager = StandManager(config, client=object())
    primary = object()

    with patch.object(manager, "_psql") as psql:
        manager._ensure_superuser_password(primary)

    assert psql.call_args.args == (
        primary,
        f"alter role \"postgres\" with password '{password}'",
    )
    assert psql.call_args.kwargs == {"synchronous_commit_off": True}


def test_application_roles_and_databases_are_created_idempotently(
    tmp_path: Path, credential_template: Path
) -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "single.yaml", environment={}, project_directory=tmp_path
    )
    copy_credential_template(credential_template, tmp_path)
    paths = ensure_credentials(tmp_path, output=lambda _: None)
    credentials = read_database_credentials(paths.database)
    manager = StandManager(config, client=object())
    calls: list[tuple[str, dict]] = []

    def psql(_container, sql: str, **kwargs):
        calls.append((sql, kwargs))
        if "from pg_catalog.pg_database" in sql:
            return "1" if "'app_01'" in sql else "0"
        return ""

    with patch.object(manager, "_psql", side_effect=psql):
        manager._ensure_application_databases(object())

    role_sql = [sql for sql, _ in calls if "pg_catalog.pg_roles" in sql]
    create_database_sql = [sql for sql, _ in calls if sql.startswith("create database")]
    alter_database_sql = [sql for sql, _ in calls if sql.startswith("alter database")]
    assert len(role_sql) == 10
    assert len(create_database_sql) == 9
    assert alter_database_sql == ['alter database "app_01" owner to "user_app_01"']
    for account in credentials.applications:
        matching_role_sql = next(sql for sql in role_sql if f'"{account.owner}"' in sql)
        assert account.password in matching_role_sql
    assert all(kwargs == {"synchronous_commit_off": True} for _, kwargs in calls)


def test_postgres_commands_include_managed_replication_settings() -> None:
    config = load_config(ASSET_ROOT / "configs" / "primary-standby-sync.yaml", environment={})

    primary = primary_postgres_command(config)
    standby = standby_postgres_command(config)

    assert "wal_level=replica" in primary
    assert "wal_keep_size=256MB" in primary
    assert "max_wal_senders=10" in primary
    assert f"log_directory={PGLOG}" in primary
    assert "logging_collector=on" in primary
    assert "log_destination=csvlog" in primary
    assert "log_statement=ddl" not in primary
    assert "shared_buffers=256MB" not in primary
    assert "hot_standby=on" in standby
    assert "hot_standby_feedback=on" in standby
    assert (
        "shared_preload_libraries=pg_stat_statements,pg_wait_sampling,pg_stat_kcache,auto_explain"
        in primary
    )


def test_tls_postgres_command_uses_repository_runtime_certificates() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={}, tls_enabled=True)

    command = primary_postgres_command(config)

    assert "ssl=on" in command
    assert "ssl_ca_file=/pg-stand-tls/ca.crt" in command
    assert "ssl_cert_file=/pg-stand-tls/server.crt" in command
    assert "ssl_key_file=/pg-stand-tls/server.key" in command
    assert "ssl=off" in primary_postgres_command(
        load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    )


def test_tunable_diagnostic_defaults_are_overridden_in_parameter_file() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    config = replace(
        config,
        postgres=replace(
            config.postgres,
            parameters={
                **config.postgres.parameters,
                "log_min_duration_statement": "250ms",
                "auto_explain.log_min_duration": "50ms",
                "pg_stat_statements.max": "10000",
            },
        ),
    )

    parameters = effective_postgres_parameters(config)

    assert parameters["log_min_duration_statement"] == "250ms"
    assert parameters["auto_explain.log_min_duration"] == "50ms"
    assert parameters["pg_stat_statements.max"] == "10000"
    assert parameters["log_statement"] == "ddl"
    assert "log_directory" not in parameters


def test_logical_profile_uses_logical_wal_level() -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "primary-standby-sync-logical.yaml", environment={}
    )

    command = primary_postgres_command(config)

    assert "wal_level=logical" in command
    assert config.logical is not None
    assert config.topology.logical_replication is not None


def test_old_postgres_uses_wal_keep_segments() -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "primary-standby-async.yaml",
        environment={},
        postgres_version=12,
    )

    command = primary_postgres_command(config)

    assert "wal_keep_segments=64" in command
    assert "wal_keep_size=256MB" not in command


def test_bootstrap_supports_pre_and_post_postgres_12_recovery() -> None:
    script = standby_bootstrap_script()

    assert "pg_basebackup" in script
    assert "standby.signal" in script
    assert "recovery.conf" in script
    assert "standby.pgpass" in script
    assert '--waldir="$PGWAL"' in script
    assert "PGSSLMODE=verify-ca" in script
    assert 'PGSSLKEY="$TLS_RUNTIME_ROOT/replicator.key"' in script


def test_tls_standby_bootstrap_uses_certificates_without_password_conninfo(
    tmp_path: Path, credential_template: Path
) -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "primary-standby-sync-logical.yaml",
        environment={},
        project_directory=tmp_path,
        tls_enabled=True,
    )
    client = MagicMock()
    client.containers.list.return_value = []
    helper = MagicMock()
    helper.wait.return_value = {"StatusCode": 0}
    helper.logs.return_value = b""
    client.containers.run.return_value = helper
    manager = StandManager(config, client=client)
    copy_credential_template(credential_template, tmp_path)
    ensure_credentials(tmp_path, output=lambda _: None)

    manager._prepare_standby_data(
        SimpleNamespace(name=config.primary.container_name),
        manager._node_storage("standby"),
        SimpleNamespace(name=config.docker.network_name),
    )

    kwargs = client.containers.run.call_args.kwargs
    environment = kwargs["environment"]
    assert environment["PG_STAND_TLS_ENABLED"] == "1"
    assert "sslmode=verify-ca" in environment["PRIMARY_CONNINFO"]
    assert "sslcert=/pg-stand-tls/replicator.crt" in environment["PRIMARY_CONNINFO"]
    assert "password=" not in environment["PRIMARY_CONNINFO"]
    assert environment["PGPASS_LINE"] == ""
    tls_credentials = (tmp_path / ".pg_stand" / "credentials" / "tls").resolve()
    assert kwargs["volumes"][str(tls_credentials)]["mode"] == "ro"


def test_reload_rejects_resource_changes_and_requires_restart(tmp_path: Path) -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "single.yaml",
        environment={},
        project_directory=tmp_path,
    )
    manager = StandManager(config, client=MagicMock())
    manager._record_applied_state()
    changed = replace(
        config,
        primary=replace(config.primary, memory_limit="2g"),
    )
    changed_manager = StandManager(changed, client=MagicMock())
    changed_manager.client.ping.return_value = True

    with pytest.raises(DockerRuntimeError, match="use apply --restart"):
        changed_manager.apply(mode="reload")


@pytest.mark.parametrize("container_layout", [False, True])
def test_resource_labels_support_docker_resource_layouts(container_layout: bool) -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    manager = StandManager(config, client=object())
    labels = {
        PROJECT_LABEL: config.metadata.name,
        INSTANCE_LABEL: config.instance_id,
        MANAGED_LABEL: "true",
        CONFIG_HASH_LABEL: config.config_hash,
    }
    attrs = {"Config": {"Labels": labels}} if container_layout else {"Labels": labels}

    manager._assert_resource_labels(_Resource(attrs), "test resource")


def test_resource_labels_reject_same_project_from_another_checkout(tmp_path: Path) -> None:
    owner = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    other = replace(owner, project_directory=tmp_path)
    labels = {
        PROJECT_LABEL: owner.metadata.name,
        INSTANCE_LABEL: owner.instance_id,
        MANAGED_LABEL: "true",
        CONFIG_HASH_LABEL: owner.config_hash,
    }

    with pytest.raises(DockerRuntimeError, match="project instance"):
        StandManager(other, client=object())._assert_resource_labels(
            _Resource({"Labels": labels}), "test resource"
        )


@pytest.mark.parametrize(
    ("filename", "standby_names", "expected_state"),
    [
        ("primary-standby-async.yaml", "", "async"),
        ("primary-standby-sync.yaml", "FIRST 1 (pg_stand_sync)", "sync"),
    ],
)
def test_replication_mode_uses_separate_alter_system_statements(
    filename: str,
    standby_names: str,
    expected_state: str,
) -> None:
    config = load_config(ASSET_ROOT / "configs" / filename, environment={})
    manager = StandManager(config, client=object())
    primary = object()
    streaming = {
        "application_name": config.topology.replication.application_name,
        "state": "streaming",
        "sync_state": expected_state,
    }

    with (
        patch.object(manager, "_wait_for_replication", side_effect=[streaming, streaming]) as wait,
        patch.object(manager, "_psql", return_value="") as psql,
    ):
        assert manager._apply_replication_mode(primary, 30) == streaming

    sql_calls = [item.args[1] for item in psql.call_args_list]
    assert sql_calls == [
        f"alter system set synchronous_standby_names = '{standby_names}'",
        "alter system set synchronous_commit = 'on'",
        "select pg_catalog.pg_reload_conf()",
    ]
    assert all(item.kwargs["synchronous_commit_off"] for item in psql.call_args_list)
    assert wait.call_args_list == [
        call(primary, 30),
        call(primary, 30, expected_sync_state=expected_state),
    ]


def test_configless_active_discovery_is_strictly_label_filtered() -> None:
    client = MagicMock()
    client.ping.return_value = True
    client.networks.list.return_value = [
        _Resource(
            {
                "Labels": {
                    PROJECT_LABEL: "pg-stand-single",
                    INSTANCE_LABEL: "test-instance",
                    MANAGED_LABEL: "true",
                    RESOURCE_KIND_LABEL: "active-lock",
                    "io.pg-stand.postgres-major": "17",
                }
            },
            name=ACTIVE_LOCK_NETWORK,
        )
    ]
    container = MagicMock()
    container.name = "pg-stand-single-primary-pg-stand-managed"
    container.short_id = "abc123"
    container.status = "running"
    container.attrs = {
        "Config": {
            "Hostname": container.name,
            "Labels": {
                PROJECT_LABEL: "pg-stand-single",
                INSTANCE_LABEL: "test-instance",
                MANAGED_LABEL: "true",
                RESOURCE_KIND_LABEL: "stand-node",
                "io.pg-stand.node": "primary",
                "io.pg-stand.postgres-major": "17",
            },
        },
        "HostConfig": {"NanoCpus": 1_000_000_000, "Memory": 1024},
        "NetworkSettings": {"Networks": {}, "Ports": {}},
    }
    client.containers.list.return_value = [container]

    result = discover_active_stand(client=client)

    assert result["active"] is True
    assert result["project"] == "pg-stand-single"
    assert result["instance_id"] == "test-instance"
    assert result["nodes"]["primary"]["limits"]["cpu_limit"] == 1.0
    client.networks.list.assert_called_once_with(
        filters={
            "label": [
                "io.pg-stand.managed=true",
                "io.pg-stand.resource-kind=active-lock",
            ]
        }
    )
    assert (
        f"{PROJECT_LABEL}=pg-stand-single"
        in client.containers.list.call_args.kwargs["filters"]["label"]
    )
    assert (
        f"{INSTANCE_LABEL}=test-instance"
        in client.containers.list.call_args.kwargs["filters"]["label"]
    )


def test_plan_is_read_only_and_classifies_offline_changes_conservatively(tmp_path: Path) -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "single.yaml",
        environment={},
        project_directory=tmp_path,
    )
    manager = StandManager(config, client=MagicMock())
    manager._record_applied_state()
    changed = replace(
        config,
        postgres=replace(
            config.postgres, parameters={**config.postgres.parameters, "work_mem": "8MB"}
        ),
    )
    client = MagicMock()
    client.ping.return_value = True
    client.containers.list.return_value = []
    changed_manager = StandManager(changed, client=client)

    result = changed_manager.plan()
    repeated = changed_manager.plan()

    assert result["required_action"] == "restart"
    assert result["desired_state_hash"] == changed.config_hash
    assert result["plan_hash"] == repeated["plan_hash"]
    assert result["parameter_changes"] == [
        {
            "name": "work_mem",
            "applied": None,
            "desired": "8MB",
            "context": "unknown",
            "action": "restart",
        }
    ]
    client.containers.run.assert_not_called()
    client.images.build.assert_not_called()
    state = changed_manager._read_applied_state()
    assert state is not None
    assert "password" not in str(state["document"])


def test_plan_detects_reloadable_parameter_with_a_running_primary(tmp_path: Path) -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "single.yaml",
        environment={},
        project_directory=tmp_path,
    )
    StandManager(config, client=MagicMock())._record_applied_state()
    changed = replace(
        config,
        postgres=replace(
            config.postgres, parameters={**config.postgres.parameters, "work_mem": "8MB"}
        ),
    )
    manager = StandManager(changed, client=MagicMock())
    manager.client.ping.return_value = True
    primary = MagicMock(status="running")

    with (
        patch.object(manager, "_owned_container", return_value=primary),
        patch.object(manager, "_parameter_contexts", return_value={"work_mem": "user"}),
    ):
        result = manager.plan(requested_mode="reload")

    assert result["required_action"] == "reload"
    assert result["can_apply"] is True


def test_applied_state_cannot_escape_through_a_symlink(tmp_path: Path) -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "single.yaml",
        environment={},
        project_directory=tmp_path,
    )
    config.storage_root.mkdir(parents=True)
    external = tmp_path / "external-state.json"
    external.write_text("{}\n", encoding="utf-8")
    (config.storage_root / ".pg-stand-applied.json").symlink_to(external)

    with pytest.raises(DockerRuntimeError, match="must not be a symlink"):
        StandManager(config, client=object())._read_applied_state()


def test_connection_info_redacts_password_by_default(
    tmp_path: Path, credential_template: Path
) -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "single.yaml", environment={}, project_directory=tmp_path
    )
    copy_credential_template(credential_template, tmp_path)
    paths = ensure_credentials(tmp_path, output=lambda _: None)
    expected = read_database_credentials(paths.database).superuser_password
    manager = StandManager(config, client=object())

    redacted = manager.connection_info("primary")
    visible = manager.connection_info("primary", show_password=True)

    assert redacted["password"] == "<redacted>"
    assert expected not in redacted["dsn"]
    assert visible["password"] == expected
    assert expected in visible["dsn"]


def test_tls_connection_info_uses_client_certificate_and_no_password() -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "single.yaml",
        environment={},
        project_directory=ROOT,
        tls_enabled=True,
    )
    manager = StandManager(config, client=object())

    info = manager.connection_info("primary", show_password=True)

    assert info["password"] is None
    assert info["tls"]["enabled"] is True
    assert info["tls"]["sslmode"] == "verify-full"
    assert info["tls"]["sslkey"].endswith("/.pg_stand/credentials/tls/postgres.key")
    assert "password=" not in info["dsn"]
    assert "sslmode=verify-full" in info["dsn"]
    assert "sslcert=" in info["dsn"]


@pytest.mark.parametrize("tls_enabled", [False, True])
def test_connection_info_brackets_ipv6_hosts(
    tls_enabled: bool, tmp_path: Path, credential_template: Path
) -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "single.yaml",
        environment={},
        project_directory=tmp_path,
        tls_enabled=tls_enabled,
    )
    config = replace(config, primary=replace(config.primary, bind_address="::1"))
    manager = StandManager(config, client=object())
    copy_credential_template(credential_template, tmp_path)
    ensure_credentials(tmp_path, output=lambda _: None)

    info = manager.connection_info("primary", show_password=True)

    assert info["host"] == "::1"
    assert "@[::1]:55432/" in info["dsn"]
    if tls_enabled:
        assert info["tls"]["sslmode"] == "verify-full"


def test_connection_info_uses_ipv6_loopback_for_ipv6_wildcard() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    config = replace(config, primary=replace(config.primary, bind_address="::"))

    info = StandManager(config, client=object()).connection_info("primary")

    assert info["host"] == "::1"
    assert "@[::1]:55432/" in info["dsn"]


def test_tls_logical_subscription_conninfo_uses_client_certificate() -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "primary-standby-sync-logical.yaml",
        environment={},
        tls_enabled=True,
    )
    manager = StandManager(config, client=object())

    with (
        patch.object(manager, "_logical_subscription_exists", return_value=False),
        patch.object(manager, "_psql", return_value="") as psql,
    ):
        manager._create_or_refresh_subscription(object())

    sql = psql.call_args.args[1]
    assert psql.call_args.kwargs["database"] == "app_01"
    assert "dbname=" in sql and "app_01" in sql
    assert "sslmode=verify-ca" in sql
    assert "/pg-stand-tls/postgres.crt" in sql
    assert "password=" not in sql


def test_entrypoint_enforces_certificate_only_tcp_hba() -> None:
    script = (ASSET_ROOT / "docker" / "pg-stand-entrypoint.sh").read_text(encoding="utf-8")

    assert "hostnossl all all 0.0.0.0/0 reject" in script
    assert "hostssl all all 0.0.0.0/0 cert map=pg_stand_tls" in script
    assert "hostssl replication %s 0.0.0.0/0 cert map=pg_stand_tls" in script
    assert "install -m 0600 -o postgres -g postgres" in script
    assert "pg-stand-pgbouncer wait-and-start" in script


def test_pgbouncer_has_separate_pool_modes_and_certificate_only_tls() -> None:
    script = (ASSET_ROOT / "docker" / "pg-stand-pgbouncer.sh").read_text(encoding="utf-8")

    assert "SESSION_PORT=6432" in script
    assert "TRANSACTION_PORT=6433" in script
    assert 'write_config session "$SESSION_PORT"' in script
    assert 'write_config transaction "$TRANSACTION_PORT"' in script
    assert "auth_type = scram-sha-256" in script
    assert "auth_type = hba" in script
    assert "client_tls_sslmode = verify-full" in script
    assert "hostnossl all all all reject" in script
    assert "hostssl all all all cert map=pg_stand_tls" in script


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-17 12:01:00+00", "2026-07-17T12:01:00+00:00"),
        ("2026-07-17 15:01:00+03", "2026-07-17T12:01:00+00:00"),
        ("2026-07-17 12:01:00 UTC", "2026-07-17T12:01:00+00:00"),
    ],
)
def test_log_timestamp_accepts_postgresql_timezone_formats(value: str, expected: str) -> None:
    parsed = StandManager._log_timestamp(value)

    assert parsed is not None
    assert parsed.isoformat() == expected


def test_log_records_merge_sort_and_filter() -> None:
    config = load_config(ASSET_ROOT / "configs" / "primary-standby-sync.yaml", environment={})
    manager = StandManager(config, client=object())
    rows = {
        "primary": [
            {
                "node": "primary",
                "log_time": "2026-07-17 12:01:00+00",
                "session_id": "p",
                "severity": "ERROR",
                "backend_type": "client backend",
                "message": "target failure",
                "detail": "",
                "hint": "",
                "context": "",
                "query": "select 1",
            }
        ],
        "standby": [
            {
                "node": "standby",
                "log_time": "2026-07-17 12:00:00+00",
                "session_id": "s",
                "severity": "WARNING",
                "backend_type": "startup",
                "message": "other",
                "detail": "",
                "hint": "",
                "context": "",
                "query": "",
            }
        ],
    }

    with patch.object(manager, "_csv_log_records", side_effect=lambda node: rows[node]):
        result = manager.log_records(
            "all",
            since=datetime(2026, 7, 17, 12, 0, 30, tzinfo=timezone.utc),
            severity="error",
            contains="target",
        )

    assert [item["node"] for item in result] == ["primary"]


def test_events_are_requested_only_for_the_current_managed_project() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    client = MagicMock()
    client.ping.return_value = True
    client.events.return_value = iter(
        [
            {
                "time": 1,
                "Type": "container",
                "Action": "start",
                "Actor": {
                    "ID": "abc",
                    "Attributes": {
                        MANAGED_LABEL: "true",
                        PROJECT_LABEL: config.metadata.name,
                        INSTANCE_LABEL: config.instance_id,
                        "io.pg-stand.node": "primary",
                        "name": config.primary.container_name,
                    },
                },
            }
        ]
    )
    manager = StandManager(config, client=client)

    result = list(manager.docker_events(since_epoch=0, until_epoch=2, count=1))

    assert result[0]["action"] == "start"
    assert client.events.call_args.kwargs["filters"]["label"] == [
        "io.pg-stand.managed=true",
        "io.pg-stand.project=pg-stand-single",
        f"io.pg-stand.instance={config.instance_id}",
    ]


def test_single_node_health_aggregates_exit_status() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    client = MagicMock()
    client.ping.return_value = True
    manager = StandManager(config, client=client)
    lock = _Resource(
        {
            "Labels": {
                PROJECT_LABEL: config.metadata.name,
                INSTANCE_LABEL: config.instance_id,
            }
        },
        name=ACTIVE_LOCK_NETWORK,
    )

    with (
        patch.object(manager, "_find_active_lock", return_value=lock),
        patch.object(
            manager, "_read_applied_state", return_value={"config_hash": config.config_hash}
        ),
        patch.object(
            manager,
            "_node_health_checks",
            return_value=[{"id": "node.primary", "status": "healthy", "summary": "ready"}],
        ),
    ):
        result = manager.health()

    assert result["status"] == "healthy"
    assert result["exit_code"] == 0


@pytest.mark.parametrize(
    ("version", "written_column", "flushed_column"),
    [(11, "received_lsn", "received_lsn"), (17, "written_lsn", "flushed_lsn")],
)
def test_wal_receiver_query_is_version_aware(
    version: int,
    written_column: str,
    flushed_column: str,
) -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "primary-standby-sync.yaml",
        environment={},
        postgres_version=version,
    )
    manager = StandManager(config, client=object())
    output = "streaming||0/1|0/2|0/2|0/2||||0"

    with patch.object(manager, "_psql", return_value=output) as psql:
        rows = manager._wal_receiver_details(object())

    query = psql.call_args.args[1]
    assert f"coalesce({written_column}::text" in query
    assert f"coalesce({flushed_column}::text" in query
    assert rows[0]["written_lsn"] == "0/2"
    assert rows[0]["flushed_lsn"] == "0/2"


def test_physical_replication_details_exclude_logical_wal_senders() -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "primary-standby-sync-logical.yaml",
        environment={},
    )
    manager = StandManager(config, client=object())

    with patch.object(manager, "_psql", return_value="") as psql:
        assert manager._physical_replication_details(object()) == []

    query = psql.call_args.args[1]
    assert config.topology.replication is not None
    assert f"where application_name = '{config.topology.replication.application_name}'" in query


def test_managed_image_inventory_requires_labels_and_tag_suffix() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    current = SimpleNamespace(
        id="sha256:current",
        short_id="sha256:current",
        tags=["pg-stand/postgres:17-pg-stand-managed"],
        attrs={
            "Size": 100,
            "Config": {
                "Labels": {
                    "io.pg-stand.managed": "true",
                    "io.pg-stand.resource-kind": "managed-image",
                    "io.pg-stand.image-schema": "5",
                    "io.pg-stand.postgres-major": "17",
                }
            },
        },
    )
    unrelated = SimpleNamespace(
        id="sha256:unrelated",
        short_id="sha256:unrelated",
        tags=["unrelated/postgres:17"],
        attrs=current.attrs,
    )
    previous_schema = SimpleNamespace(
        id="sha256:previous",
        short_id="sha256:previous",
        tags=["pg-stand/postgres:16-pg-stand-managed"],
        attrs={
            "Size": 75,
            "Config": {
                "Labels": {
                    "io.pg-stand.managed": "true",
                    "io.pg-stand.resource-kind": "managed-image",
                    "io.pg-stand.image-schema": "4",
                    "io.pg-stand.postgres-major": "16",
                }
            },
        },
    )
    legacy = SimpleNamespace(
        id="sha256:legacy",
        short_id="sha256:legacy",
        tags=["pg-stand/postgres:10"],
        attrs={
            "Size": 50,
            "Config": {
                "Labels": {
                    "io.pg-stand.image-schema": "2",
                    "io.pg-stand.postgres-major": "10",
                }
            },
        },
    )
    client = MagicMock()
    client.ping.return_value = True
    client.images.list.side_effect = [[current, previous_schema, unrelated], [legacy]]
    manager = StandManager(config, client=client)

    result = manager.image_list()

    assert [item["id"] for item in result["images"]] == [
        "sha256:legacy",
        "sha256:previous",
        "sha256:current",
    ]
    assert result["aggregate_size_bytes_including_shared_layers"] == 225
    assert result["images"][0]["legacy_without_suffix"] is True


def test_cleanup_preflights_shared_credentials_before_mutating_resources() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    blocker = MagicMock()
    blocker.name = "another-primary-pg-stand-managed"
    blocker.attrs = {
        "Config": {
            "Labels": {
                MANAGED_LABEL: "true",
                PROJECT_LABEL: "another-project",
                INSTANCE_LABEL: config.instance_id,
                RESOURCE_KIND_LABEL: "stand-node",
            }
        }
    }
    client = MagicMock()
    client.ping.return_value = True
    client.containers.list.return_value = [blocker]
    manager = StandManager(config, client=client)

    with (
        patch.object(manager, "down") as down,
        patch.object(manager, "storage_clean") as storage_clean,
        pytest.raises(DockerRuntimeError, match="another managed stand node"),
    ):
        manager.cleanup(
            remove_containers=True,
            remove_storage=True,
            remove_images=False,
            remove_cache=False,
            remove_credentials=True,
        )

    down.assert_not_called()
    storage_clean.assert_not_called()


def test_cleanup_retains_active_lock_until_credentials_are_removed() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    own = MagicMock()
    own.name = config.primary.container_name
    own.attrs = {
        "Config": {
            "Labels": {
                MANAGED_LABEL: "true",
                PROJECT_LABEL: config.metadata.name,
                INSTANCE_LABEL: config.instance_id,
                RESOURCE_KIND_LABEL: "stand-node",
            }
        }
    }
    client = MagicMock()
    client.ping.return_value = True
    client.containers.list.side_effect = [[own], []]
    manager = StandManager(config, client=client)

    with (
        patch.object(manager, "_acquire_active_lock") as acquire_lock,
        patch.object(manager, "_release_active_lock") as release_lock,
        patch.object(manager, "_has_running_owned_nodes", return_value=False),
        patch.object(manager, "down") as down,
        patch("pg_stand.runtime_docker.clean_credentials", return_value=True),
    ):
        result = manager.cleanup(
            remove_containers=True,
            remove_storage=False,
            remove_images=False,
            remove_cache=False,
            remove_credentials=True,
        )

    acquire_lock.assert_called_once_with()
    down.assert_called_once_with(
        clear_data=False,
        timeout_seconds=30,
        release_lock=False,
    )
    release_lock.assert_called_once_with()
    assert result["credentials_removed"] is True


def test_down_reserves_active_lock_before_clearing_storage() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    client = MagicMock()
    client.ping.return_value = True
    manager = StandManager(config, client=client)

    with (
        patch.object(manager, "_acquire_active_lock") as acquire_lock,
        patch.object(manager, "_remove_stale_helper"),
        patch.object(manager, "_configured_nodes", return_value=[]),
        patch.object(manager, "_find_owned_network", return_value=None),
        patch.object(manager, "storage_clean") as storage_clean,
        patch.object(manager, "_release_active_lock") as release_lock,
    ):
        manager.down(clear_data=True)

    acquire_lock.assert_called_once_with()
    storage_clean.assert_called_once_with(require_absent_containers=True)
    release_lock.assert_called_once_with()


def test_unknown_log_severity_fails_before_reading_files() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    manager = StandManager(config, client=object())

    with (
        patch.object(manager, "_csv_log_records") as read_logs,
        pytest.raises(DockerRuntimeError, match="unknown PostgreSQL severity"),
    ):
        manager.log_records("primary", severity="typo")

    read_logs.assert_not_called()


def test_cleanup_requires_an_explicit_scope() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    client = MagicMock()
    client.ping.return_value = True
    manager = StandManager(config, client=client)

    with pytest.raises(DockerRuntimeError, match="at least one cleanup scope"):
        manager.cleanup(
            remove_containers=False,
            remove_storage=False,
            remove_images=False,
            remove_cache=False,
        )


def test_read_only_storage_report_does_not_build_a_missing_image(tmp_path: Path) -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "single.yaml",
        environment={},
        project_directory=tmp_path,
    )
    client = MagicMock()
    client.ping.return_value = True
    client.images.get.side_effect = ImageNotFound("missing")
    manager = StandManager(config, client=client)

    result = manager.storage_usage(ensure_image=False)

    assert "measurement_error" in result
    assert result["total_bytes"] == 0
    client.images.build.assert_not_called()


def test_cleanup_storage_does_not_build_a_missing_image(tmp_path: Path) -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "single.yaml",
        environment={},
        project_directory=tmp_path,
    )
    client = MagicMock()
    client.ping.return_value = True
    manager = StandManager(config, client=client)

    with (
        patch.object(manager, "_acquire_active_lock"),
        patch.object(manager, "_release_active_lock"),
        patch.object(manager, "_has_running_owned_nodes", return_value=False),
        patch.object(manager, "storage_clean") as storage_clean,
        patch.object(manager, "storage_usage", return_value={"total_bytes": 0}) as usage,
    ):
        result = manager.cleanup(
            remove_containers=False,
            remove_storage=True,
            remove_images=False,
            remove_cache=False,
        )

    usage.assert_called_once_with(ensure_image=False)
    storage_clean.assert_called_once_with(require_absent_containers=True)
    assert result["storage_removed"] is True


def test_cleanup_status_uses_sized_api_with_project_filters() -> None:
    config = load_config(ASSET_ROOT / "configs" / "single.yaml", environment={})
    client = MagicMock()
    client.ping.return_value = True
    client.api.containers.return_value = [
        {
            "Names": [f"/{config.primary.container_name}"],
            "Labels": {
                MANAGED_LABEL: "true",
                PROJECT_LABEL: config.metadata.name,
                INSTANCE_LABEL: config.instance_id,
                RESOURCE_KIND_LABEL: "stand-node",
                "io.pg-stand.node": "primary",
            },
            "State": "running",
            "SizeRw": 123,
            "SizeRootFs": 456,
            "Image": config.postgres.image_name,
        }
    ]
    manager = StandManager(config, client=client)

    with (
        patch.object(manager, "storage_usage", return_value={"total_bytes": 10}),
        patch.object(manager, "image_list", return_value={"images": []}),
    ):
        result = manager.cleanup_status()

    assert result["container_writable_layers_bytes"] == 123
    client.api.containers.assert_called_once_with(
        all=True,
        size=True,
        filters={
            "label": [
                "io.pg-stand.managed=true",
                "io.pg-stand.resource-kind=stand-node",
                "io.pg-stand.project=pg-stand-single",
                f"io.pg-stand.instance={config.instance_id}",
            ]
        },
    )


def test_managed_dockerfiles_carry_cleanup_ownership_labels() -> None:
    for filename in (
        "Dockerfile.pg10",
        "Dockerfile.pg11-pg12",
        "Dockerfile.pg13-pg17",
        "Dockerfile.pg18",
    ):
        text = (ASSET_ROOT / "docker" / filename).read_text(encoding="utf-8")
        assert 'io.pg-stand.image-schema="6"' in text
        assert 'io.pg-stand.pgbouncer-version="${PGBOUNCER_VERSION}"' in text
        assert "ARG PGBOUNCER_VERSION=1.25.2" in text
        assert '"pgbouncer=${pgbouncer_package_version}"' in text
        assert "pg_stand_test.pub" not in text
        assert 'io.pg-stand.managed="true"' in text
        assert 'io.pg-stand.resource-kind="managed-image"' in text
