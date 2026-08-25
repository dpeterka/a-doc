#!/usr/bin/env bash
# scripts/local-env.sh — shared implementation behind the *-local wrapper
# scripts (start-local, stop-local, restart-local, user-create-local,
# user-list-local). Not normally invoked directly; each wrapper is a thin
# `exec "$(dirname ...)/local-env.sh" <verb> "$@"`.
#
# SAFE STORE: `ADOC_SAFE_STORE` (default `$HOME/a-doc-data-local`) is an
# a-doc data repo this tool treats as READ-ONLY — it is only ever read from
# or `git clone`d, NEVER written to, deleted, or committed into by anything
# below. The working copy this tool creates/manages lives at `--dir`
# (default `$HOME/a-doc-data-test`) and is the only thing these scripts ever
# mutate.
#
# Every command here is safe to run from any directory — nothing depends on
# the caller's CWD.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"

DEFAULT_WORKDIR="$HOME/a-doc-data-test"
DEFAULT_PORT=8000
SAFE_STORE="${ADOC_SAFE_STORE:-$HOME/a-doc-data-local}"
STATE_ROOT="${ADOC_LOCAL_DEV_STATE_DIR:-$HOME/.local/state/a-doc-local-dev}"
HEALTHZ_TIMEOUT_ITERS=60 # * 0.5s = ~30s

QUIET=0

# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------

info() {
  [ "$QUIET" -eq 1 ] && return 0
  printf '%s\n' "$*"
}

out() {
  printf '%s\n' "$*"
}

err() {
  printf '%s\n' "$*" >&2
}

# --------------------------------------------------------------------------
# path / process helpers
# --------------------------------------------------------------------------

abs_path() {
  local p="$1"
  if command -v realpath >/dev/null 2>&1; then
    realpath -m -- "$p"
  else
    python3 - "$p" <<'PY'
import os
import sys

print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
  fi
}

resolve_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  if [ -x "$HOME/.local/bin/uv" ]; then
    printf '%s\n' "$HOME/.local/bin/uv"
    return 0
  fi
  return 1
}

instance_state_dir() {
  local workdir_abs="$1"
  local slug
  slug=$(printf '%s' "$workdir_abs" | sed 's,^/,,; s,/,_,g')
  printf '%s/%s\n' "$STATE_ROOT" "$slug"
}

