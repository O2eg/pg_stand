# pg_play integration contract

This document is for orchestrator authors. The normal `pg-stand` lifecycle and
observation commands remain the supported human interface.

`pg_stand` supports the hidden `pg_play/component/v1` machine transport:

```bash
pg-stand --machine --request-id stand-001 --component-capabilities
pg-stand --machine --request-id stand-002 --config stand.yaml validate
pg-stand --machine --request-id stand-003 --config stand.yaml plan
```

## Transport and response format

`--json` selects command-specific JSON output for the standalone CLI. `--machine` wraps
the result in a versioned envelope and maps outcomes to the machine exit codes below.
Use `--machine` for orchestration; do not treat standalone `--json` as the same protocol.
Global options precede the subcommand. `--request-id` is echoed unchanged and is `null`
when omitted; it is a correlation identifier, not an idempotency key.

For example, a successful `--machine --request-id discovery-001 list` with no active
leases returns the following envelope and exits with code 0:

```json
{
  "contract_version": "pg_play/component/v1",
  "component": "pg_stand",
  "component_version": "0.3.0",
  "command": "list",
  "request_id": "discovery-001",
  "status": "succeeded",
  "result": {"stands": []},
  "artifacts": [],
  "warnings": [],
  "error": null
}
```

The component version reflects the installed package. `result` is command-specific;
non-JSON command output is represented as `{"stdout": "..."}`. When configuration loading
succeeds, `artifacts` includes a `StandConfiguration` descriptor with `schema_version`,
normalized configuration `hash`, and absolute source `path`. This descriptor does not
mean that a snapshot file was written. `warnings` is an array, currently normally empty.

A rejected `--machine --request-id unsupported-001 psql` returns this envelope and exits
with code 4, before loading configuration or connecting to Docker:

```json
{
  "contract_version": "pg_play/component/v1",
  "component": "pg_stand",
  "component_version": "0.3.0",
  "command": "psql",
  "request_id": "unsupported-001",
  "status": "failed",
  "result": null,
  "artifacts": [],
  "warnings": [],
  "error": {
    "code": "unsupported",
    "message": "interactive or unbounded streaming command is not available in machine mode"
  }
}
```

The CLI buffers command output and emits one JSON envelope after completion. Consumers
must impose a process timeout, inspect the exit code and `status`, and then interpret
`result` and `error`. A failed health check or blocked plan can have `error: null`, with
the explanation in `result`. Argument-parser errors occur before the machine wrapper:
they exit with code 2 and print usage to stderr without an envelope. Forced termination
or an unexpected process failure can also leave no envelope; handle that as a transport
failure rather than assuming an empty result means success.

## Exit codes

Capabilities advertise these names. Current behavior has the following qualifications:

| Code | Contract name | Current behavior |
|---|---|---|
| 0 | `success` | Command completed successfully; inspect the result, including the action proposed by `plan` |
| 2 | `validation_error` | Argument-parser errors; also retained for unavailable health results and some failed `apply --dry-run` results |
| 3 | `precondition_failed` | A blocked `plan` or a `PreconditionError`, including a stale supplied plan hash or a held project operation lock |
| 4 | `unsupported` | A command explicitly rejected by the machine wrapper |
| 5 | `partial` | Standalone exit code 1 is mapped to this code, including degraded health or `active` finding no lease |
| 6 | `execution_error` | Handled configuration, runtime, and Docker errors; configuration validation errors currently use this code too |
| 7 | `cancelled` | A caught keyboard interrupt |
| 8 | `ownership_error` | Advertised category; ownership failures currently use the general error handling rather than a dedicated code 8 |

Do not infer successful mutation from process completion alone. A rejected or interrupted
lifecycle command may have created some resources; inspect the stand before retrying.
See [recovery](recovery.md) for partial startup and ownership conflicts.

## Capabilities and reviewed plans

