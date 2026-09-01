#!/bin/sh
# grayson knowledge appliance entrypoint: validate configuration loudly,
# prepare git auth from mounted secrets, then exec the read-only knowledge
# MCP server. Every failure names its cause and its fix — a service that
# cannot start correctly must say why, not crash-loop on a downstream error.
set -eu

if [ -z "${GRAYSON_LIBRARY_URL:-}" ]; then
    echo "FATAL: GRAYSON_LIBRARY_URL is not set." >&2
    echo "Set it to the qa-library git URL this appliance serves" >&2
    echo "(e.g. git@your-git-host:your-org/qa-library.git), or to a mounted local path." >&2
    exit 1
fi

if [ "${GRAYSON_MCP_NO_TOKEN:-0}" = "1" ]; then
    # --token (via env) and --no-token are mutually exclusive at the CLI
    unset GRAYSON_MCP_TOKEN
elif [ -z "${GRAYSON_MCP_TOKEN:-}" ]; then
    echo "FATAL: GRAYSON_MCP_TOKEN is not set." >&2
    echo "A service must present a stable token, not mint a fresh one per restart." >&2
    echo "Set GRAYSON_MCP_TOKEN to a long random value, or GRAYSON_MCP_NO_TOKEN=1" >&2
    echo "ONLY behind a gateway that already authenticates every caller." >&2
    exit 1
fi

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh" 2>/dev/null || true
SSH_OPTS=""

# Deploy key: mount it read-only anywhere and point GRAYSON_DEPLOY_KEY at it
# (defaults cover /run/secrets and the legacy in-.ssh path). It is copied into
# place so ownership and 0600 permissions satisfy ssh under whatever UID the
# platform assigned this container.
KEY_SRC="${GRAYSON_DEPLOY_KEY:-}"
if [ -z "$KEY_SRC" ]; then
    for candidate in /run/secrets/grayson_deploy_key /home/grayson/.ssh/id_ed25519; do
        if [ -f "$candidate" ]; then
            KEY_SRC="$candidate"
            break
        fi
    done
fi
if [ -n "$KEY_SRC" ] && [ -f "$KEY_SRC" ]; then
    cp "$KEY_SRC" "$HOME/.ssh/deploy_key"
    chmod 600 "$HOME/.ssh/deploy_key"
    SSH_OPTS="-i $HOME/.ssh/deploy_key -o IdentitiesOnly=yes"
fi

# Host key policy: mount your git host's known_hosts (GRAYSON_KNOWN_HOSTS or
# /run/secrets/grayson_known_hosts) for strict checking; without one, the
# appliance trusts the host key on first use per container.
KNOWN_SRC="${GRAYSON_KNOWN_HOSTS:-/run/secrets/grayson_known_hosts}"
if [ -f "$KNOWN_SRC" ]; then
    cp "$KNOWN_SRC" "$HOME/.ssh/known_hosts"
    chmod 644 "$HOME/.ssh/known_hosts"
    SSH_OPTS="$SSH_OPTS -o StrictHostKeyChecking=yes"
else
    SSH_OPTS="$SSH_OPTS -o StrictHostKeyChecking=accept-new"
fi
export GIT_SSH_COMMAND="ssh $SSH_OPTS -o UserKnownHostsFile=$HOME/.ssh/known_hosts"

set -- grayson mcp serve --knowledge-only --http \
    --host "${GRAYSON_HOST:-0.0.0.0}" --port "${GRAYSON_PORT:-8850}" \
    --library "$GRAYSON_LIBRARY_URL"
if [ "${GRAYSON_MCP_NO_TOKEN:-0}" = "1" ]; then
    set -- "$@" --no-token
fi
exec "$@"
