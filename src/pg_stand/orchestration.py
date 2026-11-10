"""Stable machine interface for deterministic pg_play orchestration."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pg_stand import __version__

CONTRACT_VERSION = "pg_play/component/v1"
CAPABILITY_SCHEMA_VERSION = "pg_play/capabilities/v1"
MACHINE_INTERFACE = {
    "machine_flag": "--machine",
    "request_id_option": "--request-id",
    "capabilities_option": "--component-capabilities",
}
COMPONENT = "pg_stand"

EXIT_CODES = {
    "success": 0,
    "validation_error": 2,
    "precondition_failed": 3,
    "unsupported": 4,
    "partial": 5,
    "execution_error": 6,
    "cancelled": 7,
    "ownership_error": 8,
}


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def static_capabilities() -> dict[str, Any]:
    return {
        "capability_schema_version": CAPABILITY_SCHEMA_VERSION,
        "machine_interface": MACHINE_INTERFACE,
        "contract_version": CONTRACT_VERSION,
        "component": COMPONENT,
        "component_version": __version__,
        "commands": {
            "list": {"mutates_target": False, "machine_output": True, "accepts_plan_hash": False},
            "active": {"mutates_target": False, "machine_output": True, "accepts_plan_hash": False},
            "capabilities": {
                "mutates_target": False,
                "machine_output": True,
                "accepts_plan_hash": False,
            },
            "validate": {
                "mutates_target": False,
                "machine_output": True,
                "accepts_plan_hash": False,
            },
            "show": {"mutates_target": False, "machine_output": True, "accepts_plan_hash": False},
            "plan": {"mutates_target": False, "machine_output": True, "accepts_plan_hash": False},
            "status": {"mutates_target": False, "machine_output": True, "accepts_plan_hash": False},
            "health": {"mutates_target": False, "machine_output": True, "accepts_plan_hash": False},
            "up": {"mutates_target": True, "machine_output": True, "accepts_plan_hash": False},
            "apply": {
                "mutates_target": True,
                "machine_output": True,
                "accepts_plan_hash": True,
            },
            "stop": {"mutates_target": True, "machine_output": True, "accepts_plan_hash": False},
            "down": {"mutates_target": True, "machine_output": True, "accepts_plan_hash": False},
        },
        "config_schemas": ["pg_stand/v1"],
        "stand_isolation": {
            "multiple_projects": True,
            "active_stands_per_project": 1,
            "project_option": "--project",
            "explicit_names_and_ports": True,
            "external_networks": True,
            "standalone_logical_decoding": True,
        },
        "states": [
            "planned",
            "running",
            "succeeded",
            "partial",
            "failed",
            "cancelled",
            "skipped",
            "blocked",
        ],
        "exit_codes": EXIT_CODES,
        "machine_unsupported_commands": [
            "dsn --show-password",
            "events --follow",
            "logs --follow",
            "psql",
            "ssh",
            "watch",
        ],
        "secret_policy": {
            "machine_output_redacts_secrets": True,
            "dsn_password_requires_explicit_flag": True,
        },
    }


def envelope(
    command: str,
    status: str,
    *,
    request_id: str | None,
    result: Any = None,
    artifacts: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "component": COMPONENT,
        "component_version": __version__,
        "command": command,
        "request_id": request_id,
        "status": status,
        "result": result,
        "artifacts": artifacts or [],
        "warnings": warnings or [],
        "error": error,
    }
