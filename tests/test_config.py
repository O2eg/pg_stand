from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from pg_stand.config import load_config
from pg_stand.errors import ConfigError

ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = ROOT / "src" / "pg_stand"


@pytest.mark.parametrize(
    ("filename", "topology", "replication_mode", "has_standby", "has_logical"),
    [
        ("single.yaml", "single", None, False, False),
        ("primary-standby-async.yaml", "primary_standby", "async", True, False),
        ("primary-standby-sync.yaml", "primary_standby", "sync", True, False),
        (
            "primary-standby-sync-logical.yaml",
            "primary_standby_logical",
            "sync",
            True,
            True,
        ),
    ],
)
def test_example_configs_are_valid(
    filename: str,
    topology: str,
    replication_mode: str | None,
    has_standby: bool,
    has_logical: bool,
) -> None:
    config = load_config(ASSET_ROOT / "configs" / filename, environment={})

    assert config.topology.mode == topology
    assert (config.standby is not None) is has_standby
    assert (config.logical is not None) is has_logical
    actual_mode = config.topology.replication.mode if config.topology.replication else None
    assert actual_mode == replication_mode
    assert config.primary.ssh_published_port != config.primary.published_port
    assert config.primary.pgbouncer_session_published_port != config.primary.published_port
    assert config.primary.pgbouncer_transaction_published_port != config.primary.published_port
    assert config.primary.cpu_limit == 1.0
    assert config.primary.memory_limit == "1g"
    assert config.primary.container_name.endswith("-pg-stand-managed")
    assert config.docker.network_name.endswith("-pg-stand-managed")


@pytest.mark.parametrize("filename", ["single.yaml", "primary-standby-sync-logical.yaml"])
def test_tls_override_enables_certificate_mode_for_any_topology(filename: str) -> None:
    config = load_config(ASSET_ROOT / "configs" / filename, environment={}, tls_enabled=True)

    assert config.postgres.tls.enabled is True


def test_tls_defaults_to_disabled_when_block_is_absent(tmp_path: Path) -> None:
    def change(document: dict) -> None:
        document["spec"]["postgres"].pop("tls")

    path = _changed_config(tmp_path, "single.yaml", change)

    assert load_config(path, environment={}).postgres.tls.enabled is False


def test_rejects_non_boolean_tls_flag(tmp_path: Path) -> None:
    path = _changed_config(
        tmp_path,
        "single.yaml",
        lambda document: document["spec"]["postgres"]["tls"].update(enabled="true"),
    )

    with pytest.raises(ConfigError, match="must be true or false"):
        load_config(path, environment={})


def test_rejects_null_string_fields_instead_of_coercing_them(tmp_path: Path) -> None:
    path = _changed_config(
        tmp_path,
        "single.yaml",
        lambda document: document["spec"]["postgres"].update(superuser=None),
    )

    with pytest.raises(ConfigError, match="superuser must be a string"):
        load_config(path, environment={})


def test_repository_config_contains_no_passwords_and_version_override_is_resolved() -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "primary-standby-sync.yaml",
        environment={},
        postgres_version=18,
    )

    assert config.postgres.version == 18
    assert config.postgres.image_name == "pg-stand/postgres:18-pg-stand-managed"
    assert config.postgres.base_image_name == "postgres:18"
    assert config.postgres.database == "app_01"
    assert config.topology.replication is not None
    public = config.public_document()
    assert "password" not in json.dumps(public)


def _changed_config(tmp_path: Path, source_name: str, change) -> Path:
    document = yaml.safe_load((ASSET_ROOT / "configs" / source_name).read_text(encoding="utf-8"))
    change(document)
    target = tmp_path / source_name
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


def test_pg10_uses_explicit_legacy_base_image() -> None:
    config = load_config(
        ASSET_ROOT / "configs" / "single.yaml",
        environment={},
        postgres_version=10,
    )

    assert config.postgres.image_name == "pg-stand/postgres:10-pg-stand-managed"
    assert config.postgres.base_image_name == "postgres:10-bullseye"


