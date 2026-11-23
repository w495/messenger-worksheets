#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi
if ! id -u operator >/dev/null 2>&1; then
    echo "Expected operator account operator is missing." >&2
    exit 1
fi

deployment_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "${deployment_dir}/.." && pwd)
install -m 0644 -o root -g root \
    "${repo_root}/messenger-worksheets/messenger_worksheets/codex_worker.py" \
    /opt/messenger-worksheets/app/messenger_worksheets/codex_worker.py
install -d -m 0755 -o root -g root /opt/messenger-worksheets/admin
for script in diagnose_openai.py login_codex.py smoke_test_worker.py; do
    install -m 0755 -o root -g root "${deployment_dir}/${script}" \
        "/opt/messenger-worksheets/admin/${script}"
done
install -m 0755 -o root -g root "${deployment_dir}/worksheet-admin" \
    /usr/local/sbin/worksheet-admin

sudoers_tmp=$(mktemp)
trap 'rm -f -- "${sudoers_tmp}"' EXIT
for operation in status diagnose-openai login-codex smoke-worker set-native-codex \
    set-profile-gpt-6.2-sol-high \
    start-worker restart-worker stop-worker start-gateway stop-gateway \
    logs-worker logs-gateway revoke; do
    printf 'operator ALL=(root) NOPASSWD: /usr/local/sbin/worksheet-admin %s\n' "${operation}"
done > "${sudoers_tmp}"
chmod 0440 "${sudoers_tmp}"
visudo -cf "${sudoers_tmp}"
install -m 0440 -o root -g root "${sudoers_tmp}" \
    /etc/sudoers.d/messenger-worksheets-admin

echo "Temporary restricted worksheet-admin access installed for operator."
