"""Applied configuration state and host-mounted PostgreSQL storage."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from docker.errors import ContainerError, DockerException, ImageNotFound

from pg_stand.credentials import credential_paths
from pg_stand.errors import DockerRuntimeError
from pg_stand.locking import serialized
from pg_stand.orchestration import canonical_hash
from pg_stand.runtime_common import (
    PGLOG,
    PGWAL,
    SSH_SOURCE_MOUNT,
    STORAGE_ROOT,
    TLS_SOURCE_MOUNT,
    NodeStoragePaths,
    effective_postgres_parameters,
    postgres_data_paths,
    postgres_parameter_file,
)


class StateStorageMixin:
    def _storage_root(self) -> Path:
        project_directory = self.config.project_directory.resolve()
        storage_root = self.config.storage_root.resolve()
        if storage_root == project_directory or project_directory not in storage_root.parents:
            raise DockerRuntimeError(
                "storage root escaped the project directory; "
                "check symlinks and storage.root_directory"
            )
        return storage_root

    def _node_storage(self, node_name: str) -> NodeStoragePaths:
        storage_root = self._storage_root()
        requested_root = storage_root / node_name
        if requested_root.is_symlink():
            raise DockerRuntimeError(
                f"node storage directory must not be a symlink: {requested_root}"
            )
        root = requested_root.resolve()
        if root == storage_root or storage_root not in root.parents:
            raise DockerRuntimeError(f"node storage escaped the stand root: {requested_root}")
        children: dict[str, Path] = {}
        for name in ("data", "wal", "log"):
            requested = root / name
            if requested.is_symlink():
                raise DockerRuntimeError(
                    f"node storage directory must not be a symlink: {requested}"
                )
            resolved = requested.resolve()
            if resolved == root or root not in resolved.parents:
                raise DockerRuntimeError(f"node storage escaped its node root: {requested}")
            children[name] = resolved
        return NodeStoragePaths(
            root=root,
            data=children["data"],
            wal=children["wal"],
            log=children["log"],
        )

    def _applied_state_path(self) -> Path:
        path = self._storage_root() / ".pg-stand-applied.json"
        if path.is_symlink():
            raise DockerRuntimeError(f"applied configuration state must not be a symlink: {path}")
        return path

    def _configuration_signature(self, *, include_parameters: bool) -> str:
        document = self.config.configuration_document()
        document.pop("source", None)
        document.pop("project_directory", None)
        if not include_parameters:
            document["postgres"].pop("parameters", None)
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _identity_signature(self) -> str:
        document = {
            "project": self.config.metadata.name,
            "version": self.config.postgres.version,
            "database": self.config.postgres.database,
            "superuser": self.config.postgres.superuser,
            "initdb_args": self.config.postgres.initdb_args,
            "topology": self.config.topology.mode,
            "network": self.config.docker.network_name,
            "storage": self.config.storage.root_directory,
            "nodes": {name: node.container_name for name, node in self._configured_nodes()},
        }
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _read_applied_state(self) -> dict[str, Any] | None:
        path = self._applied_state_path()
        if not path.is_file():
            return None
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DockerRuntimeError(
                f"cannot read applied configuration state {path}: {exc}"
            ) from exc
        if not isinstance(state, dict):
            raise DockerRuntimeError(f"applied configuration state is invalid: {path}")
        return state

    def _applied_document(self) -> dict[str, Any]:
        document = self.config.public_document()
        for key in (
            "source",
            "project_directory",
            "resolved_storage_root",
            "config_hash",
        ):
            document.pop(key, None)
        return document

    def _record_applied_state(self) -> None:
        path = self._applied_state_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        state = {
            "state_version": 2,
            "config_hash": self.config.config_hash,
            "non_parameter_signature": self._configuration_signature(include_parameters=False),
            "identity_signature": self._identity_signature(),
            "parameters": dict(sorted(self.config.postgres.parameters.items())),
            "postgres_version": self.config.postgres.version,
            "document": self._applied_document(),
        }
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            stream = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor = -1
            with stream:
                stream.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
            temporary.replace(path)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _flatten_document(value: Any, prefix: str = "") -> dict[str, Any]:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key in sorted(value):
                child = f"{prefix}.{key}" if prefix else key
                result.update(StateStorageMixin._flatten_document(value[key], child))
            return result
        if isinstance(value, list):
            result = {}
            for index, item in enumerate(value):
                result.update(StateStorageMixin._flatten_document(item, f"{prefix}[{index}]"))
            return result
        return {prefix: value}

    def plan(self, *, requested_mode: str | None = None) -> dict[str, Any]:
        """Describe the required apply action without changing Docker or PostgreSQL state."""
        self._ping()
        state = self._read_applied_state()
        result: dict[str, Any] = {
            "project": self.config.metadata.name,
            "requested_mode": requested_mode,
            "instance_id": self.config.instance_id,
            "required_action": "none",
            "can_apply": True,
            "configuration_changes": [],
            "parameter_changes": [],
            "desired_state_hash": self.config.config_hash,
            "applied_state_hash": state.get("config_hash") if state else None,
        }
        if self._find_active_lock() is not None:
            try:
                self._assert_current_stand_is_active()
            except DockerRuntimeError as exc:
                result.update(required_action="blocked", can_apply=False, reason=str(exc))
                return self._finalize_plan(result)
        try:
            self._assert_resource_conflicts()
        except DockerRuntimeError as exc:
            result.update(required_action="blocked", can_apply=False, reason=str(exc))
            return self._finalize_plan(result)
        if state is None:
            result.update(
                {
                    "required_action": "up",
                    "can_apply": requested_mode is None,
                    "reason": "the stand has no applied configuration state",
                }
            )
            return self._finalize_plan(result)
        if state.get("config_hash") == self.config.config_hash:
            result["reason"] = "the desired and applied configurations match"
            return self._finalize_plan(result)

        old_document = state.get("document")
        if isinstance(old_document, dict):
            old_values = self._flatten_document(old_document)
            new_values = self._flatten_document(self._applied_document())
            for path in sorted(set(old_values) | set(new_values)):
                if old_values.get(path) != new_values.get(path):
                    result["configuration_changes"].append(
                        {
                            "path": path,
                            "applied": old_values.get(path),
                            "desired": new_values.get(path),
                        }
                    )

        if state.get("identity_signature") != self._identity_signature():
            result.update(
                {
                    "required_action": "blocked",
                    "can_apply": False,
                    "reason": (
                        "stand identity fields changed; use the old configuration to run down "
                        "the stand before deploying the new identity"
                    ),
                }
            )
            return self._finalize_plan(result)

        previous_parameters = state.get("parameters") or {}
        if not isinstance(previous_parameters, dict):
            result.update(
                {
                    "required_action": "restart",
                    "can_apply": requested_mode in {None, "restart"},
                    "reason": "the applied parameter state is unavailable",
                }
            )
            return self._finalize_plan(result)
        changed_names = {
            name
            for name in set(previous_parameters) | set(self.config.postgres.parameters)
            if previous_parameters.get(name) != self.config.postgres.parameters.get(name)
        }
        contexts: dict[str, str] = {}
        primary = self._owned_container(self.config.primary, "primary")
        if primary is not None:
            primary.reload()
            if primary.status == "running":
                contexts = self._parameter_contexts(primary, changed_names)
        for name in sorted(changed_names):
            context = contexts.get(name, "unknown")
            result["parameter_changes"].append(
                {
                    "name": name,
                    "applied": previous_parameters.get(name),
                    "desired": self.config.postgres.parameters.get(name),
                    "context": context,
                    "action": "restart"
                    if context in {"postmaster", "internal", "unknown"}
                    else "reload",
                }
            )

        non_parameter_changed = state.get(
            "non_parameter_signature"
        ) != self._configuration_signature(include_parameters=False)
        restart_parameter = any(
            item["context"] in {"postmaster", "internal", "unknown"}
            for item in result["parameter_changes"]
        )
        required = "restart" if non_parameter_changed or restart_parameter else "reload"
        result["required_action"] = required
        result["can_apply"] = (
            requested_mode is None
            or requested_mode == required
            or (requested_mode == "restart" and required == "reload")
        )
        if requested_mode is not None and not result["can_apply"]:
            result["reason"] = f"requested {requested_mode}, but {required} is required"
        elif non_parameter_changed:
            result["reason"] = "container, topology, image, resource, or managed setting changed"
        else:
            result["reason"] = "PostgreSQL parameters changed"
        return self._finalize_plan(result)

    @staticmethod
    def _finalize_plan(result: dict[str, Any]) -> dict[str, Any]:
        identity = {
            key: result.get(key)
            for key in (
                "project",
                "instance_id",
                "required_action",
                "configuration_changes",
                "parameter_changes",
                "desired_state_hash",
                "applied_state_hash",
            )
        }
        result["plan_hash"] = canonical_hash(identity)
        return result

    def _assert_up_configuration_is_applied(self) -> None:
        state = self._read_applied_state()
        if state is not None and state.get("config_hash") != self.config.config_hash:
            raise DockerRuntimeError(
                "the YAML differs from the applied stand configuration; run apply --reload "
                "or apply --restart"
            )

    def _sync_parameter_file(self, node_name: str) -> bool:
        paths = self._node_storage(node_name)
        pgdata, _ = postgres_data_paths(self.config.postgres.version)
        script = r"""
