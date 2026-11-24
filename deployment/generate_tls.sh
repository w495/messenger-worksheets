#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this certificate generator with sudo." >&2
    exit 1
fi
for required_group in worksheet-worker w495 worksheet-chat-admin; do
    if ! getent group "${required_group}" >/dev/null; then
        echo "Required group ${required_group} does not exist." >&2
        exit 1
    fi
done

destination=/etc/messenger-worksheets
certificate_work=$(mktemp -d)
# Удаляет временный каталог вместе с CA private key, server key, CSR и serial
# при любом выходе. Путь создаётся mktemp и не зависит от пользовательского ввода.
cleanup() {
    rm -rf -- "${certificate_work}"
}
trap cleanup EXIT

# Создаёт независимую CA/leaf/key цепочку для одного worker-профиля. Разные CA
# не позволяют full-access sandbox предъявить собственный валидный сертификат
# gateway другого профиля. CA key остаётся только во временном каталоге; leaf key
# устанавливается mode 0640 для единственной Unix-группы worker.
generate_profile() {
    local profile=$1 group=$2 directory=$3 ca_name=$4 work
    work=${certificate_work}/${profile}
    install -d -m 0700 "${work}"
    openssl genpkey -algorithm ED25519 -out "${work}/ca.key"
    openssl req -x509 -new -key "${work}/ca.key" -days 825 \
        -subj "/CN=messenger-worksheets-${profile}-ca" \
        -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
        -addext 'keyUsage=critical,keyCertSign,cRLSign' \
        -addext 'subjectKeyIdentifier=hash' \
        -out "${work}/ca.crt"
    openssl genpkey -algorithm ED25519 -out "${work}/server.key"
    openssl req -new -key "${work}/server.key" \
        -subj '/CN=localhost' \
        -addext 'basicConstraints=critical,CA:FALSE' \
        -addext 'keyUsage=critical,digitalSignature' \
        -addext 'extendedKeyUsage=serverAuth' \
        -addext 'subjectAltName=IP:127.0.0.1,DNS:localhost' \
        -out "${work}/server.csr"
    openssl x509 -req -in "${work}/server.csr" \
        -CA "${work}/ca.crt" -CAkey "${work}/ca.key" \
        -CAcreateserial -days 825 -copy_extensions copy \
        -out "${work}/server.crt"
    openssl verify -x509_strict -CAfile "${work}/ca.crt" -purpose sslserver \
        "${work}/server.crt"
    openssl x509 -in "${work}/ca.crt" -noout -text | grep -q 'Certificate Sign'
    install -d -m 0750 -o root -g "${group}" "${destination}/${directory}"
    install -m 0644 -o root -g root "${work}/ca.crt" "${destination}/${ca_name}"
    install -m 0644 -o root -g "${group}" "${work}/server.crt" \
        "${destination}/${directory}/server.crt"
    install -m 0640 -o root -g "${group}" "${work}/server.key" \
        "${destination}/${directory}/server.key"
}

install -d -m 0755 -o root -g root "${destination}"
generate_profile api worksheet-worker worker ca-api.crt
generate_profile local-user w495 local-user ca-local-user.crt
generate_profile sandbox-admin worksheet-chat-admin sandbox-admin ca-sandbox-admin.crt
# Старый single-instance gateway ожидает ca.crt; на переходный период это
# точная копия API CA, а не доверие к local/sandbox сертификатам.
install -m 0644 -o root -g root "${destination}/ca-api.crt" "${destination}/ca.crt"

echo "TLS certificate chain generated and verified."
