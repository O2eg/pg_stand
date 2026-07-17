"""Command-line interface for pg-stand."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml

import docker
from pg_stand.assets import initialize_project
from pg_stand.cli_parser import _parser
from pg_stand.config import StandConfig, load_config
from pg_stand.errors import DockerRuntimeError, PgStandError
from pg_stand.runtime import StandManager, discover_active_stand

_DURATION_RE = re.compile(r"^(?P<value>[0-9]+(?:\.[0-9]+)?)(?P<unit>[smhd])$")


def _emit(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print(yaml.safe_dump(value, sort_keys=False, allow_unicode=True).rstrip())


def _emit_stream(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, sort_keys=True), flush=True)
    else:
        print("---")
        print(yaml.safe_dump(value, sort_keys=False, allow_unicode=True).rstrip(), flush=True)


def _moment(value: str, *, now: datetime | None = None) -> datetime:
    reference = now or datetime.now(UTC)
    match = _DURATION_RE.fullmatch(value.strip().lower())
    if match:
        factors = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        seconds = float(match.group("value")) * factors[match.group("unit")]
        return reference - timedelta(seconds=seconds)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DockerRuntimeError(
            f"invalid time {value!r}; use an ISO-8601 timestamp or duration such as 30m"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _health_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "max_physical_lag_bytes": args.max_physical_lag_bytes,
        "max_physical_lag_seconds": args.max_physical_lag_seconds,
        "max_logical_lag_seconds": args.max_logical_lag_seconds,
        "min_free_bytes": args.min_free_bytes,
    }


def _manager(config: StandConfig) -> StandManager:
    return StandManager(config, output=lambda message: print(message, file=sys.stderr))


def _watch(manager: StandManager, args: argparse.Namespace, *, replication_only: bool) -> int:
    samples = 0
    while args.count == 0 or samples < args.count:
        value = manager.replication_status() if replication_only else manager.overview()
        _emit_stream(value, as_json=args.json or args.json_lines)
        samples += 1
        if args.count == 0 or samples < args.count:
            time.sleep(args.interval)
    return 0


def _emit_pretty_logs(records: list[dict[str, Any]]) -> None:
    for record in records:
        backend = f" {record['backend_type']}" if record["backend_type"] else ""
        print(
            f"{record['log_time']} [{record['node']}] {record['severity']}{backend}: "
            f"{record['message']}"
        )
        for key in ("detail", "hint", "context", "query"):
            if record[key]:
                print(f"  {key}: {record[key]}")


def _execute(args: argparse.Namespace, config: StandConfig) -> int:
    if args.command == "validate":
        result = {
            "valid": True,
            "config": str(config.source),
            "project": config.metadata.name,
            "postgres_version": config.postgres.version,
            "image": config.postgres.image_name,
            "topology": config.topology.mode,
            "tls": config.postgres.tls.enabled,
        }
        _emit(result, as_json=args.json)
        return 0
    if args.command == "show":
        _emit(config.public_document(), as_json=args.json)
        return 0

    manager = _manager(config)
    if args.command == "up":
        _emit(manager.up(timeout_seconds=args.timeout), as_json=args.json)
    elif args.command == "status":
        _emit(manager.status(), as_json=args.json)
    elif args.command == "plan":
        result = manager.plan()
        _emit(result, as_json=args.json)
        return 2 if result["required_action"] == "blocked" else 0
    elif args.command == "resources":
        _emit(manager.resources(args.node), as_json=args.json)
    elif args.command == "health":
        result = manager.health(**_health_options(args))
        _emit(result, as_json=args.json)
        return int(result["exit_code"])
    elif args.command == "wait":
        result = manager.wait_until_healthy(
            timeout_seconds=args.timeout,
            interval_seconds=args.interval,
            **_health_options(args),
        )
        _emit(result, as_json=args.json)
        return int(result["exit_code"])
    elif args.command == "watch":
        return _watch(manager, args, replication_only=False)
    elif args.command == "replication":
        if args.replication_command == "watch":
            return _watch(manager, args, replication_only=True)
        _emit(manager.replication_status(), as_json=args.json)
    elif args.command == "apply":
        requested_mode = "restart" if args.restart else ("reload" if args.reload else None)
        if args.dry_run:
            result = manager.plan(requested_mode=requested_mode)
            _emit(result, as_json=args.json)
            if result["required_action"] == "blocked":
                return 2
            return 0 if result["can_apply"] else 1
        if requested_mode is None:
            raise DockerRuntimeError(
                "apply requires --reload or --restart unless --dry-run is used"
            )
        _emit(
            manager.apply(
                mode=requested_mode,
                timeout_seconds=args.timeout,
                stop_timeout_seconds=args.stop_timeout,
            ),
            as_json=args.json,
        )
    elif args.command == "stop":
        manager.stop(timeout_seconds=args.timeout)
        _emit({"project": config.metadata.name, "state": "stopped"}, as_json=args.json)
    elif args.command == "restart":
        manager.stop(timeout_seconds=args.stop_timeout)
        _emit(manager.up(timeout_seconds=args.timeout), as_json=args.json)
    elif args.command == "recreate":
        _emit(
            manager.recreate(
                clear_data=args.clear_data,
                timeout_seconds=args.timeout,
                stop_timeout_seconds=args.stop_timeout,
            ),
            as_json=args.json,
        )
    elif args.command == "down":
        manager.down(clear_data=args.clear_data, timeout_seconds=args.timeout)
        _emit(
            {"project": config.metadata.name, "state": "removed", "data_cleared": args.clear_data},
            as_json=args.json,
        )
    elif args.command == "storage":
        if args.storage_command == "init":
            result = manager.storage_init()
        elif args.storage_command == "status":
            result = manager.storage_status()
        elif args.storage_command == "usage":
            result = manager.storage_usage()
        elif args.storage_command == "clean":
            result = manager.storage_clean()
        else:  # pragma: no cover
            raise AssertionError(f"unhandled storage command: {args.storage_command}")
        _emit(result, as_json=args.json)
    elif args.command == "logical":
        _emit(
            manager.logical_sync(
                full=args.full,
                timeout_seconds=args.timeout,
                stop_timeout_seconds=args.stop_timeout,
            ),
            as_json=args.json,
        )
    elif args.command == "standby":
        _emit(
            manager.standby_reinit(
                timeout_seconds=args.timeout,
                stop_timeout_seconds=args.stop_timeout,
            ),
            as_json=args.json,
        )
    elif args.command == "node":
        if args.node_command == "inspect":
            result = manager.node_inspect(args.node)
        else:
            result = manager.node_restart(
                args.node,
                timeout_seconds=args.timeout,
                stop_timeout_seconds=args.stop_timeout,
            )
        _emit(result, as_json=args.json)
    elif args.command == "logs":
        has_filters = any((args.since, args.until, args.severity, args.backend_type, args.contains))
        if args.follow:
            if args.node == "all" or has_filters or args.format != "raw":
                raise DockerRuntimeError("--follow supports one node in raw format without filters")
            manager.logs(args.node, follow=True, tail=args.tail)
        elif args.format == "raw" and args.node != "all" and not has_filters and not args.json:
            manager.logs(args.node, tail=args.tail)
        else:
            now = datetime.now(UTC)
            records = manager.log_records(
                args.node,
                tail=args.tail,
                since=_moment(args.since, now=now) if args.since else None,
                until=_moment(args.until, now=now) if args.until else None,
                severity=args.severity,
                backend_type=args.backend_type,
                contains=args.contains,
            )
            if args.json or args.format == "json":
                print(json.dumps(records, indent=2, sort_keys=True))
            else:
                _emit_pretty_logs(records)
    elif args.command == "events":
        if args.follow and args.until:
            raise DockerRuntimeError("events --follow cannot be combined with --until")
        now = datetime.now(UTC)
        since = int(_moment(args.since, now=now).timestamp())
        until = (
            None
            if args.follow
            else int((_moment(args.until, now=now) if args.until else now).timestamp())
        )
        for event in manager.docker_events(since_epoch=since, until_epoch=until, count=args.count):
            _emit_stream(event, as_json=args.json or args.json_lines)
    elif args.command == "doctor":
        result = manager.doctor()
        _emit(result, as_json=args.json)
        return int(result["exit_code"])
    elif args.command == "capabilities":
        _emit(manager.capabilities(), as_json=args.json)
    elif args.command == "dsn":
        _emit(
            manager.connection_info(
                args.node,
                database=args.database,
                show_password=args.show_password,
            ),
            as_json=args.json,
        )
    elif args.command == "psql":
        return manager.run_psql(args.node, database=args.database)
    elif args.command == "ssh":
        return manager.run_ssh(args.node)
    elif args.command == "image":
        if args.image_command == "list":
            result = manager.image_list()
        elif args.image_command == "inspect":
            result = manager.image_inspect()
        else:
            result = manager.image_build()
        _emit(result, as_json=args.json)
    elif args.command == "cleanup":
        if args.cleanup_command == "status":
            result = manager.cleanup_status()
        else:
            result = manager.cleanup(
                remove_containers=args.containers or args.all,
                remove_storage=args.storage or args.all,
                remove_images=args.images or args.all,
                remove_cache=args.cache or args.all,
                remove_credentials=args.all,
                stop_timeout_seconds=args.stop_timeout,
            )
        _emit(result, as_json=args.json)
    elif args.command == "sql":
        print(manager.sql(args.node, args.statement, database=args.database))
    else:  # pragma: no cover
        raise AssertionError(f"unhandled command: {args.command}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "active":
            result = discover_active_stand()
            _emit(result, as_json=args.json)
            return 0 if result["active"] else 1
        if args.command == "init":
            _emit(
                initialize_project(args.directory, force=args.force),
                as_json=args.json,
            )
            return 0
        if args.config is None:
            raise DockerRuntimeError(f"--config is required for {args.command}")
        config = load_config(
            args.config,
            postgres_version=args.postgres_version,
            tls_enabled=args.tls,
        )
        return _execute(args, config)
    except (PgStandError, docker.errors.DockerException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ERROR: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
