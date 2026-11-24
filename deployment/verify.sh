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
# AST-gate разбирает только публичные Python-корни проекта, не импортирует код
# и запрещает добавлять функции без содержательного русского docstring.
python3 deployment/check_docstrings.py
python_roots=(messenger-worksheets)
if [[ -d messenger-client ]]; then
    python_roots+=(messenger-client)
fi
python3 -m compileall -q "${python_roots[@]}" \
    deployment/check_messenger.py deployment/doctor_openai.py deployment/provision_instances.py \
    deployment/check_bot.py deployment/configure_bot.py \
    deployment/smoke_test_worker.py deployment/diagnose_openai.py deployment/login_codex.py \
    deployment/update_messenger_oauth.py
PYTHONPATH=messenger-worksheets python3 -m unittest discover -s tests -p 'test_*.py'

echo "Repository checks passed."
