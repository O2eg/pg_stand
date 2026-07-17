#!/bin/sh
set -eu

CONFIG_ROOT=/etc/pgbouncer
RUNTIME_ROOT=/run/pg-stand-pgbouncer
AUTH_FILE=$CONFIG_ROOT/userlist.txt
HBA_FILE=$CONFIG_ROOT/pg-stand-hba.conf
IDENT_FILE=$CONFIG_ROOT/pg-stand-ident.conf
SESSION_CONFIG=$CONFIG_ROOT/pgbouncer-session.ini
TRANSACTION_CONFIG=$CONFIG_ROOT/pgbouncer-transaction.ini
SESSION_PORT=6432
TRANSACTION_PORT=6433

postgres_ready() {
  runuser -u postgres -- psql --dbname postgres --tuples-only --no-align \
    --command "select count(*) from pg_catalog.pg_roles where rolname ~ '^user_app_[0-9][0-9]$'" \
    2>/dev/null | grep -qx '10'
}

write_auth_file() {
  temporary=$RUNTIME_ROOT/userlist.txt.tmp
  superuser=${POSTGRES_USER:-postgres}
  runuser -u postgres -- psql --dbname postgres --tuples-only --no-align \
    --set ON_ERROR_STOP=1 \
    --command "select '\"' || replace(rolname, '\"', '\"\"') || '\" \"' ||
replace(rolpassword, '\"', '\"\"') || '\"'
from pg_catalog.pg_authid
where rolcanlogin
  and (rolname = '$superuser' or rolname ~ '^user_app_[0-9][0-9]$')
order by rolname" > "$temporary"
  test "$(wc -l < "$temporary")" -eq 11
  install -m 0600 -o postgres -g postgres "$temporary" "$AUTH_FILE"
  rm -f "$temporary"
}

write_tls_auth_files() {
  superuser=${POSTGRES_USER:-postgres}
  if [ "${PG_STAND_TLS_ENABLED:-0}" = "1" ]; then
    cat > "$HBA_FILE" <<'EOF'
hostnossl all all all reject
hostssl all all all cert map=pg_stand_tls
EOF
    printf 'pg_stand_tls postgres %s\n' "$superuser" > "$IDENT_FILE"
  else
    : > "$HBA_FILE"
    : > "$IDENT_FILE"
  fi
  chmod 0600 "$HBA_FILE" "$IDENT_FILE"
  chown postgres:postgres "$HBA_FILE" "$IDENT_FILE"
}

write_config() {
  mode=$1
  port=$2
  target=$3
  superuser=${POSTGRES_USER:-postgres}
  {
    printf '%s\n' '[databases]'
    printf '%s\n' 'postgres = host=/var/run/postgresql port=5432 dbname=postgres'
    index=1
    while [ "$index" -le 10 ]; do
      database=$(printf 'app_%02d' "$index")
      printf '%s = host=/var/run/postgresql port=5432 dbname=%s\n' "$database" "$database"
      index=$((index + 1))
    done
    printf '\n%s\n' '[pgbouncer]'
    printf 'pool_mode = %s\n' "$mode"
    printf 'listen_addr = %s\n' '0.0.0.0'
    printf 'listen_port = %s\n' "$port"
    printf 'auth_file = %s\n' "$AUTH_FILE"
    if [ "${PG_STAND_TLS_ENABLED:-0}" = "1" ]; then
      printf '%s\n' \
        'auth_type = hba' \
        "auth_hba_file = $HBA_FILE" \
        "auth_ident_file = $IDENT_FILE" \
        'client_tls_sslmode = verify-full' \
        'client_tls_protocols = secure' \
        'client_tls_ca_file = /pg-stand-tls/ca.crt' \
        'client_tls_cert_file = /pg-stand-tls/server.crt' \
        'client_tls_key_file = /pg-stand-tls/server.key'
    else
      printf '%s\n' \
        'auth_type = scram-sha-256' \
        'client_tls_sslmode = disable'
    fi
    printf '%s\n' \
      'server_tls_sslmode = disable' \
      "admin_users = $superuser" \
      "stats_users = $superuser" \
      'max_client_conn = 200' \
      'default_pool_size = 5' \
      'min_pool_size = 0' \
      'reserve_pool_size = 2' \
      'reserve_pool_timeout = 5' \
      'max_prepared_statements = 200' \
      'server_idle_timeout = 600' \
      'server_lifetime = 3600' \
      'server_connect_timeout = 15' \
      'server_login_retry = 15' \
      'query_wait_timeout = 120' \
      'client_login_timeout = 60' \
      'ignore_startup_parameters = extra_float_digits' \
      'tcp_keepalive = 1' \
      'tcp_keepidle = 60' \
      'tcp_keepintvl = 10' \
      'tcp_keepcnt = 6' \
      'log_connections = 1' \
      'log_disconnections = 1' \
      'log_pooler_errors = 1' \
      'log_stats = 1' \
      'stats_period = 60' \
      "logfile = /pglog/pgbouncer-$mode.log" \
      "pidfile = $RUNTIME_ROOT/pgbouncer-$mode.pid"
  } > "$target"
  chmod 0600 "$target"
  chown postgres:postgres "$target"
}

process_is_running() {
  pidfile=$1
  [ -s "$pidfile" ] || return 1
  pid=$(cat "$pidfile")
  kill -0 "$pid" 2>/dev/null
}

start_or_reload() {
  mode=$1
  config=$2
  pidfile=$RUNTIME_ROOT/pgbouncer-$mode.pid
  if process_is_running "$pidfile"; then
    kill -HUP "$(cat "$pidfile")"
    return 0
  fi
  rm -f "$pidfile"
  runuser -u postgres -- "$(command -v pgbouncer)" --daemon "$config"
}

configure_and_start() {
  install -d -m 0750 -o postgres -g postgres "$CONFIG_ROOT" "$RUNTIME_ROOT"
  write_auth_file
  write_tls_auth_files
  write_config session "$SESSION_PORT" "$SESSION_CONFIG"
  write_config transaction "$TRANSACTION_PORT" "$TRANSACTION_CONFIG"
  start_or_reload session "$SESSION_CONFIG"
  start_or_reload transaction "$TRANSACTION_CONFIG"
}

wait_and_start() {
  while ! postgres_ready; do
    sleep 1
  done
  configure_and_start
}

case "${1:-}" in
  wait-and-start)
    wait_and_start
    ;;
  refresh)
    if ! postgres_ready; then
      echo "PostgreSQL application roles are not ready" >&2
      exit 1
    fi
    configure_and_start
    ;;
  *)
    echo "usage: $0 {wait-and-start|refresh}" >&2
    exit 64
    ;;
esac
