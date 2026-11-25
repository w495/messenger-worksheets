#!/usr/bin/env sh
# shellcheck enable=all
# shfmt -p -s -i 2 -kp -sr -ci -d

# Ошибка выпуска или проверки прекращает ротацию сертификатов.
set -eu
operator_uid=$(id -u)
if [ "${operator_uid}" -ne 0 ]; then
  printf '%s\n' 'Запустите восстановление TLS через sudo.' >&2
  exit 1
fi

script_directory=$(dirname "$0")
deployment_dir=$(CDPATH='' cd "${script_directory}" && pwd -P)
certificate_dir=/etc/messenger-worksheets
api_service=worksheet-worker.service
local_service=worksheet-worker-local-user.service
sandbox_service=worksheet-worker-sandbox-admin.service
api_active=no
local_active=no
sandbox_active=no

# Запоминаем состояние до ротации и останавливаем активные workers.
# Отключённые ранее профили не будут включены после замены ключей.
if systemctl is-active --quiet "${api_service}"; then
  api_active=yes
  systemctl stop "${api_service}"
fi
if systemctl is-active --quiet "${local_service}"; then
  local_active=yes
  systemctl stop "${local_service}"
fi
if systemctl is-active --quiet "${sandbox_service}"; then
  sandbox_active=yes
  systemctl stop "${sandbox_service}"
fi

"${deployment_dir}/generate_tls.sh"

# Запускает ранее активный worker. Аргументы: unit, порт и файл CA.
# Проверка требует не только systemd active, но и TLS-соединения с новым
# CA. HTTP 401 подтверждает работу RPC без раскрытия transport token.
# Между 40 попытками пауза 250 мс; один HTTP-запрос ограничен секундой.
restart_and_check() {
  check_service=$1
  check_port=$2
  check_ca_file=$3
  systemctl start "${check_service}"
  check_attempt=0
  check_ready=no
  while [ "${check_attempt}" -lt 40 ]; do
    check_code=
    if systemctl is-active --quiet "${check_service}" &&
      check_code=$(
        curl --silent --output /dev/null --write-out '%{http_code}' \
          --max-time 1 --cacert "${check_ca_file}" \
          --request POST --data '{}' \
          "https://127.0.0.1:${check_port}/v1/codex-worksheet-jobs" \
          2> /dev/null
      ) && [ "${check_code}" = 401 ]; then
      check_ready=yes
      break
    fi
    check_attempt=$((check_attempt + 1))
    sleep 0.25
  done
  if [ "${check_ready}" != yes ]; then
    printf '%s: TLS не готов после 40 попыток.\n' \
      "${check_service}" >&2
    exit 1
  fi
  printf '%s: запущен, RPC без токена вернул HTTP 401.\n' \
    "${check_service}"
}

# Восстанавливаем только сервисы, работавшие перед заменой сертификатов.
if [ "${api_active}" = yes ]; then
  restart_and_check "${api_service}" 8443 \
    "${certificate_dir}/ca-api.crt"
fi
if [ "${local_active}" = yes ]; then
  restart_and_check "${local_service}" 8444 \
    "${certificate_dir}/ca-local-user.crt"
fi
if [ "${sandbox_active}" = yes ]; then
  restart_and_check "${sandbox_service}" 8445 \
    "${certificate_dir}/ca-sandbox-admin.crt"
fi
if [ "${api_active}" = no ] && [ "${local_active}" = no ] &&
  [ "${sandbox_active}" = no ]; then
  printf '%s\n' 'Все workers были и остаются остановленными.'
fi