pid_alive() {
  local pid_file="$1"
  [ -f "$pid_file" ] || return 1
  local pid
  pid=$(cat "$pid_file" 2>/dev/null) || return 1
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

port_in_use() {
  local port="$1"
  timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/$port" 2>/dev/null
}

wait_healthz() {
  local port="$1" pid="$2" log_file="$3"
  local waited=0
  local code
  while [ "$waited" -lt "$HEALTHZ_TIMEOUT_ITERS" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      err "start: the server process exited before becoming healthy; last log lines:"
      tail -n 40 "$log_file" >&2 || true
      return 1
    fi
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/healthz" 2>/dev/null || true)
    if [ "$code" = "200" ]; then
      return 0
    fi
    sleep 0.5
    waited=$((waited + 1))
  done
  err "start: timed out waiting for http://127.0.0.1:$port/healthz to return 200; last log lines:"
  tail -n 40 "$log_file" >&2 || true
  return 1
}

# --------------------------------------------------------------------------
# safety guard: never let --force (or anything else) rm -rf the safe store,
# $HOME, or a top-level system directory
# --------------------------------------------------------------------------

guard_safe_to_delete() {
  local target_abs="$1"
  local safe_abs home_abs
  safe_abs=$(abs_path "$SAFE_STORE")
  home_abs=$(abs_path "$HOME")

  local forbidden f
  forbidden="/ /root /home /tmp /usr /etc /var /bin /sbin /opt /mnt /srv /boot /lib /lib64 /proc /sys /dev /Users /System"
  for f in $forbidden; do
    if [ "$target_abs" = "$f" ]; then
      err "refusing to delete '$target_abs': that is a top-level system directory, not a sane a-doc working dir"
      return 1
    fi
  done

  if [ "$target_abs" = "$home_abs" ]; then
    err "refusing to delete '$target_abs': that is \$HOME"
    return 1
  fi

  if [ "$target_abs" = "$safe_abs" ]; then
    err "refusing to delete '$target_abs': that IS the safe store (ADOC_SAFE_STORE) — real patient data lives there and this tool never writes to it"
    return 1
  fi

  case "$safe_abs/" in
    "$target_abs"/*)
      err "refusing to delete '$target_abs': the safe store ('$safe_abs') lives inside it — deleting this would destroy the safe store"
      return 1
      ;;
  esac
  case "$target_abs/" in
    "$safe_abs"/*)
      err "refusing to delete '$target_abs': it is inside the safe store ('$safe_abs') — this tool never writes to the safe store"
      return 1
      ;;
  esac

  return 0
}

clone_from_safe_store() {
  local dest="$1"
  local safe_abs
  safe_abs=$(abs_path "$SAFE_STORE")
  if [ ! -d "$safe_abs/.git" ]; then
    err "start: safe store '$safe_abs' is not an initialized a-doc data repo (no .git) — run 'adoc init' (or 'adoc restore') there first, or set ADOC_SAFE_STORE to point at one"
    exit 1
  fi
  mkdir -p "$(dirname "$dest")"
  info "start: populating '$dest' from safe store '$safe_abs' (git clone of committed content only — nothing gitignored, e.g. labs.sqlite/work/logs/inbox, is copied)"
  git clone --quiet --no-hardlinks -- "$safe_abs" "$dest"
  git -C "$dest" remote remove origin >/dev/null 2>&1 || true
}

# --------------------------------------------------------------------------
# maintenance actions (python helpers, run against ADOC_DATA_DIR)
# --------------------------------------------------------------------------

run_local_dev_op() {
  local workdir_abs="$1" op="$2" uv_bin="$3"
  (
    cd "$REPO_ROOT"
    export ADOC_DATA_DIR="$workdir_abs"
    export ADOC_MODELS_FILE="$REPO_ROOT/models.yaml"
    exec "$uv_bin" run python scripts/local_dev_ops.py "$op"
  )
}

run_experiment() {
  local workdir_abs="$1" profile="$2" uv_bin="$3"
  local safe_abs
  safe_abs=$(abs_path "$SAFE_STORE")
  if [ "$workdir_abs" = "$safe_abs" ]; then
    err "experiment: refusing to run against the safe store ('$safe_abs') — experiments write to case/experiments/ and (for 'dag') mutate the ledger; point --dir at a working copy instead"
    exit 1
  fi

  case "$profile" in
    baseline | study)
      info "experiment: running 'baseline' (labs-only single-shot control) profile — interpreting the owner's 'Study' request as this labs-only baseline; say so if that's wrong"
      (
        cd "$REPO_ROOT"
        export ADOC_DATA_DIR="$workdir_abs"
        export ADOC_MODELS_FILE="$REPO_ROOT/models.yaml"
        exec "$uv_bin" run python scripts/experiments/baseline_labs_only.py
      )
      ;;
    dag)
      info "experiment: running 'dag' (full production-DAG diagnostic turn) profile — WARNING: this MUTATES the working repo's differential ledger"
      (
        cd "$REPO_ROOT"
        export ADOC_DATA_DIR="$workdir_abs"
        export ADOC_MODELS_FILE="$REPO_ROOT/models.yaml"
        exec "$uv_bin" run python scripts/experiments/dag_enriched.py
      )
      ;;
    all)
      run_experiment "$workdir_abs" baseline "$uv_bin"
      run_experiment "$workdir_abs" dag "$uv_bin"
      ;;
    *)
      err "experiment: unknown profile '$profile' (expected one of: baseline, dag, study, all)"
      exit 2
      ;;
  esac
}

# --------------------------------------------------------------------------
# usage
# --------------------------------------------------------------------------

top_usage() {
  cat <<'EOF'
usage: local-env.sh <start|logs|stop|restart|user-create|user-list> [options]

Shared implementation behind scripts/start-local, stop-local, restart-local,
user-create-local, user-list-local. Run `local-env.sh <verb> --help` for a
verb's own options.

Env:
  ADOC_SAFE_STORE            read-only source data repo (default: $HOME/a-doc-data-local)
  ADOC_LOCAL_DEV_STATE_DIR   where pid/port/log files are kept (default: $HOME/.local/state/a-doc-local-dev)
EOF
}

start_usage() {
  cat <<'EOF'
usage: start-local [--dir PATH] [--port N] [--force] [--re-index] [--intake]
                    [--experiment PROFILE] [--no-wait] [--follow] [--quiet]
                    [--no-start]

Ensure the working data dir exists (creating/populating it from the safe
store if missing), optionally run maintenance actions against it, then
start `adoc serve` in the background and wait for /healthz.

  --dir PATH        working data dir (default: $HOME/a-doc-data-test)
  --port N          listen port (default: 8000)
  --force           delete and recreate --dir from the safe store. Refuses
                     if --dir resolves to the safe store, $HOME, or a
                     top-level system directory. Without --force, an
                     existing --dir is reused as-is.
  --re-index        rebuild labs.sqlite (from labs-export.jsonl) and the
                     document-text index (from doc-text/*.txt) without
                     re-ingesting; reports row/document counts.
  --intake          reset intake (facts, coverage, transcript, the 5
                     onboarding-derived case files restored to their
                     `adoc init` stubs, logs/chat cleared) so the next
                     /chat is a fresh initial visit. Commits the reset.
                     Never touches sources/, doc-text/, labs*, encounters,
                     the ledger, or work/users.yaml.
  --experiment PROFILE
                     run an experiment profile and write its output to
                     case/experiments/<name>.md. PROFILE is one of:
                       baseline  - one single-shot completion against ONLY
                                   the lab data (no case file, no ledger):
                                   the control condition
                       dag       - one full production-DAG diagnostic turn
                                   (Ledger-Maintainer -> Challenger -> apply
                                   -> Composer) against the whole case file.
                                   MUTATES the ledger; refused against the
                                   safe store.
                       study     - ALIAS for `baseline`. The owner listed
                                   "DAG, Study, All" without defining
                                   "Study"; this interprets it as the
                                   labs-only baseline. Tell us if that's
                                   wrong.
                       all       - both baseline and dag.
                     Refused if --dir resolves to the safe store.
                     stdout for both profiles is METADATA ONLY (counts,
                     durations, token usage, model ids) — never clinical
                     content.
  --no-wait         start the server but don't block waiting for /healthz
  --follow          after starting, tail -f the server log (blocks)
  --quiet           suppress informational output (errors still print)
  --no-start        run --re-index/--intake/--experiment (and/or just the
                     dir setup) without starting the server
  -h, --help        show this help and exit

Refuses to start if a server is already running for --dir (see
restart-local) or if --port is already in use by something else.
EOF
}

stop_usage() {
  cat <<'EOF'
usage: stop-local [--dir PATH]

Stop the background server recorded for --dir (default: $HOME/a-doc-data-test)
and remove its pid file. No-op (exit 0) if nothing is running.

  --dir PATH   working data dir
  -h, --help   show this help and exit
EOF
}

restart_usage() {
  cat <<'EOF'
usage: restart-local [options]

stop-local then start-local, passing every option through to start-local.
See `start-local --help` for the full option list.
EOF
}

user_create_usage() {
  cat <<'EOF'
usage: user-create-local [--dir PATH] USERNAME

Runs `adoc user add USERNAME` against --dir (default: $HOME/a-doc-data-test).
Prompts interactively for a password (stdin must be a TTY) — if stdin is
not a TTY this fails immediately with a clear error instead of hanging.

  --dir PATH   working data dir
  -h, --help   show this help and exit
EOF
}

user_list_usage() {
  cat <<'EOF'
usage: user-list-local [--dir PATH]

Runs `adoc user list` against --dir (default: $HOME/a-doc-data-test).

  --dir PATH   working data dir
  -h, --help   show this help and exit
EOF
}

# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

cmd_start() {
  local workdir="$DEFAULT_WORKDIR"
  local port="$DEFAULT_PORT"
  local force=0 reindex=0 intake_reset=0 experiment="" no_wait=0 follow=0 no_start=0

  while [ $# -gt 0 ]; do
    case "$1" in
      -h | --help)
        start_usage
        exit 0
        ;;
      --dir)
        [ $# -ge 2 ] || { err "start: --dir requires a value"; exit 2; }
        workdir="$2"
        shift 2
        ;;
      --dir=*) workdir="${1#*=}"; shift ;;
      --port)
        [ $# -ge 2 ] || { err "start: --port requires a value"; exit 2; }
        port="$2"
        shift 2
        ;;
      --port=*) port="${1#*=}"; shift ;;
      --force) force=1; shift ;;
      --re-index) reindex=1; shift ;;
      --intake) intake_reset=1; shift ;;
      --experiment)
        [ $# -ge 2 ] || { err "start: --experiment requires a value"; exit 2; }
        experiment="$2"
        shift 2
        ;;
      --experiment=*) experiment="${1#*=}"; shift ;;
      --no-wait) no_wait=1; shift ;;
      --follow) follow=1; shift ;;
      --quiet) QUIET=1; shift ;;
      --no-start) no_start=1; shift ;;
      --)
        shift
        break
        ;;
      -*)
        err "start: unknown option '$1'"
        start_usage >&2
        exit 2
        ;;
      *)
        err "start: unexpected argument '$1'"
        start_usage >&2
        exit 2
        ;;
    esac
  done

  case "$port" in
    '' | *[!0-9]*)
      err "start: --port must be a positive integer, got '$port'"
      exit 2
      ;;
  esac
  if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    err "start: --port must be between 1 and 65535, got '$port'"
    exit 2
  fi

  local uv_bin
  uv_bin=$(resolve_uv) || {
    err "start: uv not found on PATH or at \$HOME/.local/bin/uv — install uv first"
    exit 1
  }

  local workdir_abs
  workdir_abs=$(abs_path "$workdir")
  local state_dir
  state_dir=$(instance_state_dir "$workdir_abs")
  mkdir -p "$state_dir"
  local pid_file="$state_dir/adoc.pid"
  local port_file="$state_dir/adoc.port"
  local log_file="$state_dir/adoc.log"

  if pid_alive "$pid_file"; then
    err "start: a-doc is already running for '$workdir_abs' (pid $(cat "$pid_file")); use restart-local to restart it"
    exit 1
  fi

  # A clone carries committed content only, so `labs.sqlite` (gitignored,
  # derived) is never in it. Without a rebuild the app would come up with an
  # empty labs database — no analytes, no trends — which defeats the point of
  # copying the store. So a fresh clone always implies --re-index; the flag
  # remains useful on its own for refreshing an existing working dir.
  local cloned=0
  if [ -d "$workdir_abs" ]; then
    if [ "$force" -eq 1 ]; then
      guard_safe_to_delete "$workdir_abs" || exit 1
      info "start: --force given; removing existing working dir '$workdir_abs'"
      rm -rf -- "$workdir_abs"
      clone_from_safe_store "$workdir_abs"
      cloned=1
    else
      info "start: reusing existing working dir '$workdir_abs' as-is (pass --force to recreate from the safe store)"
    fi
  else
    clone_from_safe_store "$workdir_abs"
    cloned=1
  fi

  if [ "$cloned" -eq 1 ] && [ "$reindex" -eq 0 ]; then
    info "start: freshly cloned working dir — rebuilding derived indexes (implied --re-index)"
    reindex=1
  fi

  if [ "$reindex" -eq 1 ]; then
    info "start: --re-index — rebuilding labs.sqlite and the document-text index from committed sources..."
    run_local_dev_op "$workdir_abs" reindex "$uv_bin"
  fi

  if [ "$intake_reset" -eq 1 ]; then
    info "start: --intake — resetting intake for a fresh initial visit..."
    run_local_dev_op "$workdir_abs" intake-reset "$uv_bin"
  fi

  if [ -n "$experiment" ]; then
    run_experiment "$workdir_abs" "$experiment" "$uv_bin"
  fi

  if [ "$no_start" -eq 1 ]; then
    out "start: --no-start given; working dir ready at '$workdir_abs' (server not started)"
    exit 0
  fi

  if port_in_use "$port"; then
    err "start: port $port is already in use by something else — pick a different --port or free it"
    exit 1
  fi

  : >"$log_file"
  (
    cd "$REPO_ROOT"
    export ADOC_DATA_DIR="$workdir_abs"
    export ADOC_MODELS_FILE="$REPO_ROOT/models.yaml"
    exec "$uv_bin" run adoc serve --host 127.0.0.1 --port "$port"
  ) >>"$log_file" 2>&1 &
  local server_pid=$!
  disown "$server_pid" 2>/dev/null || true
  printf '%s\n' "$server_pid" >"$pid_file"
  printf '%s\n' "$port" >"$port_file"

  if [ "$no_wait" -eq 1 ]; then
    out "start: started (pid $server_pid, not waiting for /healthz) — log: $log_file"
  else
    if ! wait_healthz "$port" "$server_pid" "$log_file"; then
      kill "$server_pid" 2>/dev/null || true
      rm -f "$pid_file" "$port_file"
      exit 1
    fi
    out "start: up at http://127.0.0.1:$port/ (pid $server_pid, dir $workdir_abs)"
    # Always name the log. The server runs detached, so without this a 500
    # in the browser leaves no discoverable way to reach the traceback.
    out "start: log  $log_file"
    out "start:      ./scripts/logs-local --dir '$workdir_abs'          # last 200 lines"
    out "start:      ./scripts/logs-local --dir '$workdir_abs' --follow  # tail -f"
    out "start:      ./scripts/logs-local --dir '$workdir_abs' --errors  # tracebacks only"
  fi

  if [ "$follow" -eq 1 ]; then
    exec tail -f "$log_file"
  fi
}

cmd_logs() {
  local workdir="$DEFAULT_WORKDIR" lines=200 follow=0 errors=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --dir) workdir="${2:-}"; [ -n "$workdir" ] || die "--dir needs a path"; shift 2 ;;
      --lines|-n) lines="${2:-}"; [ -n "$lines" ] || die "--lines needs a count"; shift 2 ;;
      --follow|-f) follow=1; shift ;;
      --errors|-e) errors=1; shift ;;
      -h|--help)
        cat <<'USAGE'
usage: logs-local [--dir PATH] [--lines N] [--follow] [--errors]

Show the detached local server's log — where a browser 500's traceback
lands, since the server does not run in your terminal.

  --dir PATH     working data dir (default: the standard one)
  --lines N      how many trailing lines (default 200)
  --follow, -f   tail -f
  --errors, -e   only tracebacks and error lines
USAGE
        exit 0 ;;
      *) die "logs: unknown option '$1'" ;;
    esac
  done

  local workdir_abs state_dir log_file
  workdir_abs=$(abs_path "$workdir")
  state_dir=$(instance_state_dir "$workdir_abs")
  log_file="$state_dir/adoc.log"

  if [ ! -f "$log_file" ]; then
    err "logs: no log at $log_file — has a server run for '$workdir_abs'?"
    exit 1
  fi

  out "logs: $log_file"
  if [ "$errors" -eq 1 ]; then
    # Show each traceback with its exception line, which is the part worth
    # reading; -A keeps the frames after the header.
    grep -n -A 40 "Traceback (most recent call last)" "$log_file" | tail -n "$lines"
  elif [ "$follow" -eq 1 ]; then
    tail -n "$lines" -f "$log_file"
  else
    tail -n "$lines" "$log_file"
  fi
}

cmd_stop() {
  local workdir="$DEFAULT_WORKDIR"
  while [ $# -gt 0 ]; do
    case "$1" in
      -h | --help)
        stop_usage
        exit 0
        ;;
      --dir)
        [ $# -ge 2 ] || { err "stop: --dir requires a value"; exit 2; }
        workdir="$2"
        shift 2
        ;;
      --dir=*) workdir="${1#*=}"; shift ;;
      -*)
        err "stop: unknown option '$1'"
        stop_usage >&2
        exit 2
        ;;
      *)
        err "stop: unexpected argument '$1'"
        stop_usage >&2
        exit 2
        ;;
    esac
  done

  local workdir_abs
  workdir_abs=$(abs_path "$workdir")
  local state_dir
  state_dir=$(instance_state_dir "$workdir_abs")
  local pid_file="$state_dir/adoc.pid"
  local port_file="$state_dir/adoc.port"

  if ! pid_alive "$pid_file"; then
    rm -f "$pid_file"
    out "stop: no server running for '$workdir_abs'; nothing to do"
    exit 0
  fi

  local pid
  pid=$(cat "$pid_file")
  info "stop: stopping server for '$workdir_abs' (pid $pid)"
  kill "$pid" 2>/dev/null || true
  local waited=0
  while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 20 ]; do
    sleep 0.5
    waited=$((waited + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    info "stop: process did not exit after SIGTERM; sending SIGKILL"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$pid_file" "$port_file"
  out "stop: stopped"
}

cmd_restart() {
  case "${1:-}" in
    -h | --help)
      restart_usage
      exit 0
      ;;
  esac

  local workdir="$DEFAULT_WORKDIR"
  local prev=""
  local arg
  local saw_port=0
  for arg in "$@"; do
    if [ "$prev" = "--dir" ]; then
      workdir="$arg"
      prev=""
      continue
    fi
    case "$arg" in
      --dir) prev="--dir" ;;
      --dir=*) workdir="${arg#*=}" ;;
      --port | --port=*) saw_port=1 ;;
    esac
  done

  # A restart must come back on the SAME port. Without this, restarting a
  # server started with `--port 9001` silently fell back to the default and
  # then refused to bind because something else already held it — so a
  # plain `restart-local --dir X` stopped the server and never replaced it.
  # The port is recorded per instance by cmd_start; read it BEFORE stopping,
  # since cmd_stop removes the file.
  local extra_args=()
  if [ "$saw_port" -eq 0 ]; then
    local restart_state_dir restart_port_file
    restart_state_dir=$(instance_state_dir "$(abs_path "$workdir")")
    restart_port_file="$restart_state_dir/adoc.port"
    if [ -r "$restart_port_file" ]; then
      local restart_port
      restart_port=$(cat "$restart_port_file")
      if [ -n "$restart_port" ]; then
        out "restart: reusing port $restart_port"
        extra_args=(--port "$restart_port")
      fi
    fi
  fi

  cmd_stop --dir "$workdir"
  cmd_start "$@" "${extra_args[@]+"${extra_args[@]}"}"
}

cmd_user_create() {
  local workdir="$DEFAULT_WORKDIR"
  local username=""
  while [ $# -gt 0 ]; do
    case "$1" in
      -h | --help)
        user_create_usage
        exit 0
        ;;
      --dir)
        [ $# -ge 2 ] || { err "user-create: --dir requires a value"; exit 2; }
        workdir="$2"
        shift 2
        ;;
      --dir=*) workdir="${1#*=}"; shift ;;
      -*)
        err "user-create: unknown option '$1'"
        user_create_usage >&2
        exit 2
        ;;
      *)
        if [ -n "$username" ]; then
          err "user-create: unexpected extra argument '$1'"
          user_create_usage >&2
          exit 2
        fi
        username="$1"
        shift
        ;;
    esac
  done

  if [ -z "$username" ]; then
    err "user-create: missing required USERNAME argument"
    user_create_usage >&2
    exit 2
  fi

  if [ ! -t 0 ]; then
    err "user-create: stdin is not a TTY — 'adoc user add' prompts interactively for a password and would hang; run this from an interactive terminal"
    exit 1
  fi

  local workdir_abs
  workdir_abs=$(abs_path "$workdir")
  if [ ! -d "$workdir_abs" ]; then
    err "user-create: working dir '$workdir_abs' does not exist yet; run start-local first"
    exit 1
  fi

  local uv_bin
  uv_bin=$(resolve_uv) || {
    err "user-create: uv not found on PATH or at \$HOME/.local/bin/uv — install uv first"
    exit 1
  }

  cd "$REPO_ROOT"
  export ADOC_DATA_DIR="$workdir_abs"
  export ADOC_MODELS_FILE="$REPO_ROOT/models.yaml"
  exec "$uv_bin" run adoc user add "$username"
}

cmd_user_list() {
  local workdir="$DEFAULT_WORKDIR"
  while [ $# -gt 0 ]; do
    case "$1" in
      -h | --help)
        user_list_usage
        exit 0
        ;;
      --dir)
        [ $# -ge 2 ] || { err "user-list: --dir requires a value"; exit 2; }
        workdir="$2"
        shift 2
        ;;
      --dir=*) workdir="${1#*=}"; shift ;;
      -*)
        err "user-list: unknown option '$1'"
        user_list_usage >&2
        exit 2
        ;;
      *)
        err "user-list: unexpected argument '$1'"
        user_list_usage >&2
        exit 2
        ;;
    esac
  done

  local workdir_abs
  workdir_abs=$(abs_path "$workdir")
  if [ ! -d "$workdir_abs" ]; then
    err "user-list: working dir '$workdir_abs' does not exist yet; run start-local first"
    exit 1
  fi

  local uv_bin
  uv_bin=$(resolve_uv) || {
    err "user-list: uv not found on PATH or at \$HOME/.local/bin/uv — install uv first"
    exit 1
  }

  cd "$REPO_ROOT"
  export ADOC_DATA_DIR="$workdir_abs"
  export ADOC_MODELS_FILE="$REPO_ROOT/models.yaml"
  exec "$uv_bin" run adoc user list
}

# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

main() {
  local verb="${1:-}"
  case "$verb" in
    "" | -h | --help)
      top_usage
      exit 0
      ;;
    start)
      shift
      cmd_start "$@"
      ;;
    stop)
      shift
      cmd_stop "$@"
      ;;
    logs)
      shift
      cmd_logs "$@"
      ;;
    restart)
      shift
      cmd_restart "$@"
      ;;
    user-create)
      shift
      cmd_user_create "$@"
      ;;
    user-list)
      shift
      cmd_user_list "$@"
      ;;
    *)
      err "local-env.sh: unknown command '$verb'"
      top_usage >&2
      exit 2
      ;;
  esac
}

main "$@"