set -eu
target="$PGDATA/pg_stand_parameters.conf"
temporary="$PGDATA/pg_stand_parameters.conf.tmp"
include_line="include_if_exists = 'pg_stand_parameters.conf'"
changed=0
printf '%s' "$PG_STAND_PARAMETERS" > "$temporary"
chown postgres:postgres "$temporary"
chmod 0600 "$temporary"
if [ ! -f "$target" ] || ! cmp -s "$temporary" "$target"; then
  mv -f "$temporary" "$target"
  changed=1
else
  rm -f "$temporary"
fi
if ! grep -Fqx "$include_line" "$PGDATA/postgresql.conf"; then
  printf '%s\n' "$include_line" >> "$PGDATA/postgresql.conf"
  changed=1
fi
chown postgres:postgres "$target" "$PGDATA/postgresql.conf"
if [ "$changed" -eq 1 ]; then
  printf 'changed\n'
else
  printf 'unchanged\n'
fi
""".strip()
        result = self._run_storage_helper(
            script,
            volumes=self._storage_binds(paths),
            environment={
                "PGDATA": pgdata,
                "PG_STAND_PARAMETERS": postgres_parameter_file(
                    effective_postgres_parameters(self.config)
                ),
            },
        )
        return result.strip() == "changed"

    def _ensure_node_parameters(
        self,
        container: Any,
        node_name: str,
        timeout_seconds: float,
    ) -> bool:
        if not self._sync_parameter_file(node_name):
            return False
        self.output(f"RESTART node={node_name} reason=postgres_parameters")
        container.restart(timeout=30)
        self._wait_for_postgres(container, timeout_seconds)
        return True

    def _configured_node_names(self) -> list[str]:
        result = ["primary"]
        if self.config.standby is not None:
            result.append("standby")
        if self.config.logical is not None:
            result.append("logical")
        return result

    def _storage_binds(self, paths: NodeStoragePaths) -> dict[str, dict[str, str]]:
        _, data_mount = postgres_data_paths(self.config.postgres.version)
        return {
            str(paths.data): {"bind": data_mount, "mode": "rw"},
            str(paths.wal): {"bind": PGWAL, "mode": "rw"},
            str(paths.log): {"bind": PGLOG, "mode": "rw"},
        }

    def _database_binds(self, paths: NodeStoragePaths) -> dict[str, dict[str, str]]:
        binds = self._storage_binds(paths)
        credentials = credential_paths(self.config.project_directory)
        binds[str(credentials.ssh)] = {"bind": SSH_SOURCE_MOUNT, "mode": "ro"}
        if self.config.postgres.tls.enabled:
            binds[str(credentials.tls)] = {
                "bind": TLS_SOURCE_MOUNT,
                "mode": "ro",
            }
        return binds

    def _ensure_host_storage_directories(self) -> None:
        storage_root = self._storage_root()
        storage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for node_name in self._configured_node_names():
            paths = self._node_storage(node_name)
            paths.root.mkdir(exist_ok=True, mode=0o700)
            paths.data.mkdir(exist_ok=True, mode=0o700)
            paths.wal.mkdir(exist_ok=True, mode=0o700)
            paths.log.mkdir(exist_ok=True, mode=0o750)

    def _run_storage_helper(
        self,
        script: str,
        *,
        volumes: dict[str, dict[str, str]],
        environment: dict[str, str] | None = None,
    ) -> str:
        try:
            helper = self.client.containers.run(
                self.config.postgres.image_name,
                command=[script],
                entrypoint=["/bin/sh", "-ceu"],
                detach=True,
                environment=environment or {},
                volumes=volumes,
                labels=self._labels(node="storage-helper"),
            )
            try:
                exit_code = int(helper.wait()["StatusCode"])
                output = helper.logs(stdout=exit_code == 0, stderr=exit_code != 0)
            finally:
                # v=True also removes the anonymous volume Docker creates for the image's
                # declared data directory whenever the helper does not mount that path.
                helper.remove(force=True, v=True)
        except (DockerException, ContainerError) as exc:
            raise DockerRuntimeError(f"storage helper failed: {exc}") from exc
        text = (
            output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
        ).strip()
        if exit_code != 0:
            raise DockerRuntimeError(f"storage helper failed: exit status {exit_code}: {text}")
        return text

    def _prepare_storage(self) -> None:
        self._ensure_host_storage_directories()
        script = r"""
