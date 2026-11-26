#!/bin/sh
# Personal pytaiga-mcp keys, run on the Taiga host (needs docker compose access).
# usage: mcp-keys.sh issue <username> | list | revoke <username>
# TAIGA_COMPOSE_DIR: the compose project with the taiga-back service (default ~/control-plane).
set -e
script="$(cd "$(dirname "$0")" && pwd)/mcp_keys.py"
cd "${TAIGA_COMPOSE_DIR:-$HOME/control-plane}"
exec docker compose exec -T -e MCP_KEYS_ARGS="$*" taiga-back python manage.py shell < "$script"
