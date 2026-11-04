# Sourced by init-db and postgres-standalone, as root, at every start. On a fresh volume only, a
# cluster whose superuser, password and database match the URL the app reaches it with: the URL a
# settings file names (postgres-standalone passes the mounted factorial.env), or `factory`'s when
# none is given or the file names no URL. The superuser is recorded beside the cluster, so a start
# with another URL's user stops and says so, where the app would otherwise fail its sign-in
# saying nothing of why. A volume made before the record was made for `factory`. `return`, never
# `exit`: the standalone entrypoint goes on to start the server.

factorial_role_file=/data/postgres/factorial-role

# Writes the URL's password to $1 and the statement that makes its database to $2, and prints its
# user. $3, when given, is the settings file.
factorial_cluster_owner() {
  python -I -c '
import sys
from urllib.parse import unquote, urlsplit
from dotenv import dotenv_values

named = ""
if len(sys.argv) > 3:
    named = (dotenv_values(sys.argv[3]).get("FACTORIAL_DATABASE_URL") or "").strip()
url = urlsplit(named or "postgresql://factory:factory@127.0.0.1:5432/factory")
user, password = unquote(url.username or ""), unquote(url.password or "")
database = unquote(url.path.lstrip("/"))
for name, value in (("user", user), ("password", password), ("database", database)):
    if not value or "\n" in value or "\0" in value:
        sys.exit(f"factorial: FACTORIAL_DATABASE_URL names no {name}, or one with a line break")
with open(sys.argv[1], "w") as handle:
    handle.write(password)
with open(sys.argv[2], "w") as handle:
    handle.write("CREATE DATABASE \"%s\";\n" % database.replace("\"", "\"\""))
print(user)
' "$@"
}

# Fails, saying why, when the cluster on the volume was made for another user than $1.
factorial_check_role() {
  made_for=factory
  [ -s "$factorial_role_file" ] && made_for="$(cat "$factorial_role_file")"
  [ "$made_for" = "$1" ] && return 0
  echo "factorial: this volume's database was made for role $made_for, but factorial.env names $1. Use the factorial.env this volume was made with, or remove the volume to start over." >&2
  return 1
}

factorial_initdb() {
  bin=/usr/lib/postgresql/17/bin
  pwfile="$(mktemp)"
  create="$(mktemp)"
  if ! user="$(factorial_cluster_owner "$pwfile" "$create" "$@")"; then
    rm -f "$pwfile" "$create"
    return 1
  fi
  if [ -s /data/postgres/PG_VERSION ]; then
    rm -f "$pwfile" "$create"
    factorial_check_role "$user"
    return
  fi
  chown postgres "$pwfile"
  if /command/s6-setuidgid postgres "$bin/initdb" -D /data/postgres -U "$user" \
    --pwfile="$pwfile" -E UTF8 --locale=C.UTF-8 --auth-local=peer --auth-host=scram-sha-256 &&
    /command/s6-setuidgid postgres "$bin/postgres" --single -D /data/postgres postgres \
      < "$create" >/dev/null; then
    rm -f "$pwfile" "$create"
  else
    rm -f "$pwfile" "$create"
    return 1
  fi
  printf '%s\n' "$user" > "$factorial_role_file"
  chown postgres:postgres "$factorial_role_file"
}
