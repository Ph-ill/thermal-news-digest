#!/usr/bin/env bash
# Fixed-input daily news-and-local-events receipt printer bridge.
set -Eeuo pipefail
IFS=$'\n\t'
# Production keeps a fixed PATH. Tests set DIGEST_TOOL_PATH to fake lp and lpstat.
if [[ -n "${DIGEST_TOOL_PATH:-}" ]]; then
  PATH="$DIGEST_TOOL_PATH"
else
  PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
fi
export PATH

ROOT="${DIGEST_HOME:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
readonly ROOT
readonly GENERATOR="$ROOT/digest_generator.py"
readonly RENDERER="$ROOT/receipt_digest.py"
readonly OUTPUT_DIR="$ROOT/output"
# The edition date follows DIGEST_TZ, matching the generator, not the host zone.
DIGEST_DATE="$(TZ="${DIGEST_TZ:-America/Chicago}" date +%F)"
readonly DIGEST_DATE
readonly SOURCE="$OUTPUT_DIR/digest-source-${DIGEST_DATE}.json"
readonly STATE="$ROOT/state.json"
readonly QUEUE="${DIGEST_PRINTER_QUEUE:-receipt}"
readonly LOCK="$ROOT/print.lock"

usage() {
  printf 'Usage: %s --dry-run|--submit|--reprint|--rebuild\n' "$(basename "$0")" >&2
}

[[ $# -eq 1 ]] || { usage; exit 2; }
case "$1" in
  --dry-run|--submit|--reprint|--rebuild) readonly MODE="$1" ;;
  *) usage; exit 2 ;;
esac

[[ -x "$GENERATOR" ]] || { printf 'Missing executable generator: %s\n' "$GENERATOR" >&2; exit 1; }
[[ -x "$RENDERER" ]] || { printf 'Missing executable renderer: %s\n' "$RENDERER" >&2; exit 1; }
command -v flock >/dev/null
command -v python3 >/dev/null
command -v rsvg-convert >/dev/null
command -v lp >/dev/null
command -v lpstat >/dev/null
lpstat -p "$QUEUE" >/dev/null
mkdir -p "$OUTPUT_DIR"

# The child python must not take print.lock again; this flock already holds it.
export DIGEST_LOCK_HELD=1
exec flock --exclusive --nonblock "$LOCK" bash -c '
  set -Eeuo pipefail
  export DIGEST_LOCK_HELD=1
  generator=$1
  source_path=$2
  renderer=$3
  mode=$4
  output_dir=$5
  state_path=$6
  queue=$7
  rotated_to=""
  generate() {
    python3 "$generator" --out "$source_path" >&2
  }
  restore_rotated() {
    if [[ -n "$rotated_to" && -e "$rotated_to" ]]; then
      rm -f -- "$source_path"
      mv -- "$rotated_to" "$source_path"
      printf "Restored previous source after generation failed\n" >&2
    fi
  }
  generate_or_restore() {
    generate && return 0
    status=$?
    restore_rotated
    exit "$status"
  }
  # Keep the frozen source as digest-source-DATE.vN.json, then write a new one.
  # A failed generate moves that archive back, so the canonical file is never missing.
  rotate_source() {
    if [[ ! -e "$source_path" ]]; then
      return 0
    fi
    if [[ -L "$source_path" ]]; then
      printf "Refusing to rotate a symlink: %s\n" "$source_path" >&2
      exit 1
    fi
    local n=1 dest
    while [[ "$n" -le 99 ]]; do
      dest="${source_path%.json}.v${n}.json"
      if [[ -e "$dest" ]]; then
        n=$((n + 1))
        continue
      fi
      mv -- "$source_path" "$dest"
      rotated_to=$dest
      printf "Kept previous source as %s\n" "$dest" >&2
      return 0
    done
    printf "Too many archived source files for %s\n" "$source_path" >&2
    exit 1
  }
  # Renderer exit 2 means the source cannot be printed. Anything else is kept.
  render() {
    python3 "$renderer" "$1" --source "$source_path" --output-dir "$output_dir" --state "$state_path" --queue "$queue"
  }
  render_mode=$mode
  if [[ "$mode" == "--rebuild" ]]; then
    rotate_source
    render_mode=--reprint
  fi
  if [[ ! -s "$source_path" ]]; then
    generate_or_restore
  fi
  status=0
  render "$render_mode" || status=$?
  if [[ "$status" -eq 2 ]]; then
    rm -f -- "$source_path"
    generate_or_restore
    status=0
    render "$render_mode" || status=$?
  fi
  exit "$status"
' receipt-digest "$GENERATOR" "$SOURCE" "$RENDERER" "$MODE" "$OUTPUT_DIR" "$STATE" "$QUEUE"