The capability document uses `pg_play/capabilities/v1`. Every command declares
the common boolean fields `mutates_target`, `machine_output`, and
`accepts_plan_hash`. The public `capabilities` subcommand remains available.
The `machine_interface` object records the canonical hidden option names. Use the
advertised `commands` as the orchestrator's allowlist; other CLI commands that happen to
pass the wrapper are not automatically part of that advertised command set.

To plan a generated `pg_configurator` candidate without rewriting the stand
source, save a JSON parameter mapping and pass it through standard input. Reuse the same
mapping for planning and application:

```bash
printf '%s\n' '{"work_mem":"16MB"}' > candidate-parameters.json
pg-stand --machine --request-id stand-004 \
  --config stand.yaml --parameters-json=- plan \
  < candidate-parameters.json > reviewed-plan.json
```

Topology, TLS, fixed CSV logging, authentication, replication, and diagnostic
preload parameters listed by the public `MANAGED_POSTGRES_PARAMETERS` constant
remain owned by `pg_stand`. An orchestrator must keep those candidate values in
its plan for provenance and pass only the remaining parameter mapping here.

The plan returns its normalized `plan_hash` and a `required_action` of `none`,
`up`, `reload`, `restart`, or `blocked`. Check the process exit code, envelope status, and
proposed action in `reviewed-plan.json` before applying. For a reviewed restart:

```bash
pg_stand_plan_hash=$(jq -er '.result.plan_hash' reviewed-plan.json)
pg-stand --machine --request-id stand-005 \
  --config stand.yaml --parameters-json=- \
  apply --restart --plan-hash "$pg_stand_plan_hash" < candidate-parameters.json
```

Use `--reload` when that is the reviewed action. Passing the reviewed hash is an
orchestrator requirement, not a CLI-enforced requirement: the option is currently
optional, even with `--machine`. If provided, the CLI recomputes the current plan and
rejects a mismatch before calling apply. Without it, there is no stale-plan comparison.
Keep the same project, configuration, version/TLS overrides, and candidate mapping.
Only `apply` accepts `--plan-hash`; do not assume it protects `up`, `stop`, or `down`.

## Streaming and secret handling

The machine wrapper explicitly rejects `psql`, `ssh`, top-level `watch`, followed
log/event streams, and `dsn --show-password`. There is a current gap: nested
`replication watch` is accepted, and its default `--count 0` never completes. Its output
is buffered in memory instead of delivered as samples. Do not invoke it through machine
mode. Use bounded `replication status` calls, or an advertised observation command, and
apply an orchestrator timeout. For terminal streaming use the standalone CLI.

The `dsn` command masks its password unless explicitly requested, and configuration
password fields are rejected by the parser. There is no universal redaction pass over
machine envelopes. SQL results, log contents, free-form configuration values, and error
text can contain sensitive data. The advertised
`secret_policy.machine_output_redacts_secrets` flag overstates this protection in the
current implementation; consumers must not treat it as a guarantee for arbitrary output.
Control which commands and inputs are allowed, and protect or redact captured artifacts
before sharing them. Ownership checks and the normal standalone safety rules still apply.

## Independent stand projects

Version 0.3 advertises `stand_isolation` in capabilities. `--project DIR` selects
the same project boundary as running the component with that working directory,
so existing pg_play manifests need no command-line migration. Each directory has
one active lease; independent directories may remain active simultaneously.
Container/network names and host ports must still be unique or disjoint.

The read-only machine commands `list` and `active` expose stand discovery.
`list` returns `{"stands": [...]}`; `active` retains the existing single-stand
result and rejects an ambiguous selection. Use `--project DIR` or
`active --name NAME` to select one stand. Plans include `instance_id` in their
hash, so supplying a reviewed hash rejects a plan from a different project directory.

For standalone migration publishers, use `spec.postgres.logical_decoding` on a
single-node profile. `wal_level` remains a managed parameter and must not be sent
in `--parameters-json`. `spec.docker.external_networks` optionally connects nodes
to existing caller-owned networks, providing a route between migration endpoints.
See [multiple stands](multiple-stands.md) for connection and cleanup semantics.
