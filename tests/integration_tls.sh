#!/bin/sh
set -eu

PG_STAND_BIN=${PG_STAND_BIN:-.venv/bin/pg-stand}
PG_STAND_POSTGRES_VERSION=${PG_STAND_POSTGRES_VERSION:-}
SINGLE_CONFIG=src/pg_stand/configs/single.yaml
FULL_CONFIG=src/pg_stand/configs/primary-standby-sync-logical.yaml
ACTIVE_CONFIG=

pg_stand() {
  if [ -n "$PG_STAND_POSTGRES_VERSION" ]; then
    "$PG_STAND_BIN" --postgres-version "$PG_STAND_POSTGRES_VERSION" \
      --tls -c "$ACTIVE_CONFIG" "$@"
  else
    "$PG_STAND_BIN" --tls -c "$ACTIVE_CONFIG" "$@"
  fi
}

cleanup() {
  if [ -n "$ACTIVE_CONFIG" ]; then
    pg_stand down --clear-data --timeout 20 || true
  fi
}
trap cleanup EXIT INT TERM

run_tls_psql() {
  container=$1
  shift
  docker exec \
    -e PGSSLMODE=verify-ca \
    -e PGSSLROOTCERT=/pg-stand-tls/ca.crt \
    -e PGSSLCERT=/pg-stand-tls/postgres.crt \
    -e PGSSLKEY=/pg-stand-tls/postgres.key \
    "$container" \
    psql --host 127.0.0.1 --port 5432 --username postgres --dbname app_01 \
    --no-align --tuples-only --set ON_ERROR_STOP=1 "$@"
}

run_tls_pgbouncer() {
  container=$1
  port=$2
  shift 2
  docker exec \
    -e PGSSLMODE=verify-ca \
    -e PGSSLROOTCERT=/pg-stand-tls/ca.crt \
    -e PGSSLCERT=/pg-stand-tls/postgres.crt \
    -e PGSSLKEY=/pg-stand-tls/postgres.key \
    "$container" \
    psql --host 127.0.0.1 --port "$port" --username postgres --dbname app_01 \
    --no-align --tuples-only --set ON_ERROR_STOP=1 "$@"
}

pgbouncer_pool_mode() {
  container=$1
  port=$2
  docker exec \
    -e PGSSLMODE=verify-ca \
    -e PGSSLROOTCERT=/pg-stand-tls/ca.crt \
    -e PGSSLCERT=/pg-stand-tls/postgres.crt \
    -e PGSSLKEY=/pg-stand-tls/postgres.key \
    "$container" \
    psql --host 127.0.0.1 --port "$port" --username postgres --dbname pgbouncer \
    --no-align --tuples-only --set ON_ERROR_STOP=1 --command "show config" \
    | awk -F '|' '$1 == "pool_mode" {print $2}'
}

ACTIVE_CONFIG=$SINGLE_CONFIG
pg_stand recreate --clear-data --timeout 300
pg_stand health

run_tls_psql pg-stand-single-primary-pg-stand-managed \
  --command "select ssl from pg_stat_ssl where pid = pg_backend_pid()"
run_tls_pgbouncer pg-stand-single-primary-pg-stand-managed 6432 \
  --command "select current_database() || '|session|' || current_user" \
  | grep -Fx 'app_01|session|postgres'
run_tls_pgbouncer pg-stand-single-primary-pg-stand-managed 6433 \
  --command "select current_database() || '|transaction|' || current_user" \
  | grep -Fx 'app_01|transaction|postgres'
pgbouncer_pool_mode pg-stand-single-primary-pg-stand-managed 6432 | grep -Fx 'session'
pgbouncer_pool_mode pg-stand-single-primary-pg-stand-managed 6433 | grep -Fx 'transaction'
database_inventory=$(run_tls_psql pg-stand-single-primary-pg-stand-managed \
  --command "select d.datname || '|' || r.rolname from pg_database d \
join pg_roles r on r.oid = d.datdba where d.datname ~ '^app_[0-9][0-9]$' order by d.datname")
expected_inventory=$(printf '%s\n' \
  'app_01|user_app_01' 'app_02|user_app_02' 'app_03|user_app_03' \
  'app_04|user_app_04' 'app_05|user_app_05' 'app_06|user_app_06' \
  'app_07|user_app_07' 'app_08|user_app_08' 'app_09|user_app_09' \
  'app_10|user_app_10')
if [ "$database_inventory" != "$expected_inventory" ]; then
  echo "application database inventory is unexpected" >&2
  printf '%s\n' "$database_inventory" >&2
  exit 1
fi
run_tls_psql pg-stand-single-primary-pg-stand-managed \
  --command "select count(*) = 10 and count(distinct rolpassword) = 10 \
from pg_authid where rolname ~ '^user_app_[0-9][0-9]$'" | grep -Eq '^(t|true)$'

if docker exec \
  -e PGSSLMODE=disable \
  -e PGPASSWORD=plaintext-must-be-rejected \
  pg-stand-single-primary-pg-stand-managed \
  psql --host 127.0.0.1 --username postgres --dbname app_01 --command "select 1"; then
  echo "plaintext TCP was unexpectedly accepted" >&2
  exit 1
fi