set -eu
install -d -m 0700 -o postgres -g postgres "$PGDATA_MOUNT" "$PGDATA" "$PGWAL"
install -d -m 0750 -o postgres -g postgres "$PGLOG"
if [ -s "$PGDATA/PG_VERSION" ]; then
  actual="$(cat "$PGDATA/PG_VERSION")"
  if [ "$actual" != "$POSTGRES_MAJOR" ]; then
    echo "data directory contains PostgreSQL $actual, expected $POSTGRES_MAJOR" >&2
    exit 3
  fi
else
  if find "$PGDATA" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "data directory is not empty and has no PG_VERSION" >&2
    exit 4
  fi
  if find "$PGWAL" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "WAL directory is not empty for a new cluster" >&2
    exit 4
  fi
fi
""".strip()
        for node_name in self._configured_node_names():
            paths = self._node_storage(node_name)
            pgdata, data_mount = postgres_data_paths(self.config.postgres.version)
            self._run_storage_helper(
                script,
                volumes=self._storage_binds(paths),
                environment={
                    "PGDATA": pgdata,
                    "PGDATA_MOUNT": data_mount,
                    "PGWAL": PGWAL,
                    "PGLOG": PGLOG,
                    "POSTGRES_MAJOR": str(self.config.postgres.version),
                },
            )

    def storage_status(self, *, ensure_image: bool = True) -> dict[str, Any]:
        self._ping()
        if ensure_image:
            self._ensure_image()
        else:
            try:
                self.client.images.get(self.config.postgres.image_name)
            except ImageNotFound:
                return {
                    "project": self.config.metadata.name,
                    "project_directory": str(self.config.project_directory),
                    "storage_root": str(self._storage_root()),
                    "nodes": {
                        node_name: {
                            "postgres_version": None,
                            "directories": {
                                name: {
                                    "path": str(getattr(self._node_storage(node_name), name)),
                                    "bytes": None,
                                    "files": None,
                                }
                                for name in ("data", "wal", "log")
                            },
                        }
                        for node_name in self._configured_node_names()
                    },
                    "measurement_error": (
                        "the managed image is absent; refusing to build an image for a "
                        "read-only size report"
                    ),
                }
        self._ensure_host_storage_directories()
        script = r"""
