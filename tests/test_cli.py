from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pg_stand.cli import _moment, _parser, _postgres_parameters_override, main

ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = ROOT / "src" / "pg_stand"


def test_validate_does_not_require_docker(capsys) -> None:
    result = main(
        [
            "--config",
            str(ASSET_ROOT / "configs" / "single.yaml"),
            "--json",
            "validate",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert '"valid": true' in captured.out
    assert captured.err == ""


def test_init_materializes_bundled_profiles_without_a_config(tmp_path: Path, capsys) -> None:
    result = main(["--json", "init", "--directory", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 0
    assert '"state": "created"' in captured.out
    assert (tmp_path / "configs" / "single.yaml").is_file()
    assert (tmp_path / "schema" / "pg_stand-v1.schema.json").is_file()
    assert captured.err == ""


def test_tls_flag_overrides_the_profile_without_a_duplicate_config(capsys) -> None:
    result = main(
        [
            "--config",
            str(ASSET_ROOT / "configs" / "single.yaml"),
            "--tls",
            "--json",
            "validate",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert '"tls": true' in captured.out


def test_show_contains_no_database_secrets(capsys) -> None:
    result = main(["--config", str(ASSET_ROOT / "configs" / "primary-standby-sync.yaml"), "show"])

    captured = capsys.readouterr()
    assert result == 0
    assert "password:" not in captured.out
    assert "superuser: postgres" in captured.out


def test_invalid_config_returns_operator_error(tmp_path: Path, capsys) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("kind: wrong\n", encoding="utf-8")

    result = main(["--config", str(path), "validate"])

    captured = capsys.readouterr()
    assert result == 2
    assert "ERROR:" in captured.err


def test_recreate_clear_data_is_an_explicit_flag() -> None:
    args = _parser().parse_args(
        ["--config", str(ASSET_ROOT / "configs" / "single.yaml"), "recreate", "--clear-data"]
    )

    assert args.command == "recreate"
    assert args.clear_data is True


def test_storage_subcommands_are_declarative() -> None:
    args = _parser().parse_args(
        ["--config", str(ASSET_ROOT / "configs" / "single.yaml"), "storage", "status"]
    )

    assert args.command == "storage"
    assert args.storage_command == "status"


def test_logical_full_sync_is_an_explicit_flag() -> None:
    args = _parser().parse_args(
        [
            "--config",
            str(ASSET_ROOT / "configs" / "primary-standby-sync-logical.yaml"),
            "logical",
            "sync",
            "--full",
        ]
    )

    assert args.command == "logical"
    assert args.logical_command == "sync"
    assert args.full is True


def test_resources_and_apply_modes_are_explicit() -> None:
    resources = _parser().parse_args(
        ["--config", str(ASSET_ROOT / "configs" / "single.yaml"), "resources", "--node", "primary"]
    )
    reload_config = _parser().parse_args(
        ["--config", str(ASSET_ROOT / "configs" / "single.yaml"), "apply", "--reload"]
    )
    restart_config = _parser().parse_args(
        ["--config", str(ASSET_ROOT / "configs" / "single.yaml"), "apply", "--restart"]
    )

    assert resources.command == "resources"
    assert resources.node == "primary"
    assert reload_config.reload is True
    assert reload_config.restart is False
    assert restart_config.reload is False
    assert restart_config.restart is True


def test_active_and_capabilities_are_configless_commands(capsys) -> None:
    with patch(
        "pg_stand.cli.discover_active_stand",
        return_value={"active": True, "project": "pg-stand-single", "nodes": {}},
    ):
        assert main(["--json", "active"]) == 0

    assert '"project": "pg-stand-single"' in capsys.readouterr().out
    assert main(["status"]) == 2
    assert "--config is required for status" in capsys.readouterr().err

    assert main(["--json", "capabilities"]) == 0
    capabilities = json.loads(capsys.readouterr().out)
    assert capabilities["contract_version"] == "pg_play/component/v1"


def test_machine_capabilities_use_versioned_envelope(capsys) -> None:
    result = main(
        [
            "--machine",
            "--request-id",
            "stand-capabilities",
            "capabilities",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["component"] == "pg_stand"
    assert payload["request_id"] == "stand-capabilities"
    assert payload["status"] == "succeeded"
    assert payload["result"]["contract_version"] == "pg_play/component/v1"


def test_machine_mode_refuses_password_bearing_dsn(capsys) -> None:
    result = main(
        [
            "--machine",
            "--request-id",
            "stand-secret-check",
            "--config",
            str(ASSET_ROOT / "configs" / "single.yaml"),
            "dsn",
            "--show-password",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 4
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "unsupported"


def test_configurator_artifact_parameters_are_decoded(tmp_path: Path) -> None:
    artifact = tmp_path / "candidate.json"
    artifact.write_text(
        json.dumps(
            {
                "schema_version": "pg_configurator/v1",
                "postgresql_conf": {
                    "log_statement": "'ddl'",
                    "synchronous_standby_names": "''",
                    "log_line_prefix": "'%m user=%u O''Reilly '",
                    "work_mem": "16MB",
                },
            }
        ),
        encoding="utf-8",
    )

    assert _postgres_parameters_override(str(artifact)) == {
        "log_statement": "ddl",
        "synchronous_standby_names": "",
        "log_line_prefix": "%m user=%u O'Reilly ",
        "work_mem": "16MB",
    }


def test_new_observation_and_management_command_shapes() -> None:
    config = str(ASSET_ROOT / "configs" / "primary-standby-sync-logical.yaml")
    parser = _parser()

    assert parser.parse_args(["-c", config, "health"]).command == "health"
    assert parser.parse_args(["-c", config, "wait"]).command == "wait"
    assert parser.parse_args(["-c", config, "plan"]).command == "plan"
    assert parser.parse_args(["-c", config, "apply", "--dry-run"]).dry_run is True
    assert parser.parse_args(["-c", config, "replication", "watch", "--count", "2"]).count == 2
    assert parser.parse_args(["-c", config, "storage", "usage"]).storage_command == "usage"
    assert parser.parse_args(["-c", config, "node", "inspect", "logical"]).node == "logical"
    assert parser.parse_args(["-c", config, "standby", "reinit", "--force"]).force is True
    assert parser.parse_args(["-c", config, "logs", "all"]).node == "all"
    assert parser.parse_args(["-c", config, "image", "inspect"]).image_command == "inspect"
    cleanup = parser.parse_args(["-c", config, "cleanup", "run", "--all", "--force"])
    assert cleanup.cleanup_command == "run"
    assert cleanup.all is True
    assert cleanup.force is True


def test_relative_and_iso_times_are_supported() -> None:
    from datetime import datetime, timezone

    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)

    assert _moment("30m", now=now) == datetime(2026, 7, 17, 11, 30, tzinfo=timezone.utc)
    assert _moment("2026-07-17T14:00:00+02:00") == now


@pytest.mark.parametrize(
    "arguments",
    [
        ["up", "--timeout", "-1"],
        ["up", "--timeout", "nan"],
        ["health", "--max-physical-lag-seconds", "-1"],
        ["health", "--max-logical-lag-seconds", "inf"],
        ["down", "--timeout", "-1"],
        ["logs", "primary", "--severity", "typo"],
    ],
)
def test_cli_rejects_invalid_time_and_threshold_values(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["--config", str(ASSET_ROOT / "configs" / "single.yaml"), *arguments])
