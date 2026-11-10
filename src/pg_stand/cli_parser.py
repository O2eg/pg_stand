"""Argument parser for the pg-stand command-line interface."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from pg_stand import __version__


def _positive_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def _non_negative_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return result


def _non_negative_int(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return result


def _add_health_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-physical-lag-bytes", type=_non_negative_int, default=104_857_600)
    parser.add_argument("--max-physical-lag-seconds", type=_non_negative_float, default=30.0)
    parser.add_argument("--max-logical-lag-seconds", type=_non_negative_float, default=30.0)
    parser.add_argument("--min-free-bytes", type=_non_negative_int, default=268_435_456)


def _add_watch_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--interval", type=_positive_float, default=2.0)
    parser.add_argument(
        "--count",
        type=_non_negative_int,
        default=0,
        help="number of samples; zero watches until interrupted",
    )
    parser.add_argument(
        "--json-lines", action="store_true", help="emit one compact JSON value per line"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pg-stand",
        description="Deploy and observe declarative PostgreSQL stands through the Docker SDK.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        help="path to a pg_stand/v1 YAML configuration",
    )
    parser.add_argument("--project", type=Path, help="project storage directory (default: cwd)")
    parser.add_argument(
        "--pg-version",
        "--postgres-version",
        dest="postgres_version",
        type=int,
        help="override spec.postgres.version without editing the configuration",
    )
    parser.add_argument(
        "--tls",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="override spec.postgres.tls.enabled for this command",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument(
        "--machine",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--request-id",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--parameters-json",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--component-capabilities",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    commands = parser.add_subparsers(dest="command")
    active = commands.add_parser("active", help="discover one active stand without a config")
    active.add_argument("--name", help="select the stand by metadata.name")
    commands.add_parser("list", help="list all active stand leases on this Docker daemon")
    init = commands.add_parser(
        "init", help="write editable example profiles and the JSON Schema into a project"
    )
    init.add_argument(
        "--directory",
        type=Path,
        help="project directory to initialize; defaults to --project or the current directory",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="replace bundled project files that have different content",
    )
    commands.add_parser("validate", help="validate and resolve the configuration")
    commands.add_parser("show", help="show the resolved configuration with redacted secrets")

    up = commands.add_parser("up", help="create or start the stand and wait until it is ready")
    up.add_argument(
        "--timeout", type=_positive_float, default=180.0, help="readiness timeout in seconds"
    )

    commands.add_parser("status", help="show containers and basic replication state")
    commands.add_parser("plan", help="show the required apply action without changing the stand")

    resources = commands.add_parser(
        "resources",
        help="show desired limits, actual limits, usage, hostnames, IPs, and ports",
    )
    resources.add_argument(
        "--node",
        choices=("primary", "standby", "logical"),
        help="show only one configured node",
    )

    health = commands.add_parser("health", help="check stand, storage, and replication health")
    _add_health_options(health)

    wait = commands.add_parser("wait", help="wait until all health checks pass")
    wait.add_argument("--timeout", type=_positive_float, default=180.0)
    wait.add_argument("--interval", type=_positive_float, default=1.0)
    _add_health_options(wait)

    watch = commands.add_parser(
        "watch", help="continuously show status, resource, and replication data"
    )
    _add_watch_options(watch)

    replication = commands.add_parser(
        "replication", help="observe physical and logical replication"
    )
    replication_commands = replication.add_subparsers(dest="replication_command", required=True)
    replication_commands.add_parser("status", help="show detailed replication and slot state")
    replication_watch = replication_commands.add_parser(
        "watch", help="continuously show replication state"
    )
    _add_watch_options(replication_watch)

    apply_config = commands.add_parser(
        "apply",
        help="apply the resolved YAML or preview it with --dry-run",
    )
    apply_mode = apply_config.add_mutually_exclusive_group()
    apply_mode.add_argument(
        "--reload",
        action="store_true",
        help="apply reloadable spec.postgres.parameters and reload every node",
    )
    apply_mode.add_argument(
        "--restart",
        action="store_true",
        help="recreate every node without clearing data to apply all settings and resources",
    )
    apply_config.add_argument(
        "--dry-run", action="store_true", help="show the plan without changing anything"
    )
    apply_config.add_argument(
        "--plan-hash",
        help="apply only if the current plan still matches this hash",
    )
    apply_config.add_argument(
        "--timeout", type=_positive_float, default=180.0, help="readiness timeout in seconds"
    )
    apply_config.add_argument(
        "--stop-timeout",
        type=_non_negative_int,
        default=30,
        help="container stop timeout in seconds",
    )

    stop = commands.add_parser("stop", help="stop stand containers and preserve all data")
    stop.add_argument(
        "--timeout",
        type=_non_negative_int,
        default=30,
        help="container stop timeout in seconds",
    )

    restart = commands.add_parser("restart", help="stop and start the stand")
    restart.add_argument(
        "--timeout", type=_positive_float, default=180.0, help="readiness timeout in seconds"
    )
    restart.add_argument(
        "--stop-timeout",
        type=_non_negative_int,
        default=30,
        help="container stop timeout in seconds",
    )

    recreate = commands.add_parser(
        "recreate", help="remove and recreate stand containers, optionally with a fresh cluster"
    )
    recreate.add_argument(
        "--clear-data",
        action="store_true",
        help="permanently clear data, WAL, and logs before creating the cluster",
    )
    recreate.add_argument(
        "--timeout", type=_positive_float, default=180.0, help="readiness timeout in seconds"
    )
    recreate.add_argument(
        "--stop-timeout",
        type=_non_negative_int,
        default=30,
        help="container stop timeout in seconds",
    )

    down = commands.add_parser("down", help="remove containers and the managed network")
    down.add_argument(
        "--clear-data",
        action="store_true",
        help="also permanently clear mounted data, WAL, and logs",
    )
    down.add_argument(
        "--timeout",
        type=_non_negative_int,
        default=30,
        help="container stop timeout in seconds",
    )

    storage = commands.add_parser("storage", help="manage host-mounted stand directories")
    storage_commands = storage.add_subparsers(dest="storage_command", required=True)
    storage_commands.add_parser("init", help="create and validate data, WAL, and log directories")
    storage_commands.add_parser("status", help="show paths, sizes, and detected PostgreSQL majors")
    storage_commands.add_parser("usage", help="show per-node usage and host filesystem capacity")
    storage_commands.add_parser(
        "clean", help="clear data, WAL, and logs; refuses while stand containers exist"
    )

    logical = commands.add_parser("logical", help="manage the all-tables logical replica")
    logical_commands = logical.add_subparsers(dest="logical_command", required=True)
    logical_sync = logical_commands.add_parser(
        "sync", help="discover new publisher tables and refresh or fully reseed the subscriber"
    )
    logical_sync.add_argument(
        "--full",
        action="store_true",
        help="drop the subscription and completely rebuild only the logical subscriber",
    )
    logical_sync.add_argument(
        "--timeout",
        type=_positive_float,
        default=180.0,
        help="logical readiness timeout in seconds",
    )
    logical_sync.add_argument(
        "--stop-timeout",
        type=_non_negative_int,
        default=30,
        help="logical container stop timeout in seconds",
    )

    standby = commands.add_parser("standby", help="manage the physical standby")
    standby_commands = standby.add_subparsers(dest="standby_command", required=True)
    reinit = standby_commands.add_parser(
        "reinit", help="rebuild the physical standby from the primary"
    )
    reinit.add_argument(
        "--force",
        action="store_true",
        required=True,
        help="confirm destruction of standby-only data",
    )
    reinit.add_argument("--timeout", type=_positive_float, default=180.0)
    reinit.add_argument("--stop-timeout", type=_non_negative_int, default=30)

    node = commands.add_parser("node", help="inspect or restart one configured node")
    node_commands = node.add_subparsers(dest="node_command", required=True)
    node_inspect = node_commands.add_parser(
        "inspect", help="show detailed container and PostgreSQL state"
    )
    node_inspect.add_argument("node", choices=("primary", "standby", "logical"))
    node_restart = node_commands.add_parser(
        "restart", help="restart one node and restore topology readiness"
    )
    node_restart.add_argument("node", choices=("primary", "standby", "logical"))
    node_restart.add_argument("--timeout", type=_positive_float, default=180.0)
    node_restart.add_argument("--stop-timeout", type=_non_negative_int, default=30)

    logs = commands.add_parser("logs", help="read and filter PostgreSQL CSV logs")
    logs.add_argument("node", choices=("primary", "standby", "logical", "all"))
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("--tail", type=_non_negative_int, default=200)
    logs.add_argument("--since", help="duration such as 30m or an ISO-8601 timestamp")
    logs.add_argument("--until", help="ISO-8601 timestamp")
    logs.add_argument(
        "--severity",
        type=str.upper,
        choices=(
            "DEBUG5",
            "DEBUG4",
            "DEBUG3",
            "DEBUG2",
            "DEBUG1",
            "INFO",
            "NOTICE",
            "WARNING",
            "ERROR",
            "LOG",
            "FATAL",
            "PANIC",
        ),
        help="minimum PostgreSQL severity",
    )
    logs.add_argument("--backend-type", help="exact backend_type filter")
    logs.add_argument("--contains", help="case-insensitive message/query substring")
    logs.add_argument("--format", choices=("raw", "pretty", "json"), default="raw")

    events = commands.add_parser("events", help="show Docker events for this pg-stand project only")
    events.add_argument("--since", default="10m", help="duration or ISO-8601 timestamp")
    events.add_argument("--until", help="duration or ISO-8601 timestamp")
    events.add_argument("--follow", action="store_true")
    events.add_argument("--count", type=_non_negative_int, default=0)
    events.add_argument("--json-lines", action="store_true")

    commands.add_parser(
        "doctor", help="run read-only host, Docker, image, port, and storage checks"
    )
    commands.add_parser(
        "capabilities", help="show version, image, extension, and topology capabilities"
    )

    dsn = commands.add_parser("dsn", help="print connection details for a node")
    dsn.add_argument("--node", choices=("primary", "standby", "logical"), default="primary")
    dsn.add_argument("--database")
    dsn.add_argument("--show-password", action="store_true")

    psql = commands.add_parser("psql", help="open the host psql client against a node")
    psql.add_argument("--node", choices=("primary", "standby", "logical"), default="primary")
    psql.add_argument("--database")

    ssh = commands.add_parser("ssh", help="open an SSH session to a node with the test key")
    ssh.add_argument("--node", choices=("primary", "standby", "logical"), default="primary")

    image = commands.add_parser("image", help="build or inspect managed pg-stand images")
    image_commands = image.add_subparsers(dest="image_command", required=True)
    image_commands.add_parser("list", help="list only pg-stand-owned managed images")
    image_commands.add_parser("inspect", help="inspect the image resolved by this config")
    image_commands.add_parser("build", help="force a managed image rebuild")

    cleanup = commands.add_parser(
        "cleanup", help="report or remove only resources owned by pg-stand"
    )
    cleanup_commands = cleanup.add_subparsers(dest="cleanup_command", required=True)
    cleanup_commands.add_parser(
        "status", help="show database directories, container layers, and managed image sizes"
    )
    cleanup_run = cleanup_commands.add_parser(
        "run", help="remove explicitly selected pg-stand resources"
    )
    cleanup_run.add_argument("--containers", action="store_true")
    cleanup_run.add_argument("--storage", action="store_true")
    cleanup_run.add_argument("--images", action="store_true")
    cleanup_run.add_argument(
        "--cache",
        action="store_true",
        help="prune only dangling images with pg-stand ownership labels",
    )
    cleanup_run.add_argument(
        "--all",
        action="store_true",
        help="select this project's containers, storage and credentials; images/cache are separate",
    )
    cleanup_run.add_argument(
        "--force",
        action="store_true",
        required=True,
        help="confirm the selected destructive cleanup",
    )
    cleanup_run.add_argument("--stop-timeout", type=_non_negative_int, default=30)

    sql = commands.add_parser("sql", help="run a SQL statement inside a stand node")
    sql.add_argument("statement")
    sql.add_argument("--node", choices=("primary", "standby", "logical"), default="primary")
    sql.add_argument("--database", help="database name; defaults to app_01")
    return parser