set -eu
version=""
if [ -s "$PGDATA/PG_VERSION" ]; then
  version="$(cat "$PGDATA/PG_VERSION")"
fi
printf 'version|%s\n' "$version"
for item in "data:$PGDATA_MOUNT" "wal:$PGWAL" "log:$PGLOG"; do
  name="${item%%:*}"
  path="${item#*:}"
  bytes="$(du -sb "$path" | cut -f1)"
  files="$(find "$path" -type f | wc -l)"
  printf '%s|%s|%s\n' "$name" "$bytes" "$files"
done
""".strip()
        result: dict[str, Any] = {
            "project": self.config.metadata.name,
            "project_directory": str(self.config.project_directory),
            "storage_root": str(self._storage_root()),
            "nodes": {},
        }
        for node_name in self._configured_node_names():
            paths = self._node_storage(node_name)
            pgdata, data_mount = postgres_data_paths(self.config.postgres.version)
            output = self._run_storage_helper(
                script,
                volumes=self._storage_binds(paths),
                environment={
                    "PGDATA": pgdata,
                    "PGDATA_MOUNT": data_mount,
                    "PGWAL": PGWAL,
                    "PGLOG": PGLOG,
                },
            )
            node_result: dict[str, Any] = {"postgres_version": None, "directories": {}}
            for line in output.splitlines():
                name, first, *rest = line.split("|")
                if name == "version":
                    node_result["postgres_version"] = first or None
                    continue
                host_path = getattr(paths, name)
                node_result["directories"][name] = {
                    "path": str(host_path),
                    "bytes": int(first),
                    "files": int(rest[0]),
                }
            result["nodes"][node_name] = node_result
        return result

    def storage_usage(self, *, ensure_image: bool = True) -> dict[str, Any]:
        result = self.storage_status(ensure_image=ensure_image)
        total_bytes = 0
        total_files = 0
        for node in result["nodes"].values():
            node_bytes = sum((item["bytes"] or 0) for item in node["directories"].values())
            node_files = sum((item["files"] or 0) for item in node["directories"].values())
            node["total_bytes"] = node_bytes
            node["total_files"] = node_files
            total_bytes += node_bytes
            total_files += node_files
        filesystem_path = self._storage_root()
        while not filesystem_path.exists() and filesystem_path != filesystem_path.parent:
            filesystem_path = filesystem_path.parent
        disk = shutil.disk_usage(filesystem_path)
        result["total_bytes"] = total_bytes
        result["total_files"] = total_files
        result["filesystem"] = {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "stand_percent_of_filesystem": round(
                total_bytes / disk.total * 100.0 if disk.total else 0.0,
                4,
            ),
        }
        return result

    @serialized
    def storage_init(self) -> dict[str, Any]:
        self._ping()
        self._assert_storage_unshared()
        self._ensure_image()
        self._prepare_storage()
        return self.storage_status()

    @serialized
    def storage_clean(self, *, require_absent_containers: bool = True) -> dict[str, Any]:
        self._ping()
        self._assert_storage_unshared()
        self._ensure_image()
        if require_absent_containers:
            for node_name, node in self._configured_nodes():
                if self._owned_container(node, node_name, check_hash=False) is not None:
                    raise DockerRuntimeError(
                        "cannot clean mounted storage while stand containers exist; run down first"
                    )
        storage_root = self._storage_root()
        storage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.output(f"CLEAN storage={storage_root}")
        self._run_storage_helper(
            "find /pg-stand-storage -mindepth 1 -maxdepth 1 -exec rm -rf -- '{}' +",
            volumes={str(storage_root): {"bind": STORAGE_ROOT, "mode": "rw"}},
        )
        self._ensure_host_storage_directories()
        return self.storage_status()
