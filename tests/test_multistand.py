from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml
from docker.errors import NotFound
from docker.models.containers import ContainerCollection

from pg_stand.cli import main
from pg_stand.config import load_config
from pg_stand.errors import ConfigError, DockerRuntimeError
from pg_stand.locking import project_operation
from pg_stand.runtime import StandManager
from pg_stand.runtime_common import (
    ACTIVE_LOCK_NETWORK,
    INSTANCE_LABEL,
    MANAGED_LABEL,
    PROJECT_LABEL,
    RESOURCE_KIND_LABEL,
    STORAGE_ROOT_LABEL,
    active_lock_name,
    discover_active_stand,
    discover_stands,
    primary_postgres_command,
)

PROFILES = Path(__file__).resolve().parents[1] / "src/pg_stand/configs"


def configured(project, name="source"):
    config = load_config(PROFILES / "single.yaml", project_directory=project, environment={})
    return replace(config, metadata=replace(config.metadata, name=name))


def lease(config, *, legacy=False):
    return SimpleNamespace(
        name=ACTIVE_LOCK_NETWORK if legacy else active_lock_name(config.instance_id),
        attrs={
            "Labels": {
                MANAGED_LABEL: "true",
                RESOURCE_KIND_LABEL: "active-lock",
                INSTANCE_LABEL: config.instance_id,
                PROJECT_LABEL: config.metadata.name,
            }
        },
        remove=MagicMock(),
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_other_project_lease_does_not_block_plan_or_get_released(tmp_path, legacy):
    other = lease(configured(tmp_path / "other", "other"), legacy=legacy)
    client = MagicMock()
    client.networks.list.return_value = [other]
    config = configured(tmp_path / "current")
    manager = StandManager(config, client=client)
    assert manager.plan()["required_action"] == "up"
    assert not config.project_directory.exists()
    manager._release_active_lock()
    other.remove.assert_not_called()
    assert manager._acquire_active_lock()
    assert client.networks.create.call_args.args == (active_lock_name(config.instance_id),)


@pytest.mark.parametrize("legacy", [False, True])
def test_owned_lease_is_reused_and_released(tmp_path, legacy):
    config = configured(tmp_path)
    own = lease(config, legacy=legacy)
    client = MagicMock()
    client.networks.list.return_value = [own]
    manager = StandManager(config, client=client)
    assert not manager._acquire_active_lock()
    manager._assert_current_stand_is_active()
    manager._release_active_lock()
    own.remove.assert_called_once()
    client.networks.create.assert_not_called()


def test_discovery_requires_selection_when_more_than_one_stand_exists(tmp_path):
    first, second = configured(tmp_path / "one"), configured(tmp_path / "two", "target")
    client = MagicMock()
    client.networks.list.return_value = [lease(first, legacy=True), lease(second)]
    assert len(discover_stands(client=client)["stands"]) == 2
    with pytest.raises(DockerRuntimeError, match="multiple active stands"):
        discover_active_stand(client=client)
    assert (
        discover_active_stand(client=client, project_directory=first.project_directory)["project"]
        == "source"
    )
    assert discover_active_stand(client=client, name="target")["instance_id"] == second.instance_id
    assert not discover_active_stand(client=client, name="missing")["active"]


def test_plan_hash_is_bound_to_project_directory(tmp_path):
    first = StandManager(configured(tmp_path / "one"), client=MagicMock()).plan()
    second = StandManager(configured(tmp_path / "two"), client=MagicMock()).plan()
    assert first["desired_state_hash"] == second["desired_state_hash"]
    assert first["plan_hash"] != second["plan_hash"]


def test_lifecycle_lock_excludes_same_project_and_allows_other_projects(tmp_path):
    code = """
import sys
from pathlib import Path
from pg_stand.locking import project_operation
from pg_stand.errors import PreconditionError
try:
    with project_operation(Path(sys.argv[1])):
        pass
except PreconditionError:
    sys.exit(3)
"""
    project = tmp_path / "one"
    with project_operation(project), project_operation(project):
        assert subprocess.run([sys.executable, "-c", code, str(project)]).returncode == 3
        assert subprocess.run([sys.executable, "-c", code, str(tmp_path / "two")]).returncode == 0
    assert subprocess.run([sys.executable, "-c", code, str(project)]).returncode == 0


def test_lifecycle_lock_is_released_on_worker_loss(tmp_path):
    code = """
import sys
from pathlib import Path
from pg_stand.locking import project_operation
with project_operation(Path(sys.argv[1])):
    print('locked', flush=True)
    sys.stdin.read()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        child.kill()
        child.wait(timeout=10)
        with project_operation(tmp_path):
            pass
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


@pytest.mark.parametrize("collision", ["name", "port", "storage", "credentials", "network"])
def test_plan_rejects_foreign_resources_before_mutation(tmp_path, collision):
    config = configured(tmp_path)
    foreign = SimpleNamespace(name="foreign", attrs={}, status="running")
    client = MagicMock()
    client.containers.list.return_value = [foreign]
    if collision == "name":
        foreign.name = config.primary.container_name
    elif collision == "port":
        foreign.attrs = {
            "HostConfig": {
                "PortBindings": {
                    "5432/tcp": [
                        {"HostIp": "0.0.0.0", "HostPort": str(config.primary.published_port)}
                    ],
                }
            }
        }
    elif collision in {"storage", "credentials"}:
        source = (
            config.storage_root if collision == "storage" else tmp_path / ".pg_stand/credentials"
        )
        foreign.attrs = {"Mounts": [{"Type": "bind", "Source": str(source)}]}
    else:
        client.networks.list.return_value = [
            SimpleNamespace(name=config.docker.network_name, attrs={})
        ]
    manager = StandManager(config, client=client)
    assert manager.plan()["required_action"] == "blocked"
    client.containers.run.assert_not_called()
    client.networks.create.assert_not_called()
    assert not config.storage_root.exists()


def test_storage_cleanup_rejects_foreign_mount_before_removing_anything(tmp_path):
    config = configured(tmp_path)
    client = MagicMock()
    client.containers.list.return_value = [
        SimpleNamespace(
            name="foreign", attrs={"Mounts": [{"Type": "bind", "Source": str(config.storage_root)}]}
        )
    ]
    manager = StandManager(config, client=client)
    with patch.object(manager, "down") as down, pytest.raises(DockerRuntimeError, match="overlap"):
        manager.cleanup(
            remove_containers=True, remove_storage=True, remove_images=False, remove_cache=False
        )
    down.assert_not_called()


def test_image_cleanup_preserves_images_referenced_by_another_stand(tmp_path):
    manager = StandManager(configured(tmp_path), client=MagicMock())
    image = SimpleNamespace(id="sha256:in-use", attrs={})
    manager.client.containers.list.return_value = [SimpleNamespace(attrs={"Image": image.id})]
    with (
        patch.object(manager, "_managed_images", return_value=[(image, False)]),
        patch.object(
            manager, "_owned_image_tags", return_value=["pg-stand/postgres:18-pg-stand-managed"]
        ),
    ):
        result = manager._remove_managed_images()
    assert result["skipped_in_use_tags"]
    assert not result["removed_tags"]
    manager.client.images.remove.assert_not_called()


def test_external_network_attachment_is_idempotent_and_never_removes_network(tmp_path):
    config = configured(tmp_path)
    config = replace(config, docker=replace(config.docker, external_networks=("migration",)))
    client = MagicMock()
    network = MagicMock(attrs={"Labels": {}, "Driver": "bridge"})
    network.name = "migration"
    client.networks.get.return_value = network
    container = MagicMock(attrs={"NetworkSettings": {"Networks": {}}})
    manager = StandManager(config, client=client)
    manager._connect_external_networks(container)
    network.connect.assert_called_once_with(container)
    container.attrs["NetworkSettings"]["Networks"]["migration"] = {}
    manager._connect_external_networks(container)
    network.connect.assert_called_once()
    network.remove.assert_not_called()


def test_logical_single_does_not_create_stand_replication(tmp_path):
    config = load_config(PROFILES / "single-logical.yaml", project_directory=tmp_path)
    command = primary_postgres_command(config)
    assert "wal_level=logical" in command
    assert "max_replication_slots=10" in command
    assert not config.is_replicated
    assert not config.has_logical_replica
    assert config.standby is None and config.logical is None


@pytest.mark.parametrize(
    "change",
    ["bad_slots", "physical_topology", "duplicate_network", "managed_network", "host_network"],
)
def test_invalid_multistand_configuration_is_rejected(tmp_path, change):
    filename = "primary-standby-sync.yaml" if change == "physical_topology" else "single.yaml"
    data = yaml.safe_load((PROFILES / filename).read_text())
    if change in {"bad_slots", "physical_topology"}:
        data["spec"]["postgres"]["logical_decoding"] = {
            "max_replication_slots": 0 if change == "bad_slots" else 10
        }
    else:
        networks = {
            "duplicate_network": ["migration", "migration"],
            "managed_network": [data["spec"]["docker"]["network_name"]],
            "host_network": ["host"],
        }
        data["spec"]["docker"]["external_networks"] = networks[change]
    path = tmp_path / "stand.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_config(path, project_directory=tmp_path)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("single", "0fb5a982095b1b9037f9ae3401b07da7972031787db2ecca64272f9490718163"),
        (
            "primary-standby-async",
            "c820da96ce95e288f896bf8ad8bf0ebfc7c444359c506e3676171c1c4bcd1ebb",
        ),
        (
            "primary-standby-sync",
            "c0fa526a58fe4dc06298bccafe1a63ec8e083bac0f5c7e282adfc69427ccd2b3",
        ),
        (
            "primary-standby-sync-logical",
            "96c8533140202b60f9ffea6be4b0f27c8cc9c930d57d9138871561b433d6fe00",
        ),
    ],
)
def test_existing_profiles_preserve_v024_config_hash(name, expected):
    assert load_config(PROFILES / (name + ".yaml"), environment={}).config_hash == expected


def test_cli_project_and_machine_discovery(tmp_path, capsys):
    with patch("pg_stand.cli.discover_active_stand", return_value={"active": True}) as active:
        assert main(["--project", str(tmp_path), "--machine", "active", "--name", "source"]) == 0
    active.assert_called_once_with(project_directory=tmp_path, name="source")
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"
    with patch("pg_stand.cli.discover_stands", return_value={"stands": []}):
        assert main(["--machine", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == {"stands": []}


def test_cleanup_all_is_project_scoped(tmp_path, capsys):
    manager = MagicMock()
    manager.cleanup.return_value = {}
    with patch("pg_stand.cli._manager", return_value=manager):
        assert (
            main(
                [
                    "--project",
                    str(tmp_path),
                    "-c",
                    str(PROFILES / "single.yaml"),
                    "cleanup",
                    "run",
                    "--all",
                    "--force",
                ]
            )
            == 0
        )
    assert manager.cleanup.call_args.kwargs == dict(
        remove_containers=True,
        remove_storage=True,
        remove_credentials=True,
        remove_images=False,
        remove_cache=False,
        stop_timeout_seconds=30,
    )


def test_pgbouncer_pid_check_requires_exact_binary_and_pool_config(tmp_path):
    script = (PROFILES.parent / "docker/pg-stand-pgbouncer.sh").read_text()
    functions = script.split('case "${1:-}" in')[0]
    executable = tmp_path / "pgbouncer"
    shutil.copy2(shutil.which("sleep"), executable)
    owned = subprocess.Popen([str(executable), "120"])
    unrelated = subprocess.Popen([shutil.which("sleep"), "120"])
    try:
        pidfile = tmp_path / "pool.pid"
        environment = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
        for process, config, expected in (
            (owned, "120", 0),
            (owned, "121", 1),
            (unrelated, "120", 1),
        ):
            pidfile.write_text(str(process.pid))
            result = subprocess.run(
                [
                    "sh",
                    "-c",
                    functions + '\nprocess_is_running "$1" "$2"',
                    "test-pid",
                    str(pidfile),
                    config,
                ],
                env=environment,
            )
            assert result.returncode == expected
            assert process.poll() is None
    finally:
        for process in (owned, unrelated):
            process.terminate()
            process.wait(timeout=10)


def test_plan_tolerates_unrelated_container_removal_inside_docker_sdk(tmp_path):
    client = MagicMock()
    client.api.containers.return_value = [{"Id": "removed"}]
    client.api.inspect_container.side_effect = NotFound("container was removed")
    client.containers = ContainerCollection(client)
    manager = StandManager(configured(tmp_path), client=client)
    assert manager.plan()["required_action"] == "up"
    client.api.inspect_container.assert_called()
    client.api.create_container.assert_not_called()


def test_discovery_tolerates_a_node_removed_during_reload(tmp_path):
    config = configured(tmp_path)
    client = MagicMock()
    client.networks.list.return_value = [lease(config)]
    node = SimpleNamespace(
        name=config.primary.container_name, reload=MagicMock(side_effect=NotFound("removed"))
    )
    client.containers.list.return_value = [node]
    result = discover_active_stand(client=client)
    assert result["active"] and result["nodes"] == {}


def test_lease_reserves_storage_before_containers_are_created(tmp_path):
    config = configured(tmp_path / "current")
    other = lease(configured(tmp_path / "other", "other"))
    other.attrs["Labels"][STORAGE_ROOT_LABEL] = str(config.storage_root)
    client = MagicMock()
    client.networks.list.return_value = [other]
    result = StandManager(config, client=client).plan()
    assert result["required_action"] == "blocked"
    assert "overlap another stand lease" in result["reason"]
    client.containers.run.assert_not_called()


@pytest.mark.parametrize(
    "storage", [".pg_stand", ".pg_stand/credentials", ".pg_stand/credentials/data"]
)
def test_storage_cannot_include_or_use_credentials(tmp_path, storage):
    data = yaml.safe_load((PROFILES / "single.yaml").read_text())
    data["spec"]["storage"]["root_directory"] = storage
    path = tmp_path / "stand.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="overlap project credentials"):
        load_config(path, project_directory=tmp_path)


def test_init_respects_project_selection(tmp_path, capsys):
    assert main(["--project", str(tmp_path), "init"]) == 0
    assert (tmp_path / "configs/single-logical.yaml").is_file()


def test_renamed_node_cannot_bypass_mounted_storage_check(tmp_path):
    config = configured(tmp_path)
    client = MagicMock()
    labels = {
        MANAGED_LABEL: "true",
        PROJECT_LABEL: config.metadata.name,
        INSTANCE_LABEL: config.instance_id,
        RESOURCE_KIND_LABEL: "stand-node",
    }
    client.containers.list.return_value = [
        SimpleNamespace(
            name="old-primary-pg-stand-managed",
            status="running",
            attrs={
                "Config": {"Labels": labels},
                "Mounts": [{"Type": "bind", "Source": str(config.storage_root / "primary/data")}],
            },
        )
    ]
    manager = StandManager(config, client=client)
    with (
        patch.object(manager, "_run_storage_helper") as helper,
        pytest.raises(DockerRuntimeError, match="overlap"),
    ):
        manager.down(clear_data=True)
    helper.assert_not_called()
