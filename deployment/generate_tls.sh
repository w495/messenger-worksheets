#!/usr/bin/env sh
# shellcheck enable=all
# shfmt -p -s -i 2 -kp -sr -ci -d

# Любая ошибка прерывает выпуск: неполную цепочку нельзя устанавливать.
# Новые ключи и временные файлы по умолчанию доступны только root.
set -eu
umask 077

operator_uid=$(id -u)
if [ "${operator_uid}" -ne 0 ]; then
  printf '%s\n' 'Запустите генератор сертификатов через sudo.' >&2
  exit 1
fi
for required_group in worksheet-worker w495 worksheet-chat-admin; do
  if ! getent group "${required_group}" > /dev/null; then
    printf 'Нет обязательной группы: %s\n' "${required_group}" >&2
    exit 1
  fi
done

destination=/etc/messenger-worksheets
certificate_work=$(mktemp -d)

# Удаляет временные CA-ключи, CSR и серийные номера при любом выходе.
# Путь создаёт mktemp; пользователь не может выбрать цель удаления.
# Код выхода сохраняется явно, включая отмену в оболочке mksh.
cleanup() {
  cleanup_status=$?
  rm -rf "${certificate_work}"
  trap - 0
  exit "${cleanup_status}"
}
trap cleanup 0
trap 'exit 1' 1 2 3 15

# Выпускает отдельную CA и серверный сертификат для одного профиля.
# Аргументы: профиль, Unix-группа, подкаталог назначения и имя CA.
# Разные CA изолируют доверие API, local-user и sandbox-admin.
# CA-ключ не покидает временный каталог. Серверный ключ получает 0640
# и группу единственного worker. Runtime не может менять исходники.
# Переменные имеют префикс: POSIX sh не предоставляет local.
generate_profile() {
  profile_name=$1
  profile_group=$2
  profile_directory=$3
  profile_ca_name=$4
  profile_work=${certificate_work}/${profile_name}
  profile_target=${destination}/${profile_directory}
  install -d -m 0700 "${profile_work}"
  openssl genpkey -algorithm ED25519 -out "${profile_work}/ca.key"
  openssl req -x509 -new -key "${profile_work}/ca.key" -days 825 \
    -subj "/CN=messenger-worksheets-${profile_name}-ca" \
    -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' \
    -addext 'subjectKeyIdentifier=hash' \
    -out "${profile_work}/ca.crt"
  openssl genpkey -algorithm ED25519 \
    -out "${profile_work}/server.key"
  openssl req -new -key "${profile_work}/server.key" \
    -subj '/CN=localhost' \
    -addext 'basicConstraints=critical,CA:FALSE' \
    -addext 'keyUsage=critical,digitalSignature' \
    -addext 'extendedKeyUsage=serverAuth' \
    -addext 'subjectAltName=IP:127.0.0.1,DNS:localhost' \
    -out "${profile_work}/server.csr"
  openssl x509 -req -in "${profile_work}/server.csr" \
    -CA "${profile_work}/ca.crt" -CAkey "${profile_work}/ca.key" \
    -CAcreateserial -days 825 -copy_extensions copy \
    -out "${profile_work}/server.crt"
  openssl verify -x509_strict -CAfile "${profile_work}/ca.crt" \
    -purpose sslserver "${profile_work}/server.crt"

  # В POSIX sh нет pipefail. Раздельные команды позволяют обнаружить
  # ошибку OpenSSL, даже если следующий grep мог бы завершиться успешно.
  openssl x509 -in "${profile_work}/ca.crt" -noout -text \
    > "${profile_work}/ca.txt"
  grep -q 'Certificate Sign' "${profile_work}/ca.txt"
  install -d -m 0750 -o root -g "${profile_group}" "${profile_target}"
  install -m 0644 -o root -g root "${profile_work}/ca.crt" \
    "${destination}/${profile_ca_name}"
  install -m 0644 -o root -g "${profile_group}" \
    "${profile_work}/server.crt" "${profile_target}/server.crt"
  install -m 0640 -o root -g "${profile_group}" \
    "${profile_work}/server.key" "${profile_target}/server.key"
}

install -d -m 0755 -o root -g root "${destination}"
generate_profile api worksheet-worker worker ca-api.crt
generate_profile local-user w495 local-user ca-local-user.crt
generate_profile sandbox-admin worksheet-chat-admin \
  sandbox-admin ca-sandbox-admin.crt

# Однопрофильная установка использует ca.crt как копию только API CA.
# Эта копия не добавляет доверие к local-user или sandbox-admin.
install -m 0644 -o root -g root "${destination}/ca-api.crt" \
  "${destination}/ca.crt"
printf '%s\n' 'Цепочки TLS созданы и проверены.'
