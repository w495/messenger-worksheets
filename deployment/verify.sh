#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "${repo_root}"

bash -n deployment/install.sh deployment/verify.sh deployment/generate_tls.sh deployment/repair_tls.sh \
    deployment/install_admin_helper.sh deployment/worksheet-admin
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck deployment/install.sh deployment/verify.sh deployment/generate_tls.sh deployment/repair_tls.sh \
        deployment/install_admin_helper.sh deployment/worksheet-admin
fi
python3 -m compileall -q messenger-worksheets messenger-client \
    deployment/check_messenger.py deployment/provision_instances.py deployment/smoke_test_worker.py \
    deployment/diagnose_openai.py deployment/login_codex.py deployment/update_messenger_oauth.py

echo "Repository checks passed."
