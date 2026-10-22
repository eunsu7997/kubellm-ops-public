#!/usr/bin/env bash
# Isolated, opt-in model-load fault with captured original model/args and finally recovery.
# Defaults to dry-run. Required --output-dir must be a new path.
# Example (inside WSL):
# bash scripts/failure-tests/scenario4_model_load_failure.sh --k3s \
#   --output-dir evidence/new-model-failure --execute
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/../kubernetes/model-load-failure.py" "$@"
