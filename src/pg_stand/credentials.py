"""Generation and lifecycle of local pg-stand SSH and TLS credentials."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pg_stand.database_credentials import (
    DATABASE_CREDENTIALS_DIRECTORY,
    DATABASE_CREDENTIALS_FILE,
    DEFAULT_REPLICATION_USER,
    database_credentials_summary,
    generate_database_credentials,
    read_database_credentials,
)
from pg_stand.errors import DockerRuntimeError

CREDENTIALS_ROOT = Path(".pg_stand/credentials")
SSH_DIRECTORY = "ssh"
SSH_PRIVATE_NAME = "pg_stand_test"
SSH_REQUIRED_FILES = (SSH_PRIVATE_NAME, f"{SSH_PRIVATE_NAME}.pub")
TLS_DIRECTORY = "tls"
TLS_REQUIRED_FILES = (
    "ca.crt",
    "server.crt",
    "server.key",
    "postgres.crt",
    "postgres.key",
    "replicator.crt",
    "replicator.key",
)
DATABASE_REQUIRED_FILES = (DATABASE_CREDENTIALS_FILE,)
PRIVATE_FILES = {
    SSH_PRIVATE_NAME,
    "server.key",
    "postgres.key",
    "replicator.key",
    DATABASE_CREDENTIALS_FILE,
}


@dataclass(frozen=True)
class CredentialPaths:
    root: Path
    ssh: Path
    ssh_private_key: Path
    ssh_public_key: Path
    tls: Path
    database: Path
    database_passwords: Path


def credential_paths(project_directory: Path) -> CredentialPaths:
    project = project_directory.resolve()
    root = (project / CREDENTIALS_ROOT).resolve()
    if root == project or project not in root.parents:
        raise DockerRuntimeError("credential root escaped the project directory")
    ssh = root / SSH_DIRECTORY
    return CredentialPaths(
        root=root,
        ssh=ssh,
        ssh_private_key=ssh / SSH_PRIVATE_NAME,
        ssh_public_key=ssh / f"{SSH_PRIVATE_NAME}.pub",
        tls=root / TLS_DIRECTORY,
        database=root / DATABASE_CREDENTIALS_DIRECTORY,
        database_passwords=root / DATABASE_CREDENTIALS_DIRECTORY / DATABASE_CREDENTIALS_FILE,
    )


def _run(command: list[str]) -> str:
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise DockerRuntimeError(f"credential generator is not installed: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise DockerRuntimeError(f"credential command failed: {detail}") from exc
    return result.stdout.strip()


def _generate_ssh(directory: Path) -> None:
    _run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "pg-stand generated test key",
            "-f",
            str(directory / SSH_PRIVATE_NAME),
        ]
    )


def _generate_signed_certificate(
    directory: Path,
    *,
    name: str,
    common_name: str,
    ca_key: Path,
    extensions: list[str],
) -> None:
    key = directory / f"{name}.key"
    request = directory / f"{name}.csr"
    command = [
        "openssl",
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-sha256",
        "-keyout",
        str(key),
        "-out",
        str(request),
        "-subj",
        f"/CN={common_name}/O=pg-stand",
    ]
    for extension in extensions:
        command.extend(["-addext", extension])
    _run(command)
    serial = directory / "ca.srl"
    signing_command = [
        "openssl",
        "x509",
        "-req",
        "-sha256",
        "-days",
        "7300",
        "-in",
        str(request),
        "-CA",
        str(directory / "ca.crt"),
        "-CAkey",
        str(ca_key),
        "-CAserial",
        str(serial),
    ]
    if not serial.exists():
        signing_command.append("-CAcreateserial")
    signing_command.extend(["-copy_extensions", "copy", "-out", str(directory / f"{name}.crt")])
    _run(signing_command)


def _generate_tls(directory: Path) -> None:
    ca_key = directory / "ca.key"
    _run(["openssl", "genrsa", "-out", str(ca_key), "3072"])
    _run(
        [
            "openssl",
            "req",
            "-new",
            "-x509",
            "-sha256",
            "-days",
            "7300",
            "-key",
            str(ca_key),
            "-out",
            str(directory / "ca.crt"),
            "-subj",
            "/CN=pg-stand generated test CA/O=pg-stand",
        ]
    )
    _generate_signed_certificate(
        directory,
        name="server",
        common_name="localhost",
        ca_key=ca_key,
        extensions=[
            "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1",
            "extendedKeyUsage=serverAuth",
            "keyUsage=digitalSignature,keyEncipherment",
        ],
    )
    for name in ("postgres", "replicator"):
        _generate_signed_certificate(
            directory,
            name=name,
            common_name=name,
            ca_key=ca_key,
            extensions=["extendedKeyUsage=clientAuth", "keyUsage=digitalSignature"],
        )
    ca_key.unlink()
    for generated in directory.iterdir():
        if generated.name not in TLS_REQUIRED_FILES:
            generated.unlink()


def _validate_ssh(directory: Path) -> None:
    derived = _run(["ssh-keygen", "-y", "-f", str(directory / SSH_PRIVATE_NAME)]).split()
    try:
        public = (directory / f"{SSH_PRIVATE_NAME}.pub").read_text(encoding="utf-8").split()
    except OSError as exc:
        raise DockerRuntimeError(f"cannot read generated SSH public key: {exc}") from exc
    if len(derived) < 2 or len(public) < 2 or derived[:2] != public[:2]:
        raise DockerRuntimeError("generated SSH public and private keys do not match")


def _validate_tls(directory: Path) -> None:
    ca = directory / "ca.crt"
    for name, purpose in (
        ("server", "sslserver"),
        ("postgres", "sslclient"),
        ("replicator", "sslclient"),
    ):
        certificate = directory / f"{name}.crt"
        key = directory / f"{name}.key"
        verify = [
            "openssl",
            "verify",
            "-purpose",
            purpose,
            "-CAfile",
            str(ca),
        ]
        if name == "server":
            verify.extend(["-verify_hostname", "localhost"])
        verify.append(str(certificate))
        _run(verify)
        certificate_public = _run(["openssl", "x509", "-in", str(certificate), "-pubkey", "-noout"])
        key_public = _run(["openssl", "pkey", "-in", str(key), "-pubout"])
        if certificate_public != key_public:
            raise DockerRuntimeError(f"generated TLS certificate and key do not match: {name}")
        if name != "server":
            subject = _run(
                [
                    "openssl",
                    "x509",
                    "-in",
                    str(certificate),
                    "-noout",
                    "-subject",
                    "-nameopt",
                    "RFC2253",
                ]
            )
            if f"CN={name}" not in subject.split("subject=", 1)[-1].split(","):
                raise DockerRuntimeError(
                    f"generated TLS client certificate has an unexpected subject: {name}"
                )


def _ensure_set(
    destination: Path,
    required_files: tuple[str, ...],
    generator: Callable[[Path], None],
    validator: Callable[[Path], None],
    *,
    output: Callable[[str], None],
) -> None:
    if destination.is_symlink():
        raise DockerRuntimeError(f"credential directory must not be a symlink: {destination}")
    if destination.exists() and not destination.is_dir():
        raise DockerRuntimeError(f"credential path is not a directory: {destination}")
    actual = {item.name for item in destination.iterdir()} if destination.is_dir() else set()
    unexpected = sorted(actual - set(required_files))
    if unexpected:
        raise DockerRuntimeError(
            f"credential directory contains unexpected entries: {destination}; "
            + ", ".join(unexpected)
        )
    existing = {
        name
        for name in required_files
        if (destination / name).is_file() and not (destination / name).is_symlink()
    }
    if len(existing) == len(required_files):
        for name in required_files:
            os.chmod(destination / name, 0o600 if name in PRIVATE_FILES else 0o644)
        validator(destination)
        return
    if destination.exists() and existing:
        missing = sorted(set(required_files) - existing)
        raise DockerRuntimeError(
            f"credential directory is incomplete: {destination}; missing: {', '.join(missing)}"
        )
    if destination.exists():
        try:
            destination.rmdir()
        except OSError as exc:
            raise DockerRuntimeError(
                f"credential directory contains unexpected files: {destination}"
            ) from exc
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}-generate-",
        dir=destination.parent,
    ) as temporary:
        staging = Path(temporary)
        os.chmod(staging, 0o700)
        generator(staging)
        missing = [name for name in required_files if not (staging / name).is_file()]
        if missing:
            raise DockerRuntimeError(
                f"credential generator did not create required files: {', '.join(missing)}"
            )
        unexpected = sorted(
            item.name for item in staging.iterdir() if item.name not in required_files
        )
        if unexpected:
            raise DockerRuntimeError(
                "credential generator created unexpected entries: " + ", ".join(unexpected)
            )
        for name in required_files:
            os.chmod(staging / name, 0o600 if name in PRIVATE_FILES else 0o644)
        validator(staging)
        staging.rename(destination)
    output(f"GENERATE credentials={destination.name} path={destination}")


def ensure_credentials(
    project_directory: Path,
    *,
    superuser: str = "postgres",
    replication_username: str = DEFAULT_REPLICATION_USER,
    output: Callable[[str], None] = print,
) -> CredentialPaths:
    paths = credential_paths(project_directory)
    _ensure_set(
        paths.ssh,
        SSH_REQUIRED_FILES,
        _generate_ssh,
        _validate_ssh,
        output=output,
    )
    _ensure_set(
        paths.tls,
        TLS_REQUIRED_FILES,
        _generate_tls,
        _validate_tls,
        output=output,
    )
    _ensure_set(
        paths.database,
        DATABASE_REQUIRED_FILES,
        lambda directory: generate_database_credentials(
            directory,
            superuser=superuser,
            replication_user=replication_username,
        ),
        lambda directory: read_database_credentials(
            directory,
            expected_superuser=superuser,
            expected_replication_user=replication_username,
        ),
        output=output,
    )
    return paths


def _directory_status(
    directory: Path,
    required_files: tuple[str, ...],
    validator: Callable[[Path], None],
) -> dict[str, Any]:
    directory_is_symlink = directory.is_symlink()
    files = {}
    for name in required_files:
        path = directory / name
        present = not directory_is_symlink and path.is_file() and not path.is_symlink()
        stat = path.stat() if present else None
        files[name] = {
            "path": str(path),
            "present": present,
            "bytes": stat.st_size if stat is not None else 0,
            "mode": oct(stat.st_mode & 0o777) if stat is not None else None,
        }
    present_count = sum(1 for item in files.values() if item["present"])
    actual = (
        {item.name for item in directory.iterdir()}
        if directory.is_dir() and not directory_is_symlink
        else set()
    )
    unexpected = sorted(actual - set(required_files))
    if directory_is_symlink:
        state = "invalid"
        unexpected = ["<directory-symlink>"]
    elif unexpected:
        state = "invalid"
    elif present_count == len(required_files):
        state = "ready"
    else:
        state = "absent" if not present_count and not actual else "partial"
    validation_error = None
    if state == "ready":
        try:
            validator(directory)
        except DockerRuntimeError as exc:
            state = "invalid"
            validation_error = str(exc)
    return {
        "path": str(directory),
        "state": state,
        "bytes": sum(item["bytes"] for item in files.values()),
        "unexpected_entries": unexpected,
        "validation_error": validation_error,
        "files": files,
    }


def credentials_status(project_directory: Path) -> dict[str, Any]:
    paths = credential_paths(project_directory)
    ssh = _directory_status(paths.ssh, SSH_REQUIRED_FILES, _validate_ssh)
    tls = _directory_status(paths.tls, TLS_REQUIRED_FILES, _validate_tls)
    database = _directory_status(
        paths.database,
        DATABASE_REQUIRED_FILES,
        read_database_credentials,
    )
    if database["state"] == "ready":
        database.update(database_credentials_summary(paths.database))
    return {
        "root": str(paths.root),
        "bytes": ssh["bytes"] + tls["bytes"] + database["bytes"],
        "ssh": ssh,
        "tls": tls,
        "database": database,
        "generators": {
            "ssh-keygen": shutil.which("ssh-keygen"),
            "openssl": shutil.which("openssl"),
        },
    }


def clean_credentials(project_directory: Path) -> bool:
    paths = credential_paths(project_directory)
    if not paths.root.exists():
        return False
    shutil.rmtree(paths.root)
    return True
