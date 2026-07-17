#!/bin/sh
set -eu

TLS_SOURCE_ROOT=/pg-stand-tls-source
TLS_RUNTIME_ROOT=/pg-stand-tls
SSH_SOURCE_ROOT=/pg-stand-ssh-source
AUTH_TEMPLATE=/tmp/pg-stand-pg_hba.conf
IDENT_TEMPLATE=/tmp/pg-stand-pg_ident.conf

prepare_ssh_authorized_key() {
  if [ ! -f "$SSH_SOURCE_ROOT/pg_stand_test.pub" ]; then
    echo "missing generated pg-stand SSH public key" >&2
    exit 78
  fi
  install -d -m 0700 -o root -g root /root/.ssh
  install -m 0600 -o root -g root \
    "$SSH_SOURCE_ROOT/pg_stand_test.pub" /root/.ssh/authorized_keys
}

prepare_tls_files() {
  [ "${PG_STAND_TLS_ENABLED:-0}" = "1" ] || return 0
  for name in ca.crt server.crt server.key postgres.crt postgres.key replicator.crt replicator.key; do
    if [ ! -f "$TLS_SOURCE_ROOT/$name" ]; then
      echo "missing pg-stand TLS fixture: $TLS_SOURCE_ROOT/$name" >&2
      exit 78
    fi
  done
  install -d -m 0700 -o postgres -g postgres "$TLS_RUNTIME_ROOT"
  for name in ca.crt server.crt postgres.crt replicator.crt; do
    install -m 0644 -o postgres -g postgres \
      "$TLS_SOURCE_ROOT/$name" "$TLS_RUNTIME_ROOT/$name"
  done
  for name in server.key postgres.key replicator.key; do
    install -m 0600 -o postgres -g postgres \
      "$TLS_SOURCE_ROOT/$name" "$TLS_RUNTIME_ROOT/$name"
  done
}

write_auth_templates() {
  replication_user=${PG_STAND_REPLICATION_USER:-}
  superuser=${POSTGRES_USER:-postgres}
  rm -f "$AUTH_TEMPLATE" "$IDENT_TEMPLATE"
  if [ "${PG_STAND_TLS_ENABLED:-0}" = "1" ]; then
    {
      printf '%s\n' '# Managed by pg-stand: TCP requires TLS client certificates.'
      printf '%s\n' 'local all all trust'
      printf '%s\n' 'local replication all trust'
      printf '%s\n' 'hostnossl all all 0.0.0.0/0 reject'
      printf '%s\n' 'hostnossl all all ::/0 reject'
      if [ -n "$replication_user" ]; then
        printf 'hostssl replication %s 0.0.0.0/0 cert map=pg_stand_tls\n' "$replication_user"
        printf 'hostssl replication %s ::/0 cert map=pg_stand_tls\n' "$replication_user"
      fi
      printf '%s\n' 'hostssl all all 0.0.0.0/0 cert map=pg_stand_tls'
      printf '%s\n' 'hostssl all all ::/0 cert map=pg_stand_tls'
      printf '%s\n' 'host all all 0.0.0.0/0 reject'
      printf '%s\n' 'host all all ::/0 reject'
    } > "$AUTH_TEMPLATE"
    {
      printf 'pg_stand_tls postgres %s\n' "$superuser"
      if [ -n "$replication_user" ]; then
        printf 'pg_stand_tls replicator %s\n' "$replication_user"
      fi
    } > "$IDENT_TEMPLATE"
  else
    {
      printf '%s\n' '# Managed by pg-stand: password-authenticated TCP.'
      printf '%s\n' 'local all all trust'
      printf '%s\n' 'local replication all trust'
      if [ -n "$replication_user" ]; then
        printf 'host replication %s 0.0.0.0/0 scram-sha-256\n' "$replication_user"
        printf 'host replication %s ::/0 scram-sha-256\n' "$replication_user"
      fi
      printf '%s\n' 'host all all 0.0.0.0/0 scram-sha-256'
      printf '%s\n' 'host all all ::/0 scram-sha-256'
    } > "$AUTH_TEMPLATE"
    : > "$IDENT_TEMPLATE"
  fi
  chmod 0600 "$AUTH_TEMPLATE" "$IDENT_TEMPLATE"
  chown postgres:postgres "$AUTH_TEMPLATE" "$IDENT_TEMPLATE"
}

install_auth_files() {
  if [ -s "$PGDATA/PG_VERSION" ]; then
    install -m 0600 -o postgres -g postgres "$AUTH_TEMPLATE" "$PGDATA/pg_hba.conf"
    install -m 0600 -o postgres -g postgres "$IDENT_TEMPLATE" "$PGDATA/pg_ident.conf"
    return 0
  fi
  install -d -m 0755 /docker-entrypoint-initdb.d
  cat > /docker-entrypoint-initdb.d/pg-stand-auth.sh <<'EOF'
#!/bin/sh
set -eu
install -m 0600 /tmp/pg-stand-pg_hba.conf "$PGDATA/pg_hba.conf"
install -m 0600 /tmp/pg-stand-pg_ident.conf "$PGDATA/pg_ident.conf"
EOF
  chmod 0755 /docker-entrypoint-initdb.d/pg-stand-auth.sh
}

prepare_tls_files
prepare_ssh_authorized_key
write_auth_templates
install_auth_files
ssh-keygen -A
/usr/sbin/sshd
/usr/local/bin/pg-stand-pgbouncer wait-and-start &

exec /usr/local/bin/docker-entrypoint.sh "$@"
