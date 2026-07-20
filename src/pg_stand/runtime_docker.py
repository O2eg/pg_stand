"""Managed Docker images, cleanup, and host preflight operations."""

from __future__ import annotations

import os
import shutil
import socket
from typing import Any

from docker.errors import APIError, BuildError, ImageNotFound

from pg_stand.assets import docker_build_context
from pg_stand.credentials import clean_credentials, credentials_status
from pg_stand.errors import DockerRuntimeError
from pg_stand.runtime_common import (
    IMAGE_BASE_LABEL,
    IMAGE_FAMILY_LABEL,
    IMAGE_MAJOR_LABEL,
    IMAGE_PGBOUNCER_LABEL,
    IMAGE_RESOURCE_KIND,
    IMAGE_SCHEMA_LABEL,
    IMAGE_SCHEMA_VERSION,
    INSTANCE_LABEL,
    MANAGED_LABEL,
    MANAGED_RESOURCE_SUFFIX,
    NODE_LABEL,
    PGBOUNCER_VERSION,
    PROJECT_LABEL,
    RESOURCE_KIND_LABEL,
    managed_dockerfile,
)


class DockerResourceMixin:
    def _ensure_image(self) -> None:
        image = self.config.postgres.image_name
        policy = self.config.docker.pull_policy
        if self.config.postgres.uses_managed_image:
            self._ensure_managed_image(image, policy)
            return
        try:
            if policy == "always":
                self.output(f"PULL image={image}")
                self.client.images.pull(image)
                return
            self.client.images.get(image)
        except ImageNotFound:
            if policy == "never":
                raise DockerRuntimeError(
                    f"Docker image {image} is absent and pull_policy is never"
                ) from None
            self.output(f"PULL image={image}")
            try:
                self.client.images.pull(image)
            except APIError as exc:
                raise DockerRuntimeError(f"cannot pull Docker image {image}: {exc}") from exc

    def _ensure_managed_image(self, image_name: str, policy: str) -> None:
        rebuild = policy == "always"
        dockerfile_path, image_family = managed_dockerfile(self.config.postgres.version)
        expected_labels = {
            IMAGE_SCHEMA_LABEL: IMAGE_SCHEMA_VERSION,
            IMAGE_BASE_LABEL: self.config.postgres.base_image_name,
            IMAGE_FAMILY_LABEL: image_family,
            IMAGE_MAJOR_LABEL: str(self.config.postgres.version),
            IMAGE_PGBOUNCER_LABEL: PGBOUNCER_VERSION,
            MANAGED_LABEL: "true",
            RESOURCE_KIND_LABEL: IMAGE_RESOURCE_KIND,
        }
        try:
            image = self.client.images.get(image_name)
            labels = (image.attrs.get("Config") or {}).get("Labels") or {}
            if any(labels.get(key) != value for key, value in expected_labels.items()):
                if policy == "never":
                    raise DockerRuntimeError(
                        f"managed image {image_name} has an incompatible image schema"
                    )
                rebuild = True
        except ImageNotFound:
            if policy == "never":
                raise DockerRuntimeError(
                    f"Docker image {image_name} is absent and pull_policy is never"
                ) from None
            rebuild = True
        if not rebuild:
            return
        build_context = docker_build_context()
        dockerfile = build_context / dockerfile_path
        if not dockerfile.is_file():
            raise DockerRuntimeError(f"managed image Dockerfile does not exist: {dockerfile}")
        self.output(
            f"BUILD image={image_name} base={self.config.postgres.base_image_name} "
            f"dockerfile={dockerfile_path}"
        )
        try:
            self.client.images.build(
                path=str(build_context),
                dockerfile=dockerfile_path,
                tag=image_name,
                buildargs={
                    "POSTGRES_BASE_IMAGE": self.config.postgres.base_image_name,
                    "PG_MAJOR": str(self.config.postgres.version),
                    "PGBOUNCER_VERSION": PGBOUNCER_VERSION,
                },
                pull=policy == "always",
                rm=True,
                forcerm=True,
            )
        except BuildError as exc:
            details = "\n".join(
                str(item.get("stream") or item.get("error") or "").strip()
                for item in exc.build_log
                if item.get("stream") or item.get("error")
            )
            raise DockerRuntimeError(
                f"cannot build managed Docker image {image_name}: {exc}\n{details}"
            ) from exc
        except APIError as exc:
            raise DockerRuntimeError(
                f"cannot build managed Docker image {image_name}: {exc}"
            ) from exc
        except OSError as exc:
            raise DockerRuntimeError(
                f"cannot prepare Docker build context for {image_name}: {exc}"
            ) from exc

    @staticmethod
    def _owned_image_tags(image: Any, *, legacy: bool) -> list[str]:
        result = []
        for tag in image.tags:
            if not tag.startswith("pg-stand/postgres:"):
                continue
            image_tag = tag.split(":", 1)[1]
            if legacy:
                if image_tag.isdigit():
                    result.append(tag)
            elif image_tag.endswith(MANAGED_RESOURCE_SUFFIX):
                result.append(tag)
        return result

    def _managed_images(self, *, include_legacy: bool = True) -> list[tuple[Any, bool]]:
        current = self.client.images.list(
            filters={
                "label": [
                    f"{MANAGED_LABEL}=true",
                    f"{RESOURCE_KIND_LABEL}={IMAGE_RESOURCE_KIND}",
                ]
            }
        )
        result: list[tuple[Any, bool]] = []
        seen: set[str] = set()
        for image in current:
            labels = (image.attrs.get("Config") or {}).get("Labels") or {}
            if (
                labels.get(MANAGED_LABEL) == "true"
                and labels.get(RESOURCE_KIND_LABEL) == IMAGE_RESOURCE_KIND
                and self._owned_image_tags(image, legacy=False)
            ):
                result.append((image, False))
                seen.add(image.id)
        if include_legacy:
            legacy_images = self.client.images.list(filters={"label": f"{IMAGE_SCHEMA_LABEL}=2"})
            for image in legacy_images:
                labels = (image.attrs.get("Config") or {}).get("Labels") or {}
                if (
                    image.id not in seen
                    and labels.get(IMAGE_SCHEMA_LABEL) == "2"
                    and self._owned_image_tags(image, legacy=True)
                ):
                    result.append((image, True))
        return result

    def image_list(self) -> dict[str, Any]:
        self._ping()
        result = []
        for image, legacy in self._managed_images():
            labels = (image.attrs.get("Config") or {}).get("Labels") or {}
            tags = self._owned_image_tags(image, legacy=legacy)
            result.append(
                {
                    "id": image.short_id,
                    "tags": sorted(tags),
                    "postgres_version": labels.get(IMAGE_MAJOR_LABEL),
                    "pgbouncer_version": labels.get(IMAGE_PGBOUNCER_LABEL),
                    "family": labels.get(IMAGE_FAMILY_LABEL),
                    "base_image": labels.get(IMAGE_BASE_LABEL),
                    "schema": labels.get(IMAGE_SCHEMA_LABEL),
                    "legacy_without_suffix": legacy,
                    "size_bytes": int(image.attrs.get("Size", 0) or 0),
                }
            )
        return {
            "images": sorted(result, key=lambda item: item["tags"]),
            "aggregate_size_bytes_including_shared_layers": sum(
                item["size_bytes"] for item in result
            ),
        }

    def image_inspect(self) -> dict[str, Any]:
        self._ping()
        name = self.config.postgres.image_name
        try:
            image = self.client.images.get(name)
        except ImageNotFound:
            return {"image": name, "present": False, "compatible": False}
        labels = (image.attrs.get("Config") or {}).get("Labels") or {}
        dockerfile, family = managed_dockerfile(self.config.postgres.version)
        expected = {
            IMAGE_SCHEMA_LABEL: IMAGE_SCHEMA_VERSION,
            IMAGE_BASE_LABEL: self.config.postgres.base_image_name,
            IMAGE_FAMILY_LABEL: family,
            IMAGE_MAJOR_LABEL: str(self.config.postgres.version),
            IMAGE_PGBOUNCER_LABEL: PGBOUNCER_VERSION,
            MANAGED_LABEL: "true",
            RESOURCE_KIND_LABEL: IMAGE_RESOURCE_KIND,
        }
        compatible = not self.config.postgres.uses_managed_image or all(
            labels.get(key) == value for key, value in expected.items()
        )
        return {
            "image": name,
            "present": True,
            "id": image.short_id,
            "tags": sorted(image.tags),
            "size_bytes": int(image.attrs.get("Size", 0) or 0),
            "managed": self.config.postgres.uses_managed_image,
            "compatible": compatible,
            "dockerfile": dockerfile if self.config.postgres.uses_managed_image else None,
            "labels": labels,
            "expected_labels": expected if self.config.postgres.uses_managed_image else {},
        }

    def image_build(self) -> dict[str, Any]:
        if not self.config.postgres.uses_managed_image:
            raise DockerRuntimeError("image build is available only for managed pg-stand images")
        self._ping()
        self._ensure_managed_image(self.config.postgres.image_name, "always")
        return self.image_inspect()

    def cleanup_status(self) -> dict[str, Any]:
        self._ping()
        containers = self.client.api.containers(
            all=True,
            size=True,
            filters=self._managed_filters(
                resource_kind="stand-node",
                project=self.config.metadata.name,
            ),
        )
        container_rows = []
        for container in containers:
            names = container.get("Names") or []
            name = names[0].lstrip("/") if names else ""
            if not name.endswith(MANAGED_RESOURCE_SUFFIX):
                continue
            labels = container.get("Labels") or {}
            if (
                labels.get(PROJECT_LABEL) != self.config.metadata.name
                or labels.get(INSTANCE_LABEL) != self.config.instance_id
            ):
                continue
            container_rows.append(
                {
                    "container": name,
                    "node": labels.get(NODE_LABEL),
                    "status": container.get("State"),
                    "writable_layer_bytes": int(container.get("SizeRw", 0) or 0),
                    "root_filesystem_bytes": int(container.get("SizeRootFs", 0) or 0),
                    "image": container.get("Image"),
                }
            )
        return {
            "project": self.config.metadata.name,
            "containers": container_rows,
            "container_writable_layers_bytes": sum(
                item["writable_layer_bytes"] for item in container_rows
            ),
            "storage": self.storage_usage(ensure_image=False),
            "credentials": credentials_status(self.config.project_directory),
            "images": self.image_list(),
            "cache_scope": (
                "Only dangling images with pg-stand image ownership labels can be pruned; "
                "Docker BuildKit cache is daemon-global and is never pruned by pg-stand."
            ),
        }

    def _remove_managed_images(self) -> dict[str, Any]:
        removed_tags: list[str] = []
        estimated_bytes = 0
        for image, legacy in self._managed_images():
            tags = self._owned_image_tags(image, legacy=legacy)
            if not tags:
                continue
            estimated_bytes += int(image.attrs.get("Size", 0) or 0)
            for tag in tags:
                self.output(f"REMOVE image={tag}")
                try:
                    self.client.images.remove(tag, force=False, noprune=False)
                except APIError as exc:
                    raise DockerRuntimeError(
                        f"cannot remove managed image {tag}; remove its managed containers "
                        f"first: {exc}"
                    ) from exc
                removed_tags.append(tag)
        return {
            "removed_tags": removed_tags,
            "estimated_size_bytes_including_shared_layers": estimated_bytes,
        }

    def _prune_managed_dangling_images(self) -> dict[str, Any]:
        current = self.client.images.prune(
            filters={
                "label": [
                    f"{MANAGED_LABEL}=true",
                    f"{RESOURCE_KIND_LABEL}={IMAGE_RESOURCE_KIND}",
                ]
            }
        )
        legacy = self.client.images.prune(filters={"label": f"{IMAGE_SCHEMA_LABEL}=2"})
        return {
            "deleted": (current.get("ImagesDeleted") or []) + (legacy.get("ImagesDeleted") or []),
            "space_reclaimed_bytes": int(current.get("SpaceReclaimed", 0) or 0)
            + int(legacy.get("SpaceReclaimed", 0) or 0),
        }

    def cleanup(
        self,
        *,
        remove_containers: bool,
        remove_storage: bool,
        remove_images: bool,
        remove_cache: bool,
        remove_credentials: bool = False,
        stop_timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        if not any(
            (
                remove_containers,
                remove_storage,
                remove_images,
                remove_cache,
                remove_credentials,
            )
        ):
            raise DockerRuntimeError("cleanup requires at least one cleanup scope")
        self._ping()
        reserve_active_lock = remove_storage or remove_credentials
        if reserve_active_lock:
            self._acquire_active_lock()
        try:
            if remove_credentials:
                running_nodes = self.client.containers.list(
                    filters={
                        "label": [
                            f"{MANAGED_LABEL}=true",
                            f"{RESOURCE_KIND_LABEL}=stand-node",
                            f"{INSTANCE_LABEL}={self.config.instance_id}",
                        ]
                    }
                )
                blockers = []
                for container in running_nodes:
                    if not container.name.endswith(MANAGED_RESOURCE_SUFFIX):
                        continue
                    labels = (container.attrs.get("Config") or {}).get("Labels") or {}
                    if labels.get(INSTANCE_LABEL) != self.config.instance_id:
                        continue
                    owned_by_cleanup = (
                        remove_containers
                        and labels.get(PROJECT_LABEL) == self.config.metadata.name
                        and labels.get(INSTANCE_LABEL) == self.config.instance_id
                    )
                    if not owned_by_cleanup:
                        blockers.append(container.name)
                if blockers:
                    raise DockerRuntimeError(
                        "cannot remove shared credentials while another managed stand node is "
                        "running: " + ", ".join(sorted(blockers))
                    )
            result: dict[str, Any] = {
                "project": self.config.metadata.name,
                "containers_removed": False,
                "storage_removed": False,
                "credentials_removed": False,
            }
            if remove_containers:
                self.down(
                    clear_data=False,
                    timeout_seconds=stop_timeout_seconds,
                    release_lock=not reserve_active_lock,
                )
                result["containers_removed"] = True
            if remove_storage:
                result["storage_before"] = self.storage_usage(ensure_image=False)
                self.storage_clean(require_absent_containers=True)
                result["storage_removed"] = True
            if remove_credentials:
                running_nodes = self.client.containers.list(
                    filters={
                        "label": [
                            f"{MANAGED_LABEL}=true",
                            f"{RESOURCE_KIND_LABEL}=stand-node",
                            f"{INSTANCE_LABEL}={self.config.instance_id}",
                        ]
                    }
                )
                if any(
                    container.name.endswith(MANAGED_RESOURCE_SUFFIX)
                    and ((container.attrs.get("Config") or {}).get("Labels") or {}).get(
                        INSTANCE_LABEL
                    )
                    == self.config.instance_id
                    for container in running_nodes
                ):
                    raise DockerRuntimeError(
                        "cannot remove shared credentials while a managed stand node is running"
                    )
                result["credentials_removed"] = clean_credentials(self.config.project_directory)
            if remove_images:
                result["images"] = self._remove_managed_images()
            if remove_cache:
                result["cache"] = self._prune_managed_dangling_images()
            return result
        finally:
            if reserve_active_lock and not self._has_running_owned_nodes():
                self._release_active_lock()

    @staticmethod
    def _port_available(address: str, port: int) -> tuple[bool, str]:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.bind((address, port))
        except OSError as exc:
            return False, str(exc)
        finally:
            sock.close()
        return True, "available"

    def doctor(self) -> dict[str, Any]:
        """Run non-mutating host, Docker, image, port, and storage preflight checks."""
        checks: list[dict[str, Any]] = []
        self._ping()
        info = self.client.info()
        checks.append(
            {
                "id": "docker.daemon",
                "status": "healthy",
                "summary": (
                    f"Docker {info.get('ServerVersion', 'unknown')}; "
                    f"CPUs={info.get('NCPU', 'unknown')} memory={info.get('MemTotal', 'unknown')}"
                ),
            }
        )
        configured_cpu = sum(node.cpu_limit for _, node in self._configured_nodes())
        configured_memory = 0
        for _, node in self._configured_nodes():
            suffix = node.memory_limit[-1].lower()
            factor = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}.get(suffix, 1)
            number = node.memory_limit[:-1] if suffix in "bkmg" else node.memory_limit
            configured_memory += int(number) * factor
        enough_cpu = configured_cpu <= float(info.get("NCPU", 0) or 0)
        enough_memory = configured_memory <= int(info.get("MemTotal", 0) or 0)
        checks.append(
            {
                "id": "host.capacity",
                "status": "healthy" if enough_cpu and enough_memory else "degraded",
                "summary": (
                    f"requested CPUs={configured_cpu:g}, memory={configured_memory}; "
                    f"host CPUs={info.get('NCPU', 0)}, memory={info.get('MemTotal', 0)}"
                ),
            }
        )
        image = self.image_inspect()
        checks.append(
            {
                "id": "docker.image",
                "status": "healthy"
                if image.get("present") and image.get("compatible")
                else "degraded",
                "summary": (
                    "configured image is present and compatible"
                    if image.get("present") and image.get("compatible")
                    else "configured image is absent or requires a managed rebuild"
                ),
                "details": image,
            }
        )
        for node_name, node in self._configured_nodes():
            container = self._owned_container(node, node_name)
            for service, port in (
                ("postgresql", node.published_port),
                ("ssh", node.ssh_published_port),
                ("pgbouncer_session", node.pgbouncer_session_published_port),
                ("pgbouncer_transaction", node.pgbouncer_transaction_published_port),
            ):
                if container is not None:
                    available, detail = True, "reserved by the owned container"
                else:
                    available, detail = self._port_available(node.bind_address, port)
                checks.append(
                    {
                        "id": f"node.{node_name}.port.{service}",
                        "status": "healthy" if available else "unavailable",
                        "summary": f"{node.bind_address}:{port}: {detail}",
                    }
                )
        project_directory = self.config.project_directory.resolve()
        storage_root = self.config.storage_root.resolve()
        inside = storage_root != project_directory and project_directory in storage_root.parents
        existing_parent = storage_root
        while not existing_parent.exists() and existing_parent != existing_parent.parent:
            existing_parent = existing_parent.parent
        writable = os.access(existing_parent, os.W_OK | os.X_OK)
        disk = shutil.disk_usage(existing_parent)
        checks.append(
            {
                "id": "storage.root",
                "status": "healthy" if inside and writable else "unavailable",
                "summary": (
                    f"path={storage_root} boundary_ok={inside} writable={writable} "
                    f"free_bytes={disk.free}"
                ),
            }
        )
        credential_report = credentials_status(self.config.project_directory)
        generators_ready = all(credential_report["generators"].values())
        checks.append(
            {
                "id": "credentials.generators",
                "status": "healthy" if generators_ready else "unavailable",
                "summary": (
                    f"ssh-keygen={credential_report['generators']['ssh-keygen']} "
                    f"openssl={credential_report['generators']['openssl']}"
                ),
            }
        )
        for credential_type in ("ssh", "tls", "database"):
            entry = credential_report[credential_type]
            private_modes = [
                item["mode"]
                for name, item in entry["files"].items()
                if name.endswith(".key") or name == "pg_stand_test" or name == "passwords.json"
            ]
            modes_ok = all(mode == "0o600" for mode in private_modes)
            ready = entry["state"] == "ready" and modes_ok
            pending = entry["state"] == "absent" and (
                credential_type == "database" or generators_ready
            )
            checks.append(
                {
                    "id": f"credentials.{credential_type}",
                    "status": "healthy" if ready or pending else "unavailable",
                    "summary": (
                        f"state={entry['state']} path={entry['path']} bytes={entry['bytes']} "
                        f"private_modes={private_modes}; "
                        + (
                            "will be generated on first up"
                            if pending
                            else (
                                "credential set is ready" if ready else "credential set is invalid"
                            )
                        )
                    ),
                }
            )
        level = self._health_level(checks)
        return {
            "project": self.config.metadata.name,
            "status": level,
            "exit_code": {"healthy": 0, "degraded": 1, "unavailable": 2}[level],
            "checks": checks,
        }
