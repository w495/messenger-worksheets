#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this certificate generator with sudo." >&2
    exit 1
fi
if ! getent group worksheet-worker >/dev/null; then
    echo "The worksheet-worker group does not exist." >&2
    exit 1
fi

destination=/etc/messenger-worksheets
certificate_work=$(mktemp -d)
cleanup() {
    rm -rf -- "${certificate_work}"
}
trap cleanup EXIT

openssl genpkey -algorithm ED25519 -out "${certificate_work}/ca.key"
openssl req -x509 -new -key "${certificate_work}/ca.key" -days 825 \
    -subj '/CN=messenger-worksheets-local-ca' \
    -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' \
    -addext 'subjectKeyIdentifier=hash' \
    -out "${certificate_work}/ca.crt"

openssl genpkey -algorithm ED25519 -out "${certificate_work}/server.key"
openssl req -new -key "${certificate_work}/server.key" \
    -subj '/CN=localhost' \
    -addext 'basicConstraints=critical,CA:FALSE' \
    -addext 'keyUsage=critical,digitalSignature' \
    -addext 'extendedKeyUsage=serverAuth' \
    -addext 'subjectAltName=IP:127.0.0.1,DNS:localhost' \
    -out "${certificate_work}/server.csr"
openssl x509 -req -in "${certificate_work}/server.csr" \
    -CA "${certificate_work}/ca.crt" -CAkey "${certificate_work}/ca.key" \
    -CAcreateserial -days 825 -copy_extensions copy \
    -out "${certificate_work}/server.crt"

openssl verify -x509_strict -CAfile "${certificate_work}/ca.crt" -purpose sslserver \
    "${certificate_work}/server.crt"
openssl x509 -in "${certificate_work}/ca.crt" -noout -text | \
    grep -q 'Certificate Sign'

install -d -m 0755 -o root -g root "${destination}"
install -d -m 0750 -o root -g worksheet-worker "${destination}/worker"
install -m 0644 -o root -g root "${certificate_work}/ca.crt" "${destination}/ca.crt"
install -m 0644 -o root -g worksheet-worker "${certificate_work}/server.crt" \
    "${destination}/worker/server.crt"
install -m 0640 -o root -g worksheet-worker "${certificate_work}/server.key" \
    "${destination}/worker/server.key"
if getent group operator >/dev/null && [[ -d ${destination}/local-user ]]; then
    install -m 0644 -o root -g operator "${certificate_work}/server.crt" \
        "${destination}/local-user/server.crt"
    install -m 0640 -o root -g operator "${certificate_work}/server.key" \
        "${destination}/local-user/server.key"
fi

echo "TLS certificate chain generated and verified."
