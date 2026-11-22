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
    "${client_source}/client.py" \
    "${client_source}/transport.py" \
    "${deployment_source}/worksheet-worker.service" \
    "${deployment_source}/worksheet-gateway.service"; do
    [[ -f ${required} ]] || { echo "Missing bundle file: ${required}" >&2; exit 1; }
done

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

for account in worksheet-worker worksheet-gateway; do
    if ! id -u "${account}" >/dev/null 2>&1; then
        useradd --system --user-group --home-dir "/var/lib/messenger-worksheets-${account#worksheet-}" \
            --no-create-home --shell /usr/sbin/nologin "${account}"
    fi
done

install -d -m 0755 -o root -g root /opt/messenger-worksheets
install -d -m 0755 -o root -g root /opt/messenger-worksheets/app
rsync -a --exclude='__pycache__/' "${app_source}/" /opt/messenger-worksheets/app/
chown -R root:root /opt/messenger-worksheets/app
find /opt/messenger-worksheets/app -type d -exec chmod 0755 {} +
find /opt/messenger-worksheets/app -type f -exec chmod 0644 {} +

install -d -m 0750 -o root -g worksheet-gateway /opt/messenger-worksheets/messenger-client
install -m 0640 -o root -g worksheet-gateway "${client_source}/client.py" \
    /opt/messenger-worksheets/messenger-client/client.py
install -m 0640 -o root -g worksheet-gateway "${client_source}/transport.py" \
    /opt/messenger-worksheets/messenger-client/transport.py

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
    --disable-pip-version-check --requirement /opt/messenger-worksheets/app/requirements.txt
chown -R root:root /opt/messenger-worksheets/venv

install -d -m 0755 -o root -g root /etc/messenger-worksheets
install -d -m 0750 -o root -g worksheet-worker /etc/messenger-worksheets/worker

if [[ ! -s /etc/messenger-worksheets/ca.crt || ! -s /etc/messenger-worksheets/worker/server.crt \
      || ! -s /etc/messenger-worksheets/worker/server.key ]]; then
    certificate_work=$(mktemp -d)
    trap 'rm -rf -- "${certificate_work}"' EXIT
    openssl genpkey -algorithm ED25519 -out "${certificate_work}/ca.key"
    openssl req -x509 -new -key "${certificate_work}/ca.key" -days 825 \
        -subj '/CN=messenger-worksheets-local-ca' -out "${certificate_work}/ca.crt"
    openssl genpkey -algorithm ED25519 -out "${certificate_work}/server.key"
    openssl req -new -key "${certificate_work}/server.key" \
        -subj '/CN=localhost' -addext 'subjectAltName=IP:127.0.0.1,DNS:localhost' \
        -out "${certificate_work}/server.csr"
    printf '%s\n' 'subjectAltName=IP:127.0.0.1,DNS:localhost' > "${certificate_work}/server.ext"
    openssl x509 -req -in "${certificate_work}/server.csr" \
        -CA "${certificate_work}/ca.crt" -CAkey "${certificate_work}/ca.key" \
        -CAcreateserial -days 825 -extfile "${certificate_work}/server.ext" \
        -out "${certificate_work}/server.crt"
    install -m 0644 -o root -g root "${certificate_work}/ca.crt" /etc/messenger-worksheets/ca.crt
    install -m 0644 -o root -g worksheet-worker "${certificate_work}/server.crt" \
        /etc/messenger-worksheets/worker/server.crt
    install -m 0640 -o root -g worksheet-worker "${certificate_work}/server.key" \
        /etc/messenger-worksheets/worker/server.key
fi

worker_tmp=$(mktemp)
gateway_tmp=$(mktemp)
trap 'rm -f -- "${worker_tmp}" "${gateway_tmp}"; [[ -z ${certificate_work:-} ]] || rm -rf -- "${certificate_work}"' EXIT
messenger_oauth=
openai_key=
read_secret "Personal Yandex Messenger OAuth" messenger_oauth
read_secret "OpenAI API key" openai_key
transport_token=$(openssl rand -hex 32)
{
    printf 'WORKSHEET_CODEX_PROVIDER=openai\n'
    printf 'WORKSHEET_CODEX_BINARY=/usr/local/bin/codex\n'
    printf 'WORKSHEET_CODEX_LISTEN=127.0.0.1\n'
    printf 'WORKSHEET_CODEX_PORT=8443\n'
    printf 'WORKSHEET_CODEX_TLS_CERT=/etc/messenger-worksheets/worker/server.crt\n'
    printf 'WORKSHEET_CODEX_TLS_KEY=/etc/messenger-worksheets/worker/server.key\n'
    printf 'WORKSHEET_CODEX_TRANSPORT_TOKEN=%s\n' "${transport_token}"
    printf 'WORKSHEET_CODEX_API_KEY=%s\n' "${openai_key}"
} > "${worker_tmp}"
{
    printf 'WORKSHEET_MESSENGER_CLIENT_DIR=/opt/messenger-worksheets/messenger-client\n'
    printf 'WORKSHEET_MESSENGER_TOKEN=%s\n' "${messenger_oauth}"
    printf 'WORKSHEET_CODEX_WORKER_URL=https://127.0.0.1:8443/v1/codex-worksheet-jobs\n'
    printf 'WORKSHEET_CODEX_WORKER_CA=/etc/messenger-worksheets/ca.crt\n'
    printf 'WORKSHEET_CODEX_TRANSPORT_TOKEN=%s\n' "${transport_token}"
} > "${gateway_tmp}"
install -m 0600 -o root -g root "${worker_tmp}" /etc/messenger-worksheets/worker.env
install -m 0600 -o root -g root "${gateway_tmp}" /etc/messenger-worksheets/gateway.env
unset messenger_oauth openai_key transport_token

install -m 0644 -o root -g root "${deployment_source}/worksheet-worker.service" \
    /etc/systemd/system/worksheet-worker.service
install -m 0644 -o root -g root "${deployment_source}/worksheet-gateway.service" \
    /etc/systemd/system/worksheet-gateway.service
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/worksheet-worker.service \
    /etc/systemd/system/worksheet-gateway.service

echo "Installation prepared. Services were not enabled or started."