def test_environment_interpolation_remains_available_for_custom_configs(tmp_path: Path) -> None:
    path = _changed_config(
        tmp_path,
        "single.yaml",
        lambda document: document["spec"]["postgres"].update(base_image="${CUSTOM_BASE_IMAGE}"),
    )

    config = load_config(
        path,
        environment={"CUSTOM_BASE_IMAGE": "registry.example/postgres:17"},
    )

    assert config.postgres.base_image_name == "registry.example/postgres:17"


def test_passwords_cannot_be_declared_in_yaml(tmp_path: Path) -> None:
    postgres_path = _changed_config(
        tmp_path,
        "single.yaml",
        lambda document: document["spec"]["postgres"].update(superuser_password="secret"),
    )
    with pytest.raises(ConfigError, match="unknown spec.postgres key.*superuser_password"):
        load_config(postgres_path, environment={})

    replication_path = _changed_config(
        tmp_path,
        "primary-standby-sync.yaml",
        lambda document: document["spec"]["topology"]["replication"].update(password="secret"),
    )
    with pytest.raises(ConfigError, match="unknown spec.topology.replication key.*password"):
        load_config(replication_path, environment={})


def test_rejects_non_boolean_replication_flag(tmp_path: Path) -> None:
    path = _changed_config(
        tmp_path,
        "primary-standby-async.yaml",
        lambda document: document["spec"]["topology"]["replication"].update(
            hot_standby_feedback="false"
        ),
    )

    with pytest.raises(ConfigError, match="must be true or false"):
        load_config(path, environment={})


def test_rejects_non_mapping_parameters(tmp_path: Path) -> None:
    path = _changed_config(
        tmp_path,
        "single.yaml",
        lambda document: document["spec"]["postgres"].update(parameters=["max_connections"]),
    )

    with pytest.raises(ConfigError, match="parameters must be a mapping"):
        load_config(path, environment={})


def test_rejects_overlapping_wildcard_ports(tmp_path: Path) -> None:
    def change(document: dict) -> None:
        primary = document["spec"]["nodes"]["primary"]
        standby = document["spec"]["nodes"]["standby"]
        primary["bind_address"] = "0.0.0.0"
        standby["published_port"] = primary["published_port"]

    path = _changed_config(tmp_path, "primary-standby-async.yaml", change)

    with pytest.raises(ConfigError, match="must not publish the same"):
        load_config(path, environment={})


def test_rejects_cross_protocol_port_collisions(tmp_path: Path) -> None:
    def change(document: dict) -> None:
        primary = document["spec"]["nodes"]["primary"]
        standby = document["spec"]["nodes"]["standby"]
        standby["ssh_published_port"] = primary["published_port"]

    path = _changed_config(tmp_path, "primary-standby-async.yaml", change)

    with pytest.raises(ConfigError, match="must not publish the same"):
        load_config(path, environment={})


def test_rejects_same_database_and_ssh_port(tmp_path: Path) -> None:
    def change(document: dict) -> None:
        primary = document["spec"]["nodes"]["primary"]
        primary["ssh_published_port"] = primary["published_port"]

    path = _changed_config(tmp_path, "single.yaml", change)

    with pytest.raises(ConfigError, match="different PostgreSQL, SSH, and PgBouncer ports"):
        load_config(path, environment={})


def test_rejects_same_pgbouncer_session_and_transaction_port(tmp_path: Path) -> None:
    def change(document: dict) -> None:
        primary = document["spec"]["nodes"]["primary"]
        primary["pgbouncer_transaction_published_port"] = primary[
            "pgbouncer_session_published_port"
        ]

    path = _changed_config(tmp_path, "single.yaml", change)

    with pytest.raises(ConfigError, match="different PostgreSQL, SSH, and PgBouncer ports"):
        load_config(path, environment={})


