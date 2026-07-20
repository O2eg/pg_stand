"""Stable machine interface for deterministic pg_play orchestration."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pg_stand import __version__

CONTRACT_VERSION = "pg_play/component/v1"
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
        "contract_version": CONTRACT_VERSION,
        "component": COMPONENT,
        "component_version": __version__,
        "commands": {
            "validate": {"mutates_target": False, "machine_output": True},
            "show": {"mutates_target": False, "machine_output": True},
            "plan": {"mutates_target": False, "machine_output": True},
            "status": {"mutates_target": False, "machine_output": True},
            "health": {"mutates_target": False, "machine_output": True},
            "up": {"mutates_target": True, "machine_output": True},
            "apply": {
                "mutates_target": True,
                "machine_output": True,
                "accepts_plan_hash": True,
            },
            "stop": {"mutates_target": True, "machine_output": True},
            "down": {"mutates_target": True, "machine_output": True},
        },
        "config_schemas": ["pg_stand/v1"],
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
