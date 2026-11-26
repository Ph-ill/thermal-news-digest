#!/usr/bin/env bash
# Fixed-input daily news-and-local-events receipt printer bridge.
set -Eeuo pipefail
IFS=$'\n\t'
PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
export PATH

ROOT="${DIGEST_HOME:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
readonly ROOT
readonly GENERATOR="$ROOT/digest_generator.py"
readonly RENDERER="$ROOT/receipt_digest.py"
readonly OUTPUT_DIR="$ROOT/output"
readonly SOURCE="$OUTPUT_DIR/digest-source-$(date +%F).json"
readonly STATE="$ROOT/state.json"
readonly QUEUE="${DIGEST_PRINTER_QUEUE:-receipt}"
readonly LOCK="$ROOT/print.lock"

usage() {
  printf 'Usage: %s --dry-run|--submit\n' "$(basename "$0")" >&2
}

[[ $# -eq 1 ]] || { usage; exit 2; }
case "$1" in
  --dry-run|--submit) readonly MODE="$1" ;;
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

exec flock --exclusive --nonblock "$LOCK" bash -c '
  set -Eeuo pipefail
  # Freeze one atomic source edition per local date. Repeated button presses
  # reuse that edition so changing live feeds cannot bypass duplicate protection.
  if [[ ! -s "$2" ]]; then
    python3 "$1" --out "$2" >&2
  fi
  exec python3 "$3" "$4" --source "$2" --output-dir "$5" --state "$6" --queue "$7"
' receipt-digest "$GENERATOR" "$SOURCE" "$RENDERER" "$MODE" "$OUTPUT_DIR" "$STATE" "$QUEUE"
