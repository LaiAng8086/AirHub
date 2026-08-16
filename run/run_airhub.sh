#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_environment.sh"
ensure_airhub_python

cd "${PROJECT_ROOT}"
python -u -m airhub "$@"