def test_rejects_container_name_without_managed_suffix(tmp_path: Path) -> None:
    path = _changed_config(
        tmp_path,
        "single.yaml",
        lambda document: document["spec"]["nodes"]["primary"].update(
            container_name="unrelated-postgres"
        ),
    )

    with pytest.raises(ConfigError, match="must end with -pg-stand-managed"):
        load_config(path, environment={})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("cpu_limit", 0, "positive number"),
        ("cpu_limit", float("nan"), "positive number"),
        ("cpu_limit", float("inf"), "positive number"),
        ("cpu_limit", "1.0", "positive number"),
        ("memory_limit", "one gigabyte", "Docker byte value"),
        ("shm_size", "half-memory", "Docker byte value"),
    ],
)
def test_rejects_invalid_node_resource_limits(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    path = _changed_config(
        tmp_path,
        "single.yaml",
        lambda document: document["spec"]["nodes"]["primary"].update({field: value}),
    )

    with pytest.raises(ConfigError, match=message):
        load_config(path, environment={})


def test_rejects_fractional_integer_fields(tmp_path: Path) -> None:
    path = _changed_config(
        tmp_path,
        "single.yaml",
        lambda document: document["spec"]["postgres"].update(version=17.5),
    )

    with pytest.raises(ConfigError, match="version must be an integer"):
        load_config(path, environment={})


def test_rejects_non_ip_bind_address(tmp_path: Path) -> None:
    path = _changed_config(
        tmp_path,
        "single.yaml",
        lambda document: document["spec"]["nodes"]["primary"].update(bind_address="localhost"),
    )

    with pytest.raises(ConfigError, match="must be an IPv4 or IPv6 address"):
        load_config(path, environment={})


def test_rejects_unknown_keys(tmp_path: Path) -> None:
    path = _changed_config(
        tmp_path,
        "single.yaml",
        lambda document: document["spec"].update(typo=True),
    )

    with pytest.raises(ConfigError, match="unknown spec key"):
        load_config(path, environment={})


def test_rejects_storage_outside_project(tmp_path: Path) -> None:
    path = _changed_config(
        tmp_path,
        "single.yaml",
        lambda document: document["spec"]["storage"].update(root_directory="../outside"),
    )

    with pytest.raises(ConfigError, match="relative subdirectory"):
        load_config(path, environment={}, project_directory=tmp_path)


def test_rejects_managed_storage_parameters(tmp_path: Path) -> None:
    path = _changed_config(
        tmp_path,
        "single.yaml",
        lambda document: document["spec"]["postgres"]["parameters"].update(
            log_directory="somewhere"
        ),
    )

    with pytest.raises(ConfigError, match="managed by pg-stand"):
        load_config(path, environment={})


@pytest.mark.parametrize("name", ["ssl", "ssl_ca_file", "ssl_cert_file", "ssl_key_file"])
def test_rejects_tls_parameters_managed_by_certificate_mode(tmp_path: Path, name: str) -> None:
    path = _changed_config(
        tmp_path,
        "single.yaml",
        lambda document: document["spec"]["postgres"]["parameters"].update({name: "on"}),
    )

    with pytest.raises(ConfigError, match="managed by pg-stand"):
        load_config(path, environment={})


def test_allows_diagnostic_and_logging_threshold_overrides(tmp_path: Path) -> None:
    path = _changed_config(
        tmp_path,
        "single.yaml",
        lambda document: document["spec"]["postgres"]["parameters"].update(
            {
                "log_min_duration_statement": "250ms",
                "auto_explain.log_min_duration": "50ms",
                "pg_stat_statements.max": "10000",
                "track_io_timing": "off",
            }
        ),
    )

    config = load_config(path, environment={})

    assert config.postgres.parameters["log_min_duration_statement"] == "250ms"
    assert config.postgres.parameters["auto_explain.log_min_duration"] == "50ms"


def test_rejects_managed_initdb_paths(tmp_path: Path) -> None:
    path = _changed_config(
        tmp_path,
        "single.yaml",
        lambda document: document["spec"]["postgres"].update(
            initdb_args="--data-checksums --waldir=/tmp/wal"
        ),
    )

    with pytest.raises(ConfigError, match="initdb option --waldir is managed"):
        load_config(path, environment={})


def test_json_schema_is_valid_json() -> None:
    document = json.loads(
        (ASSET_ROOT / "schema" / "pg_stand-v1.schema.json").read_text(encoding="utf-8")
    )

    assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"