for pgbouncer_port in 6432 6433; do
  if docker exec \
    -e PGSSLMODE=disable \
    -e PGPASSWORD=plaintext-must-be-rejected \
    pg-stand-single-primary-pg-stand-managed \
    psql --host 127.0.0.1 --port "$pgbouncer_port" --username postgres \
    --dbname app_01 --command "select 1"; then
    echo "plaintext PgBouncer TCP on port $pgbouncer_port was unexpectedly accepted" >&2
    exit 1
  fi
  if docker exec \
    -e PGSSLMODE=verify-ca \
    -e PGSSLROOTCERT=/pg-stand-tls/ca.crt \
    pg-stand-single-primary-pg-stand-managed \
    psql --host 127.0.0.1 --port "$pgbouncer_port" --username postgres \
    --dbname app_01 --command "select 1"; then
    echo "PgBouncer TLS without a client certificate was unexpectedly accepted on port $pgbouncer_port" >&2
    exit 1
  fi
done

if docker exec \
  -e PGSSLMODE=verify-ca \
  -e PGSSLROOTCERT=/pg-stand-tls/ca.crt \
  pg-stand-single-primary-pg-stand-managed \
  psql --host 127.0.0.1 --username postgres --dbname app_01 --command "select 1"; then
  echo "TLS without a client certificate was unexpectedly accepted" >&2
  exit 1
fi

docker exec \
  -e PGSSLMODE=verify-ca \
  -e PGSSLROOTCERT=/pg-stand-tls/ca.crt \
  -e PGSSLCERT=/pg-stand-tls/postgres.crt \
  -e PGSSLKEY=/pg-stand-tls/postgres.key \
  pg-stand-single-primary-pg-stand-managed \
  pgbench --host 127.0.0.1 --port 5432 --username postgres --initialize --scale 1 app_01

docker exec \
  -e PGSSLMODE=verify-ca \
  -e PGSSLROOTCERT=/pg-stand-tls/ca.crt \
  -e PGSSLCERT=/pg-stand-tls/postgres.crt \
  -e PGSSLKEY=/pg-stand-tls/postgres.key \
  pg-stand-single-primary-pg-stand-managed \
  pgbench --host 127.0.0.1 --port 5432 --username postgres \
  --client 2 --jobs 1 --time 5 app_01

pg_stand down --clear-data --timeout 20
ACTIVE_CONFIG=$FULL_CONFIG
pg_stand recreate --clear-data --timeout 300
pg_stand health

run_tls_psql pg-stand-sync-logical-primary-pg-stand-managed \
  --command "create table tls_ci_probe (id bigint primary key, payload text not null); \
insert into tls_ci_probe values (1, 'certificate-only')"
docker exec pg-stand-sync-logical-primary-pg-stand-managed \
  psql --username postgres --dbname app_02 --set ON_ERROR_STOP=1 \
  --command "create table logical_exclusion_probe (id bigint primary key); \
insert into logical_exclusion_probe values (1)"
pg_stand logical sync --timeout 180

attempt=0
while [ "$attempt" -lt 30 ]; do
  standby_value=$(run_tls_psql pg-stand-sync-logical-standby-pg-stand-managed \
    --command "select payload from tls_ci_probe where id = 1" || true)
  logical_value=$(run_tls_psql pg-stand-sync-logical-subscriber-pg-stand-managed \
    --command "select payload from tls_ci_probe where id = 1" || true)
  if [ "$standby_value" = "certificate-only" ] && [ "$logical_value" = "certificate-only" ]; then
    break
  fi
  attempt=$((attempt + 1))
  sleep 1
done

if [ "${standby_value:-}" != "certificate-only" ]; then
  echo "physical standby did not receive the TLS probe row" >&2
  exit 1
fi
if [ "${logical_value:-}" != "certificate-only" ]; then
  echo "logical subscriber did not receive the TLS probe row" >&2
  exit 1
fi
docker exec pg-stand-sync-logical-standby-pg-stand-managed \
  psql --username postgres --dbname app_02 --no-align --tuples-only \
  --command "select count(*) from logical_exclusion_probe" | grep -Fx '1'
docker exec pg-stand-sync-logical-subscriber-pg-stand-managed \
  psql --username postgres --dbname app_02 --no-align --tuples-only \
  --command "select pg_catalog.to_regclass('public.logical_exclusion_probe') is null" \
  | grep -Eq '^(t|true)$'

client_dn_column=$(docker exec pg-stand-sync-logical-primary-pg-stand-managed \
  psql --username postgres --dbname app_01 --no-align --tuples-only \
  --command "select case when exists (select 1 from pg_attribute \
where attrelid = 'pg_catalog.pg_stat_ssl'::regclass and attname = 'client_dn') \
then 'client_dn' else 'clientdn' end")
client_dns=$(docker exec pg-stand-sync-logical-primary-pg-stand-managed \
  psql --username postgres --dbname app_01 --no-align --tuples-only \
  --command "select a.usename || '|' || s.ssl::text || '|' || s.$client_dn_column \
from pg_stat_ssl s join pg_stat_activity a using (pid) \
where a.backend_type = 'walsender' order by a.usename")
printf '%s\n' "$client_dns"
printf '%s\n' "$client_dns" | grep -F "postgres|true|/CN=postgres/O=pg-stand"
printf '%s\n' "$client_dns" | grep -F "replicator|true|/CN=replicator/O=pg-stand"
run_tls_psql pg-stand-sync-logical-primary-pg-stand-managed \
  --command "select database from pg_replication_slots \
where slot_name = 'pg_stand_all_tables_slot'" | grep -Fx 'app_01'

pg_stand down --clear-data --timeout 20
ACTIVE_CONFIG=
