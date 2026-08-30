#!/usr/bin/env bash
# Idempotent Danse delivery predicate. It never calls render.py or changes a
# delivery artifact. The final checker atomically records only the submission
# validation it actually executes and its human receipt chain; the preceding
# portable and browser predicates remain truthful process gates, not claims in
# that narrower durable receipt.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE"
package=""
phase="package"

usage() {
  echo "usage: done.sh --package <path> [--phase package|uploaded|submitted]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --package)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      package="$2"
      shift 2
      ;;
    --phase)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      phase="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

[[ -n "$package" ]] || { usage; exit 2; }
case "$phase" in
  package|uploaded|submitted) ;;
  *) usage; exit 2 ;;
esac

python3 "$ROOT/scripts/check-danse.py"
python3 "$ROOT/render/browser.py" --check --verify --arrival --probe
python3 "$ROOT/submission/check.py" \
  --package "$package" \
  --phase "$phase" \
  --write-done-receipt

echo
phase_upper="$(printf '%s' "$phase" | tr '[:lower:]' '[:upper:]')"
echo "DANSE ${phase_upper} DONE — invariant, Metal, reproduction, arrival, continuity, package, and exact receipt-chain predicates hold"
