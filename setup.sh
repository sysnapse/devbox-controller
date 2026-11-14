#!/usr/bin/env bash
# One-time (and idempotent) host setup for the devbox controller.
#
# It only touches things you would otherwise get wrong by hand:
#   1. .env exists, with a real DCA_TOKEN (32 bytes) and an absolute DCA_HOST_WORK_ROOT
#   2. DCA_HOST_WORK_ROOT exists and is owned by the uid the controller runs as
#   3. DOCKER_GID matches the host's docker socket group
#
# Safe to re-run: it keeps existing values and never overwrites a non-empty token.
set -euo pipefail

cd "$(dirname "$0")"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ---- what uid/gid does the controller run as? -------------------------------
# Must match controller/Dockerfile defaults; devbox templates use 1000:1000 too,
# so cloned files stay writable inside the devbox.
CTRL_UID="${DCA_UID:-1000}"
CTRL_GID="${DCA_GID:-1000}"

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

work="$(get_val DCA_HOST_WORK_ROOT || true)"
if [ -z "$work" ]; then
  work="$(pwd)/work"
  say "DCA_HOST_WORK_ROOT was empty, defaulting to $work"
fi
case "$work" in
  /*) : ;;
  *) die "DCA_HOST_WORK_ROOT must be an absolute path, got: $work" ;;
esac
set_val DCA_HOST_WORK_ROOT "$work"

# ---- work root ownership ---------------------------------------------------
# The controller cannot chown a host directory itself: it is unprivileged
# inside the container. Someone on the host has to do it once.
mkdir -p "$work" 2>/dev/null || true
if [ ! -d "$work" ]; then
  die "cannot create $work (parent not writable, try: sudo mkdir -p $work)"
fi

cur_uid="$(stat -c %u "$work")"
cur_gid="$(stat -c %g "$work")"
if [ "$cur_uid" = "$CTRL_UID" ] && [ "$cur_gid" = "$CTRL_GID" ]; then
  say "work root already owned by $CTRL_UID:$CTRL_GID"
else
  say "work root is $cur_uid:$cur_gid, needs $CTRL_UID:$CTRL_GID"
  if [ "$(id -u)" = "$CTRL_UID" ] && [ -w "$work" ]; then
    # Common case on pi5: your user IS uid 1000, same as the controller.
    say "your user is uid $CTRL_UID and can write it already; nothing to do"
  else
    chown_cmd="chown -R $CTRL_UID:$CTRL_GID $work"
    if [ "$(id -u)" = "0" ]; then
      sh -c "$chown_cmd"
      say "chowned $work to $CTRL_UID:$CTRL_GID"
    elif command -v sudo >/dev/null 2>&1; then
      say "running: sudo $chown_cmd"
      sudo sh -c "$chown_cmd"
    else
      die "need to run as root: $chown_cmd"
    fi
  fi
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
