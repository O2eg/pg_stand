"""Exercise two isolated stands and cross-network logical replication on Docker.

Run explicitly with an installed pg-stand: python tests/integration_multistand.py.
Only newly created projects and the test's external network are removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import docker
import yaml

from pg_stand.config import load_config
from pg_stand.database_credentials import read_database_credentials
from pg_stand.runtime import StandManager
from pg_stand.runtime_common import MANAGED_LABEL, discover_stands


def cli(project, config, *arguments):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pg_stand",
            "--project",
            str(project),
            "--config",
            str(config),
            "--machine",
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return json.loads(result.stdout)


def sql(manager, statement):
    return manager.sql("primary", statement, database="app_01").strip()


def wait_rows(manager, count):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if sql(manager, "select count(*) from public.multi_probe") == str(count):
            return
        time.sleep(0.2)
    raise AssertionError("logical rows did not arrive at the target")


def existing_nodes(client):
    return {
        node.id: (node.status, node.attrs["State"]["StartedAt"])
        for node in client.containers.list(
            all=True, ignore_removed=True, filters={"label": f"{MANAGED_LABEL}=true"}
        )
    }


def main():
    client = docker.from_env()
    previous = existing_nodes(client)
    token = uuid.uuid4().hex[:8]
    network = client.networks.create("pg-stand-test-" + token, driver="bridge")
    managers = []
    pg_major = int(os.environ.get("PG_STAND_POSTGRES_VERSION", "18"))
    try:
        root = Path(tempfile.mkdtemp(prefix="pg-stand-multi-"))
        sockets = [socket.socket() for _ in range(8)]
        try:
            for listener in sockets:
                listener.bind(("127.0.0.1", 0))
            ports = [listener.getsockname()[1] for listener in sockets]
            projects = []
            configs = []
            for index, side in enumerate(("source", "target")):
                project = root / side
                project.mkdir()
                name = f"multi-{side}-{token}"
                pg = {
                    "version": pg_major,
                    "parameters": {"shared_buffers": "64MB", "max_connections": 100},
                }
                if side == "source":
                    pg["logical_decoding"] = {}
                document = {
                    "api_version": "pg_stand/v1",
                    "kind": "PostgreSQLStand",
                    "metadata": {"name": name},
                    "spec": {
                        "postgres": pg,
                        "topology": {"mode": "single"},
                        "docker": {"external_networks": [network.name]},
                        "nodes": {
                            "primary": dict(
                                zip(
                                    (
                                        "published_port",
                                        "ssh_published_port",
                                        "pgbouncer_session_published_port",
                                        "pgbouncer_transaction_published_port",
                                    ),
                                    ports[index * 4 : index * 4 + 4],
                                    strict=True,
                                ),
                                bind_address="127.0.0.1",
                                cpu_limit=1.0,
                                memory_limit="768m",
                                shm_size="256m",
                            )
                        },
                    },
                }
                path = project / "stand.yaml"
                path.write_text(yaml.safe_dump(document))
                projects.append(project)
                configs.append(path)
                managers.append(
                    StandManager(
                        load_config(path, project_directory=project), output=lambda _: None
                    )
                )
        finally:
            for listener in sockets:
                listener.close()

        # Resolve/build the shared image once before the concurrent lifecycle test.
        managers[0]._ensure_image()
        print("Starting two projects concurrently", flush=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda pair: cli(*pair, "up", "--timeout", "240"),
                    zip(projects, configs, strict=True),
                )
            )
        assert all(result["status"] == "succeeded" for result in results)
        source, target = managers
        try:
            records = discover_stands(client=client)["stands"]
            assert all(
                any(row["instance_id"] == manager.config.instance_id for row in records)
                for manager in managers
            )
            assert source.health()["exit_code"] == target.health()["exit_code"] == 0
            assert sql(source, "show wal_level") == "logical"
            assert sql(source, "select count(*) from pg_publication") == "0"
            assert sql(source, "select count(*) from pg_replication_slots") == "0"
            target_start = sql(target, "select pg_postmaster_start_time()")
            password_file = (
                target.config.project_directory / ".pg_stand/credentials/database/passwords.json"
            )
            credential_hash = hashlib.sha256(password_file.read_bytes()).hexdigest()

            for manager in managers:
                sql(manager, "create table public.multi_probe(id integer primary key)")
            sql(source, "insert into public.multi_probe values(1),(2),(3)")
            sql(source, "create publication multi_publication for table public.multi_probe")
            credentials = read_database_credentials(
                source.config.project_directory / ".pg_stand/credentials/database"
            )
            target_container = client.containers.get(target.config.primary.container_name)
            result = target_container.exec_run(
                [
                    "sh",
                    "-ceu",
                    'umask 077; printf "%s\\n" "$PGPASS_LINE" > /tmp/multi-repl.pgpass',
                ],
                user="postgres",
                environment={
                    "PGPASS_LINE": source.config.primary.container_name
                    + ":5432:app_01:postgres:"
                    + credentials.superuser_password
                },
            )
            assert result.exit_code == 0
            sql(
                target,
                "create subscription multi_subscription connection 'host="
                + source.config.primary.container_name
                + " port=5432 dbname=app_01 user=postgres"
                + " passfile=/tmp/multi-repl.pgpass sslmode=disable'"
                + " publication multi_publication",
            )
            wait_rows(target, 3)
            print("Logical initial copy and DML across the external network", flush=True)
            sql(source, "insert into public.multi_probe values(4)")
            wait_rows(target, 4)

            # A second directory may not take over the source's names or ports.
            competitor = replace(source.config, project_directory=root / "competitor")
            assert StandManager(competitor).plan()["required_action"] == "blocked"
            competitor = replace(
                competitor,
                primary=replace(
                    competitor.primary,
                    container_name="multi-competitor-" + token + "-pg-stand-managed",
                ),
                docker=replace(
                    competitor.docker, network_name="multi-other-" + token + "-pg-stand-managed"
                ),
            )
            assert StandManager(competitor).plan()["required_action"] == "blocked"
            assert not competitor.project_directory.exists()

            source.stop()
            assert target.health()["exit_code"] == 0
            source.up()
            source = StandManager(
                replace(
                    source.config,
                    postgres=replace(
                        source.config.postgres,
                        parameters={**source.config.postgres.parameters, "work_mem": "8MB"},
                    ),
                ),
                output=lambda _: None,
            )
            source.apply(mode="reload")
            assert sql(source, "show work_mem") == "8MB"
            source = StandManager(
                replace(
                    source.config,
                    postgres=replace(
                        source.config.postgres,
                        parameters={
                            **source.config.postgres.parameters,
                            "max_connections": "120",
                        },
                    ),
                ),
                output=lambda _: None,
            )
            source.apply(mode="restart")
            assert sql(source, "show max_connections") == "120"
            sql(source, "insert into public.multi_probe values(5)")
            wait_rows(target, 5)
            assert sql(target, "select pg_postmaster_start_time()") == target_start
            print("Stop, resume, reload and restart preserve the other stand", flush=True)
            sql(target, "drop subscription multi_subscription")
            cli(projects[0], configs[0], "cleanup", "run", "--all", "--force")
            assert target.health()["exit_code"] == 0
            assert sql(target, "select pg_postmaster_start_time()") == target_start
            assert hashlib.sha256(password_file.read_bytes()).hexdigest() == credential_hash
            assert sql(target, "select count(*) from public.multi_probe") == "5"
            assert client.networks.get(network.id)
            print(
                "Project cleanup preserves target data, credentials, image and external network",
                flush=True,
            )
        finally:
            for project, config in reversed(list(zip(projects, configs, strict=True))):
                cli(project, config, "cleanup", "run", "--all", "--force")
        managers.clear()
        shutil.rmtree(root)
    finally:
        # A failed image build must not make cleanup rebuild that same image.
        failures = []
        for manager in reversed(managers):
            try:
                manager.cleanup(
                    remove_containers=True,
                    remove_storage=manager.config.storage_root.exists(),
                    remove_images=False,
                    remove_cache=False,
                    remove_credentials=(
                        manager.config.project_directory / ".pg_stand/credentials"
                    ).exists(),
                )
            except Exception as exc:
                failures.append(type(exc).__name__ + ": " + manager.config.metadata.name)
        try:
            network.remove()
        except Exception as exc:
            failures.append(type(exc).__name__ + ": " + network.name)
        if failures:
            raise RuntimeError("test cleanup requires inspection: " + ", ".join(failures))
        if root.exists():
            shutil.rmtree(root)
        after = existing_nodes(client)
        assert all(after.get(node_id) == state for node_id, state in previous.items())
    print("PASS: two-stand lifecycle, logical replication, cleanup and existing-stand isolation")


if __name__ == "__main__":
    main()
