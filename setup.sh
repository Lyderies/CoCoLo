#!/bin/bash
# Launcher for COCOLO's setup wizard (Claude Orchestrator, Codex Orchestrator, Local Orchestrator).
#
#   ./setup.sh [PROJECT_PATH]
#
# Trusts python3 before it ever runs, then hands over to the wizard itself, which is Python
# (scripts/setup-wizard.py and scripts/setup_wizard/): checks, project, providers, install,
# collector, billing attestation, the optional Codex orchestrator profile, the optional Codex
# commands rule, and a summary. It needs a real terminal, because the billing attestation is a
# human step that must never be automated.
#
# Written for bash 3.2 (macOS): no associative arrays, no case-conversion expansions,
# no mapfile or readarray. Only standard tools plus tput/stty and python3.
#
# Owner authority stays out of reach of the AI agents this sets up. Every Python it starts
# is the real python3 file found on PATH, trusted before it ever runs (never a symlink, a
# virtual environment's copy, macOS's /usr/bin shim, or one in a temporary folder, the
# project, or outside the runtime's trusted installation folders), run with
# -I -B -S -X pycache_prefix=/dev/null so nothing from the working folder, the environment,
# site-packages or a bytecode cache loads. Its working files live in a host-private folder
# no agent sandbox can write. Owner commands run this repository's own orchestrator CLI
# with --project-root, never the project's copy, and only once the installed copy is this
# release byte for byte. Git is a trusted real binary run with plumbing only (no status,
# add or commit), every helper and transport off, and not at all on a partial clone or
# while the project's own Git settings name a helper program. Started as ./setup.sh, the bash
# that runs this file and every basic tool it calls come from the system folders, never an
# earlier PATH entry, and the Codex and Claude clients run only as the runtime runs them
# (attest.py client-status). This folder must sit outside every folder opened in Codex.
#
# Test hook (no terminal needed):
#   ./setup.sh --print-provider-routing CODEX CLAUDE MODELS BRIDGE
# where CODEX, CLAUDE and BRIDGE are "on" or "off" and MODELS is a comma-separated
# list of Claude model names, or "-" for none. It prints provider-routing.json.
#   ./setup.sh --print-expanded-path VALUE     prints VALUE with a leading ~ or $HOME expanded
#   ./setup.sh --print-codex-hint WANTED READY prints the Codex command and why (1 or 0 each)
#   ./setup.sh --print-codex-primary FILE      prints yes when a Codex session may orchestrate
#   ./setup.sh --print-changed-paths PROJECT PATH...  NUL-delimited PATHs with real changes
#   ./setup.sh --print-ignored-paths PROJECT PATH...  NUL-delimited PATHs ignored by .gitignore
#   ./setup.sh --print-python                  prints the trusted Python, or "refused: REASON"
#   ./setup.sh --print-git                     prints the trusted git binary, or "none"
#   ./setup.sh --print-trusted-roots           prints the trusted installation folders, one per line
#   ./setup.sh --print-git-helpers PROJECT     prints helper programs the project's Git settings name, or "none"

# Basic tools (dirname, uname, id, sed, tput and the rest) come only from the system folders:
# an earlier PATH entry, which a Codex session opened on a folder above it could write, never
# stands in for one. The PATH you set is kept only to find python3, which is trusted before it
# ever runs (resolve_python), and to say where a client the runtime will not use was found.
OWNER_PATH=${PATH-}
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
# Never let Python write bytecode into the project (it would dirty managed paths).
export PYTHONDONTWRITEBYTECODE=1
ROOT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
ORCH_DIR="$ROOT_DIR/packages/codex-agents/native-package/.codex/orchestration"
# Owner commands run this repository's CLI, never the project's installed copy.
CLONE_CLI="$ORCH_DIR/cli.py"
# Flags for every Python this wizard starts: isolated, no bytecode written or read.
PYF=(-I -B -S -X pycache_prefix=/dev/null)
# Git options for every command on the project: no repository hooks, file-system monitor,
# signing or automatic maintenance.
GIT_SAFE=(-c core.hooksPath=/dev/null -c core.fsmonitor=false -c commit.gpgSign=false -c tag.gpgSign=false -c gc.auto=0 -c maintenance.auto=false)
# Repository settings that name a program Git may start (scripts/install.py uses the same
# pattern): filters, diff and merge drivers, transports, pagers, editors, signing and
# credential helpers, trailer commands, and includes, whose files cannot be vetted here.
GIT_HELPER_KEYS='^(filter\..+\.(clean|smudge|process)|diff\.external|diff\..+\.(command|textconv)|merge\..+\.driver|core\.(sshcommand|pager|editor|askpass|gitproxy|alternaterefscommand)|gpg\.(.+\.)?program|gpg\.ssh\.defaultkeycommand|credential\.(.+\.)?helper|pager\..+|sequence\.editor|include\.path|includeif\..+\.path|trailer\..+\.(command|cmd))$'
# Where a trusted program may be installed: the runtime's host_paths.TRUSTED_INSTALL_ROOTS
# (a test keeps this copy equal; the Linuxbrew prefix is split as there), plus a hidden
# top-level folder of the account's home folder. The interpreter is judged against it before
# it ever runs, so the check is in bash.
TRUSTED_INSTALL_ROOTS="/usr/bin /bin /usr/libexec /usr/local /opt/homebrew /home""/linuxbrew/.linuxbrew /Library/Frameworks/Python.framework /Library/Developer/CommandLineTools /Applications /nix/store"
SETUP_VERSION=1
CLAUDE_MODELS_ALL="claude-opus-5-5 claude-sonnet-5-5 claude-haiku-5-5"
STEP_NAMES=("Checks" "Project" "Providers" "Install" "Collector" "Billing" "Profile" "Commands" "Done")
STEP_TOTAL=9

