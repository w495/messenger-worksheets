#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "${repo_root}"

bash -n deployment/install.sh deployment/verify.sh
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck deployment/install.sh deployment/verify.sh
fi
python3 -m compileall -q messenger-worksheets messenger-client deployment/smoke_test_worker.py

echo "Repository checks passed."
