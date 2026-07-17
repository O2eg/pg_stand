"""Generated PostgreSQL accounts for local pg-stand clusters."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from pg_stand.errors import DockerRuntimeError

DATABASE_CREDENTIALS_DIRECTORY = "database"
DATABASE_CREDENTIALS_FILE = "passwords.json"
DATABASE_CREDENTIALS_VERSION = 1
DEFAULT_APPLICATION_DATABASE = "app_01"
DEFAULT_REPLICATION_USER = "replicator"
APPLICATION_DATABASES = tuple(f"app_{index:02d}" for index in range(1, 11))
APPLICATION_OWNERS = tuple(f"user_app_{index:02d}" for index in range(1, 11))
APPLICATION_ACCOUNTS = tuple(zip(APPLICATION_DATABASES, APPLICATION_OWNERS, strict=True))


@dataclass(frozen=True)
class ApplicationCredential:
    database: str
    owner: str
    password: str


@dataclass(frozen=True)
class DatabaseCredentials:
    superuser: str
    superuser_password: str
    replication_user: str
    replication_password: str
    applications: tuple[ApplicationCredential, ...]


def _new_password(used: set[str]) -> str:
    while True:
        password = secrets.token_urlsafe(32)
        if password not in used:
            used.add(password)
            return password


def generate_database_credentials(
    directory: Path,
    *,
    superuser: str,
    replication_user: str,
) -> None:
    used: set[str] = set()
    document = {
        "version": DATABASE_CREDENTIALS_VERSION,
        "superuser": {
            "username": superuser,
            "password": _new_password(used),
        },
        "replication": {
            "username": replication_user,
            "password": _new_password(used),
        },
        "applications": [
            {
                "database": database,
                "owner": owner,
                "password": _new_password(used),
            }
            for database, owner in APPLICATION_ACCOUNTS
        ],
    }
    path = directory / DATABASE_CREDENTIALS_FILE
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DockerRuntimeError(f"database credential field {label} must be a non-empty string")
    return value


def read_database_credentials(
    directory: Path,
    *,
    expected_superuser: str | None = None,
    expected_replication_user: str | None = None,
) -> DatabaseCredentials:
    path = directory / DATABASE_CREDENTIALS_FILE
    if path.is_symlink() or not path.is_file():
        raise DockerRuntimeError(f"database credential file is missing or invalid: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DockerRuntimeError(f"cannot read database credential file {path}: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {
        "version",
        "superuser",
        "replication",
        "applications",
    }:
        raise DockerRuntimeError(f"database credential file has an invalid structure: {path}")
    if document["version"] != DATABASE_CREDENTIALS_VERSION:
        raise DockerRuntimeError(
            f"database credential file has unsupported version: {document['version']}"
        )
    superuser = document["superuser"]
    replication = document["replication"]
    applications = document["applications"]
    if not isinstance(superuser, dict) or set(superuser) != {"username", "password"}:
        raise DockerRuntimeError("database credential superuser entry is invalid")
    if not isinstance(replication, dict) or set(replication) != {"username", "password"}:
        raise DockerRuntimeError("database credential replication entry is invalid")
    if not isinstance(applications, list) or len(applications) != len(APPLICATION_DATABASES):
        raise DockerRuntimeError("database credential application list is invalid")

    superuser_name = _required_text(superuser["username"], "superuser.username")
    superuser_password = _required_text(superuser["password"], "superuser.password")
    replication_name = _required_text(replication["username"], "replication.username")
    replication_password = _required_text(replication["password"], "replication.password")
    accounts: list[ApplicationCredential] = []
    for index, entry in enumerate(applications):
        if not isinstance(entry, dict) or set(entry) != {"database", "owner", "password"}:
            raise DockerRuntimeError(f"database credential application entry {index} is invalid")
        accounts.append(
            ApplicationCredential(
                database=_required_text(entry["database"], f"applications[{index}].database"),
                owner=_required_text(entry["owner"], f"applications[{index}].owner"),
                password=_required_text(entry["password"], f"applications[{index}].password"),
            )
        )
    if tuple(account.database for account in accounts) != APPLICATION_DATABASES:
        raise DockerRuntimeError("database credential application database list is unexpected")
    if tuple(account.owner for account in accounts) != APPLICATION_OWNERS:
        raise DockerRuntimeError("database credential application owner list is unexpected")
    passwords = [superuser_password, replication_password]
    passwords.extend(account.password for account in accounts)
    if any(len(password) < 32 for password in passwords) or len(set(passwords)) != len(passwords):
        raise DockerRuntimeError("database credentials must contain distinct strong passwords")
    if expected_superuser is not None and superuser_name != expected_superuser:
        raise DockerRuntimeError(
            f"generated database credentials belong to superuser {superuser_name}, "
            f"not {expected_superuser}; remove credentials only after stopping the stand"
        )
    if expected_replication_user is not None and replication_name != expected_replication_user:
        raise DockerRuntimeError(
            f"generated database credentials belong to replication user {replication_name}, "
            f"not {expected_replication_user}; remove credentials only after stopping the stand"
        )
    return DatabaseCredentials(
        superuser=superuser_name,
        superuser_password=superuser_password,
        replication_user=replication_name,
        replication_password=replication_password,
        applications=tuple(accounts),
    )


def database_credentials_summary(directory: Path) -> dict[str, object]:
    credentials = read_database_credentials(directory)
    return {
        "superuser": credentials.superuser,
        "replication_user": credentials.replication_user,
        "applications": [
            {"database": account.database, "owner": account.owner}
            for account in credentials.applications
        ],
    }
