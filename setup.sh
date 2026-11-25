#!/usr/bin/env bash
# One-time (and idempotent) host setup for the devbox controller.
#
# It only touches things you would otherwise get wrong by hand:
#   1. .env exists, with a real DCA_TOKEN (32 bytes)
#   2. DOCKER_GID matches the host's docker socket group
#
# 2026-11-25: there is no host work root any more, so no directory ownership fix.
# Devboxes own an ephemeral in-container workspace; `dc rm` / `dc gc` removes it.
# Safe to re-run: it keeps existing values and never overwrites a non-empty token.
set -euo pipefail

cd "$(dirname "$0")"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ---- .env ------------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  say "created .env from .env.example"
fi

get_val() { sed -n "s/^$1=//p" .env | head -n1; }
set_val() { # set_val KEY VALUE  (replace or append; VALUE must be sed-safe)
  if grep -q "^$1=" .env; then
    sed -i "s|^$1=.*|$1=$2|" .env
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}

tok="$(get_val DCA_TOKEN || true)"
if [ -z "$tok" ] || [ "${#tok}" -lt 16 ]; then
  if command -v openssl >/dev/null 2>&1; then
    tok="$(openssl rand -hex 32)"
  else
    tok="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  fi
  set_val DCA_TOKEN "$tok"
  say "generated DCA_TOKEN (${#tok} chars)"
else
  say "kept existing DCA_TOKEN"
fi

# ---- docker socket group ---------------------------------------------------
sock="${DCA_DOCKER_SOCK:-/var/run/docker.sock}"
[ -S "$sock" ] || die "$sock not found; is docker running?"
want_gid="$(stat -c %g "$sock")"
have_gid="$(get_val DOCKER_GID || true)"
if [ "$have_gid" = "$want_gid" ]; then
  say "DOCKER_GID already $want_gid"
else
  set_val DOCKER_GID "$want_gid"
  say "set DOCKER_GID=$want_gid (from $sock)"
fi

# ---- sanity ----------------------------------------------------------------
a="$(get_val DCA_BIND || true)"
[ -n "$a" ] || set_val DCA_BIND 0.0.0.0
p="$(get_val DCA_PORT || true)"
if [ "$p" = "8080" ]; then
  set_val DCA_PORT 47123
  say "migrated DCA_PORT 8080 -> 47123 (8080 is too commonly taken)"
fi

command -v docker >/dev/null 2>&1 || die "docker CLI not found"
docker compose version >/dev/null 2>&1 || die "docker compose v2 not found"

say ""
say "OK. Next:"
say "  docker compose up -d --build"
say "  docker compose logs -f controller     # expect: listening on 0.0.0.0:47123"
say ""
say "Note: devboxes keep their workspace inside the container only. Use"
say "  dc rm <task>  or  dc gc  to remove a box and everything it built."
