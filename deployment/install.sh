#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi

bundle_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
app_source=${bundle_root}/messenger-worksheets
client_source=${bundle_root}/messenger-client
deployment_source=${bundle_root}/deployment

for required in \
    "${app_source}/messenger_worksheets/cloud_gateway.py" \
    "${app_source}/messenger_worksheets/codex_rpc.py" \
    "${app_source}/messenger_user_client/client.py" \
    "${app_source}/requirements.lock" \
    "${deployment_source}/worksheet-worker.service" \
    "${deployment_source}/worksheet-gateway.service"; do
    [[ -f ${required} ]] || { echo "Missing bundle file: ${required}" >&2; exit 1; }
done

# Скрыто читает секрет из TTY, проверяет ограниченный алфавит и присваивает его
# переменной по имени без eval. Значение не печатается, не попадает в argv и
# живёт только до записи root-owned env с mode 0600.
read_secret() {
    local prompt=$1 variable=$2 value
    IFS= read -r -s -p "${prompt}: " value
    echo
    if [[ -z ${value} || ! ${value} =~ ^[A-Za-z0-9._~+/=-]+$ ]]; then
        echo "The value is empty or contains unsupported characters." >&2
        exit 1
    fi
    printf -v "${variable}" '%s' "${value}"
}

if ! id -u w495 >/dev/null 2>&1; then
    echo "Expected operator account w495 is missing." >&2
    exit 1
fi
for account in worksheet-worker worksheet-gateway worksheet-chat-admin; do
    if ! id -u "${account}" >/dev/null 2>&1; then
        useradd --system --user-group --home-dir "/var/lib/messenger-worksheets-${account#worksheet-}" \
            --no-create-home --shell /usr/sbin/nologin "${account}"
    fi
done

# Код приложения публикуется read-only для runtime users. Legacy client получает
# отдельную группу worksheet-gateway, clean-room package находится в app tree;
# ни worker, ни gateway не получают права записи в исполняемые исходники.
install -d -m 0755 -o root -g root /opt/messenger-worksheets
install -d -m 0755 -o root -g root /opt/messenger-worksheets/app
rsync -a --exclude='__pycache__/' "${app_source}/" /opt/messenger-worksheets/app/
chown -R root:root /opt/messenger-worksheets/app
find /opt/messenger-worksheets/app -type d -exec chmod 0755 {} +
find /opt/messenger-worksheets/app -type f -exec chmod 0644 {} +

# Legacy adapter необязателен и в публичный исходный код не входит. Он может
# устанавливаться только из отдельного правомерно полученного дистрибутива.
if [[ -f ${client_source}/client.py && -f ${client_source}/transport.py ]]; then
    install -d -m 0750 -o root -g worksheet-gateway /opt/messenger-worksheets/messenger-client
    install -m 0640 -o root -g worksheet-gateway "${client_source}/client.py" \
        /opt/messenger-worksheets/messenger-client/client.py
    install -m 0640 -o root -g worksheet-gateway "${client_source}/transport.py" \
        /opt/messenger-worksheets/messenger-client/transport.py
fi

if ! python3 -m ensurepip --version >/dev/null 2>&1; then
    echo "Python ensurepip is unavailable. Install python3.14-venv and rerun." >&2
    exit 1
fi
if [[ ! -x /opt/messenger-worksheets/venv/bin/python ]]; then
    python3 -m venv /opt/messenger-worksheets/venv
fi
if ! /opt/messenger-worksheets/venv/bin/python -m pip --version >/dev/null 2>&1; then
    /opt/messenger-worksheets/venv/bin/python -m ensurepip --upgrade
fi
/opt/messenger-worksheets/venv/bin/python -m pip install \
    --disable-pip-version-check --require-hashes \
    --requirement /opt/messenger-worksheets/app/requirements.lock
chown -R root:root /opt/messenger-worksheets/venv