# ---------------------------------------------------------------------------
# provider-routing.json (the closed key set the router validates)
# ---------------------------------------------------------------------------

# routing_json CODEX CLAUDE MODELS_CSV BRIDGE -> prints the JSON, returns 1 on bad input.

real_file() {
  local path=$1 link dir n=0
  while [ -L "$path" ]; do
    n=$((n + 1)); [ "$n" -le 40 ] || return 1
    link=$(readlink "$path") || return 1
    case "$link" in /*) path=$link;; *) path="$(dirname -- "$path")/$link";; esac
  done
  [ -f "$path" ] || return 1
  dir=$(CDPATH='' cd -- "$(dirname -- "$path")" 2>/dev/null && pwd -P) || return 1
  printf '%s/%s\n' "$dir" "$(basename -- "$path")"
}

# inside_dir PATH ROOT : succeeds when PATH is ROOT or lies below it.
inside_dir() {
  case "$1" in "$2"|"$2"/*) return 0;; esac
  return 1
}

# inside_temp PATH : succeeds when PATH lies in a temporary folder a sandboxed session can write.
inside_temp() {
  local root real
  for root in /tmp /var/tmp "${TMPDIR:-}" "$( [ "$(uname -s)" = Darwin ] && /usr/bin/getconf DARWIN_USER_TEMP_DIR 2>/dev/null)"; do
    case "$root" in /*) ;; *) continue;; esac
    real=$(CDPATH='' cd -- "$root" 2>/dev/null && pwd -P) || continue
    inside_dir "$1" "$real" && return 0
  done
  return 1
}

# work_tree_top DIR : the top folder of the Git working tree holding DIR, found by walking up
# to a .git entry without running anything, or nothing.
work_tree_top() {
  local dir
  dir=$(CDPATH='' cd -- "$1" 2>/dev/null && pwd -P) || return 1
  while :; do
    if [ -e "$dir/.git" ] || [ -L "$dir/.git" ]; then printf '%s\n' "$dir"; return 0; fi
    [ "$dir" = / ] && return 1
    dir=$(dirname -- "$dir")
  done
}

# account_home : this account's home folder from the system account database (bash expands
# ~user through it), never from HOME, as the runtime's host_paths.account_home.
account_home() {
  local user home
  user=$(id -un 2>/dev/null) || return 1
  case "$user" in ''|*[!A-Za-z0-9._-]*) return 1;; esac
  home=$(eval "printf '%s' ~$user")
  case "$home" in /*) ;; *) return 1;; esac
  CDPATH='' cd -- "$home" 2>/dev/null && pwd -P
}

# install_location_trusted FILE : succeeds when FILE lies under a trusted installation folder
# or in a hidden top-level folder of the account's home folder (host_paths's rule).
install_location_trusted() {
  local root home rel
  for root in $TRUSTED_INSTALL_ROOTS; do inside_dir "$1" "$root" && return 0; done
  home=$(account_home) || return 1
  if [ "$home" != / ] && inside_dir "$1" "$home" && [ "$1" != "$home" ]; then
    rel=${1#"$home"/}
    case "$rel" in .*) return 0;; esac
  fi
  return 1
}

# python_location_problem FILE [PROJECT] : why an interpreter's real file may not run owner
# code (temporary, project, venv, shim or install), or nothing. It is decided before the file
# ever runs, with the same rules as attest.py's python_location_problem.
python_location_problem() {
  local py=$1 project=${2:-}
  if inside_temp "$py"; then echo temporary; return 0; fi
  if [ -n "$project" ] && inside_dir "$py" "$project"; then echo project; return 0; fi
  if [ -e "$(dirname -- "$py")/pyvenv.cfg" ] || [ -e "$(dirname -- "$(dirname -- "$py")")/pyvenv.cfg" ]; then
    echo venv; return 0
  fi
  if [ "$(uname -s)" = Darwin ] && [ "$(dirname -- "$py")" = /usr/bin ]; then echo shim; return 0; fi
  install_location_trusted "$py" || echo install
  return 0
}

# owner_path_lookup NAME : the first executable NAME in the PATH you set, as `command -v`
# finds it, but only in absolute folders. A lookup only: nothing found here runs unchecked.
owner_path_lookup() {
  local dir rest="$OWNER_PATH:"
  while [ -n "$rest" ]; do
    dir=${rest%%:*}; rest=${rest#*:}
    case "$dir" in /*) ;; *) continue;; esac
    if [ -f "$dir/$1" ] && [ -x "$dir/$1" ]; then printf '%s\n' "$dir/$1"; return 0; fi
  done
  return 1
}

# resolve_python [START] -> PY, PY_PROBLEM : the python3 on your PATH as its real file, trusted
# before it runs at all. START (default: the current folder) stands for the project until one
# is chosen; the chosen project is checked again in step 2.
PY=""
PY_PROBLEM=""
resolve_python() {
  local found top
  PY=""; PY_PROBLEM=""
  found=$(owner_path_lookup python3) || { PY_PROBLEM=missing; return 1; }
  case "$found" in /*) ;; *) PY_PROBLEM=missing; return 1;; esac
  PY=$(real_file "$found") || { PY=""; PY_PROBLEM=missing; return 1; }
  top=$(work_tree_top "${1:-$PWD}") || top=""
  PY_PROBLEM=$(python_location_problem "$PY" "$top")
  [ -z "$PY_PROBLEM" ] && return 0
  PY=""
  return 1
}

# python_problem_text : a plain sentence for PY_PROBLEM.
python_problem_text() {
  case "$PY_PROBLEM" in
    temporary) echo "The python3 on your PATH is in a temporary folder, where an AI agent could change it.";;
    venv) echo "The python3 on your PATH belongs to a virtual environment, whose settings an AI agent could change.";;
    project) echo "The python3 on your PATH is inside this project, where an AI agent could change it.";;
    shim) echo "The python3 on your PATH is macOS's /usr/bin/python3, a shim that reads a cache an AI agent can write.";;
    install) echo "The python3 on your PATH is not in a trusted installation folder (a system folder, Homebrew, python.org, Xcode, Nix, or a hidden folder in your home folder), so a Codex session opened on a folder above it could change it.";;
    *) echo "python3 was not found.";;
  esac
}

# The interpreter is trusted before anything runs (resolve_python); the folder setup starts in (or
# the project given on the command line) stands for the project until one is chosen.
case "${1:-}" in
  --*) START_DIR=$PWD;;
  *) START_DIR=${1:-$PWD};;
esac
resolve_python "$START_DIR"

case "${1:-}" in
  --print-python)
    if [ -n "$PY" ]; then printf '%s\n' "$PY"; else printf 'refused: %s\n' "$PY_PROBLEM"; fi; exit 0;;
  --print-trusted-roots)
    printf '%s\n' $TRUSTED_INSTALL_ROOTS; exit 0;;
esac

if [ -z "$PY" ]; then
  if [ "$PY_PROBLEM" = missing ]; then
    echo "python3 not found. Fix: install Python 3.11 or later." >&2
  else
    echo "python3 cannot be trusted. $(python_problem_text)" >&2
    echo "Fix: put a system Python, such as Homebrew's or python.org's, first on PATH." >&2
  fi
  exit 1
fi
if ! "$PY" "${PYF[@]}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  echo "python3 is older than 3.11. Fix: install Python 3.11 or later and put it first on PATH." >&2
  exit 1
fi

# The wizard itself (scripts/setup_wizard), in the trusted Python, isolated.
COCOLO_PYTHON=$PY COCOLO_OWNER_PATH=$OWNER_PATH COCOLO_BASH_VERSION=${BASH_VERSION%%(*} \
  exec "$PY" "${PYF[@]}" "$ROOT_DIR/scripts/setup-wizard.py" "$@"
