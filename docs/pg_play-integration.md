# pg_play integration contract

This document is for orchestrator authors. The normal `pg-stand` lifecycle and
observation commands remain the supported human interface.

`pg_stand` supports the hidden `pg_play/component/v1` machine transport:

```bash
pg-stand --machine --request-id stand-001 --component-capabilities
pg-stand --machine --request-id stand-002 --config stand.yaml validate
pg-stand --machine --request-id stand-003 --config stand.yaml plan
```

The capability document uses `pg_play/capabilities/v1`. Every command declares
the common boolean fields `mutates_target`, `machine_output`, and
`accepts_plan_hash`. The public `capabilities` subcommand remains available.
The `machine_interface` object records the canonical hidden option names.

To plan a generated `pg_configurator` candidate without rewriting the stand
source, pass a JSON parameter mapping through standard input:

```bash
pg-stand --machine --request-id stand-004 \
  --config stand.yaml --parameters-json=- plan
```

Topology, TLS, fixed CSV logging, authentication, replication, and diagnostic
preload parameters listed by the public `MANAGED_POSTGRES_PARAMETERS` constant
remain owned by `pg_stand`. An orchestrator must keep those candidate values in
its plan for provenance and pass only the remaining parameter mapping here.

The plan returns its normalized `plan_hash` and a `required_action` of `none`,
`up`, `reload`, `restart`, or `blocked`. A mutating reload/restart must carry
the reviewed hash:

```bash
pg-stand --machine --request-id stand-005 \
  --config stand.yaml --parameters-json=- \
  apply --restart --plan-hash sha256:...
```

Interactive and unbounded operations are unavailable in machine mode,
including `psql`, `ssh`, `watch`, followed log/event streams, and
`dsn --show-password`. Machine results always redact credentials. Ownership
checks and the normal standalone safety rules still apply.
