# Sourced by env.sh and the s6 scripts: which Postgres the image uses (tech-stack decisions 52, 73).
# External when the container's environment sets FACTORIAL_DATABASE_URL to a host that is not
# loopback, as an ECS task does for RDS; the image's own otherwise. A URL only the mounted settings
# file names never decides it: init reads the environment, and env.sh replaces such a URL unless it
# names the compose database service. `init` decides while the URL is still in the environment and
# records the answer as FACTORIAL_IMAGE_DATABASE, which every later script and service reads, and
# an external database's address in the file below, for db-ready.
# The image's own database runs in this container unless FACTORIAL_IMAGE_DATABASE_HOST names the
# compose service it runs in instead, as `make up` does. Its role and password are the ones the
# URL in the public compose's factorial.env names (postgres-standalone); with no URL there, as
# under `make up` or in one container, they are the image's `factory`, which nobody guards.

factorial_database_address_file=/run/factorial-database

factorial_external_database() {
  [ "${FACTORIAL_IMAGE_DATABASE:-}" = external ]
}

factorial_own_database_host() {
  printf '%s\n' "${FACTORIAL_IMAGE_DATABASE_HOST:-127.0.0.1}"
}

factorial_runs_postgres() {
  ! factorial_external_database && [ -z "${FACTORIAL_IMAGE_DATABASE_HOST:-}" ]
}

# `host port` when the URL names a host that is not loopback; nothing otherwise.
factorial_database_address() {
  python3 -I -c '
import ipaddress
import sys
from urllib.parse import urlsplit

url = urlsplit(sys.argv[1])
host = url.hostname or ""
try:
    loopback = ipaddress.ip_address(host).is_loopback
except ValueError:
    loopback = host in ("", "localhost")
if not loopback:
    print(host, url.port or 5432)
' "$1"
}