# CA пересоздаётся только при отсутствии обязательных файлов либо keyCertSign;
# корректная существующая цепочка и связанные секреты не ротируются без причины.
if [[ ! -s /etc/messenger-worksheets/ca-api.crt \
      || ! -s /etc/messenger-worksheets/ca-local-user.crt \
      || ! -s /etc/messenger-worksheets/ca-sandbox-admin.crt \
      || ! -s /etc/messenger-worksheets/worker/server.key \
      || ! -s /etc/messenger-worksheets/local-user/server.key \
      || ! -s /etc/messenger-worksheets/sandbox-admin/server.key ]] \
      || ! openssl x509 -in /etc/messenger-worksheets/ca-api.crt -noout -text | \
          grep -q 'Certificate Sign'; then
    "${deployment_source}/generate_tls.sh"
fi

worker_tmp=$(mktemp)
gateway_tmp=$(mktemp)
trap 'rm -f -- "${worker_tmp}" "${gateway_tmp}"' EXIT
messenger_oauth=
openai_key=
read_secret "Personal Yandex Messenger OAuth" messenger_oauth
read_secret "OpenAI API key" openai_key
transport_token=$(openssl rand -hex 32)
# Секреты сначала записываются во временные файлы, затем устанавливаются как
# root:root 0600. Trap удаляет временные копии при успехе и при любой ошибке.
{
    printf 'WORKSHEET_CODEX_PROVIDER=openai\n'
    printf 'WORKSHEET_CODEX_MODEL=gpt-5.3-codex\n'
    printf 'WORKSHEET_CODEX_REASONING_EFFORT=high\n'
    printf 'WORKSHEET_CODEX_TASK_TIMEOUT=1800\n'
    printf 'WORKSHEET_CODEX_CHAT_TIMEOUT=1800\n'
    printf 'WORKSHEET_CODEX_MAX_CONCURRENT_JOBS=4\n'
    printf 'WORKSHEET_CODEX_BINARY=/usr/local/bin/codex\n'
    printf 'WORKSHEET_CODEX_LISTEN=127.0.0.1\n'
    printf 'WORKSHEET_CODEX_PORT=8443\n'
    printf 'WORKSHEET_CODEX_TLS_CERT=/etc/messenger-worksheets/worker/server.crt\n'
    printf 'WORKSHEET_CODEX_TLS_KEY=/etc/messenger-worksheets/worker/server.key\n'
    printf 'WORKSHEET_CODEX_TRANSPORT_TOKEN=%s\n' "${transport_token}"
    printf 'WORKSHEET_CODEX_API_KEY=%s\n' "${openai_key}"
} > "${worker_tmp}"
{
    printf 'WORKSHEET_MESSENGER_BACKEND=cleanroom\n'
    printf 'WORKSHEET_MESSENGER_CLIENT_DIR=/opt/messenger-worksheets/messenger-client\n'
    printf 'WORKSHEET_MESSENGER_TOKEN=%s\n' "${messenger_oauth}"
    printf 'WORKSHEET_CODEX_WORKER_URL=https://127.0.0.1:8443/v1/codex-worksheet-jobs\n'
    printf 'WORKSHEET_CODEX_TASK_RPC_TIMEOUT=1860\n'
    printf 'WORKSHEET_CODEX_CHAT_RPC_TIMEOUT=1860\n'
    printf 'WORKSHEET_CODEX_WORKER_CA=/etc/messenger-worksheets/ca-api.crt\n'
    printf 'WORKSHEET_CODEX_TRANSPORT_TOKEN=%s\n' "${transport_token}"
} > "${gateway_tmp}"
install -m 0600 -o root -g root "${worker_tmp}" /etc/messenger-worksheets/worker.env
install -m 0600 -o root -g root "${gateway_tmp}" /etc/messenger-worksheets/gateway.env
unset messenger_oauth openai_key transport_token

# Unit-файлы устанавливаются и проходят systemd-analyze, но installer намеренно
# не включает и не запускает сервисы: operator сначала выполняет read-only smoke.
install -m 0644 -o root -g root "${deployment_source}/worksheet-worker.service" \
    /etc/systemd/system/worksheet-worker.service
install -m 0644 -o root -g root "${deployment_source}/worksheet-gateway.service" \
    /etc/systemd/system/worksheet-gateway.service
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/worksheet-worker.service \
    /etc/systemd/system/worksheet-gateway.service

echo "Installation prepared. Services were not enabled or started."
