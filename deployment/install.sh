#!/usr/bin/env sh
# shellcheck enable=all
# shfmt -p -s -i 2 -kp -sr -ci -d

# Установка рассчитана на Linux с systemd. Ошибки нельзя пропускать:
# конфигурация и ключи должны появиться до запуска сервисов.
set -eu
umask 077
operator_uid=$(id -u)
if [ "${operator_uid}" -ne 0 ]; then
  printf '%s\n' 'Запустите установщик через sudo.' >&2
  exit 1
fi

script_directory=$(dirname "$0")
bundle_root=$(CDPATH='' cd "${script_directory}/.." && pwd -P)
app_source=${bundle_root}/messenger-worksheets
deployment_source=${bundle_root}/deployment
install_root=/opt/messenger-worksheets
app_target=${install_root}/app
venv_target=${install_root}/venv
config_target=/etc/messenger-worksheets
unit_target=/etc/systemd/system

# Проверяем комплект поставки до создания пользователей и записи файлов.
for required in \
  "${app_source}/messenger_worksheets/cloud_gateway.py" \
  "${app_source}/messenger_worksheets/codex_rpc.py" \
  "${app_source}/messenger_user_client/client.py" \
  "${app_source}/requirements.lock" \
  "${deployment_source}/worksheet-worker.service" \
  "${deployment_source}/worksheet-gateway.service"; do
  if [ ! -f "${required}" ]; then
    printf 'В комплекте нет файла: %s\n' "${required}" >&2
    exit 1
  fi
done

# Каталог с секретами получает 0700, временные файлы — 0600.
# Обработчик выхода восстанавливает TTY при прерванном вводе.
# Код выхода сохраняется явно, включая отмену в оболочке mksh.
secret_directory=$(mktemp -d)
terminal_state=
cleanup() {
  cleanup_status=$?
  if [ -n "${terminal_state}" ]; then
    stty "${terminal_state}" < /dev/tty || :
  fi
  rm -rf "${secret_directory}"
  trap - 0
  exit "${cleanup_status}"
}
trap cleanup 0
trap 'exit 1' 1 2 3 15

# Читает секрет с терминала без эха, результат — в secret_value.
# Присваивание явно выполняет вызывающий код: eval и printf -v не нужны.
# Алфавит исключает переносы, пробелы и синтаксис shell из env-файлов.
# Значение не выводится и не передаётся отдельной внешней команде.
read_secret() {
  terminal_state=$(stty -g < /dev/tty)
  stty -echo < /dev/tty
  printf '%s: ' "$1" > /dev/tty
  if ! IFS= read -r secret_value < /dev/tty; then
    printf '\n' > /dev/tty
    printf '%s\n' 'Не удалось прочитать секрет с терминала.' >&2
    exit 1
  fi
  stty "${terminal_state}" < /dev/tty
  terminal_state=
  printf '\n' > /dev/tty
  case ${secret_value} in
    '' | *[!A-Za-z0-9._~+/=-]*)
      printf '%s\n' 'Секрет пуст или содержит недопустимые символы.' >&2
      exit 1
      ;;
    *) ;;
  esac
}

if ! id -u w495 > /dev/null 2>&1; then
  printf '%s\n' 'Не найдена учётная запись оператора w495.' >&2
  exit 1
fi
for account in \
  worksheet-worker worksheet-gateway worksheet-chat-admin; do
  if ! id -u "${account}" > /dev/null 2>&1; then
    account_home=/var/lib/messenger-worksheets-${account#worksheet-}
    useradd --system --user-group --home-dir "${account_home}" \
      --no-create-home --shell /usr/sbin/nologin "${account}"
  fi
done

# Приложение устанавливается вместе с пользовательским клиентом.
# Runtime-пользователи читают исходники, но не могут подменить код.
install -d -m 0755 -o root -g root "${install_root}" "${app_target}"
rsync -a --exclude='__pycache__/' "${app_source}/" "${app_target}/"
chown -R root:root "${app_target}"
find "${app_target}" -type d -exec chmod 0755 {} +
find "${app_target}" -type f -exec chmod 0644 {} +

# Виртуальное окружение не зависит от пользовательских пакетов Python.
# Зависимости проверяются по хешам из файла поставки.
if ! python3 -m ensurepip --version > /dev/null 2>&1; then
  printf '%s\n' 'Нет ensurepip. Установите пакет python3.14-venv.' >&2
  exit 1
fi
if [ ! -x "${venv_target}/bin/python" ]; then
  python3 -m venv "${venv_target}"
fi
if ! "${venv_target}/bin/python" -m pip --version > /dev/null 2>&1; then
  "${venv_target}/bin/python" -m ensurepip --upgrade
fi
"${venv_target}/bin/python" -m pip install \
  --disable-pip-version-check --require-hashes \
  --requirement "${app_target}/requirements.lock"
chown -R root:root "${venv_target}"

# Не меняем существующие ключи без причины. Выпуск нужен при отсутствии
# файлов или неверном назначении API CA. OpenSSL проверяется отдельно:
# POSIX sh не умеет обнаруживать сбой первого звена через pipefail.
need_certificates=no
for certificate_file in \
  ca-api.crt ca-local-user.crt ca-sandbox-admin.crt \
  worker/server.key local-user/server.key sandbox-admin/server.key; do
  if [ ! -s "${config_target}/${certificate_file}" ]; then
    need_certificates=yes
  fi
done
if [ "${need_certificates}" = no ]; then
  if ! openssl x509 -in "${config_target}/ca-api.crt" -noout -text \
    > "${secret_directory}/ca.txt"; then
    need_certificates=yes
  elif ! grep -q 'Certificate Sign' "${secret_directory}/ca.txt"; then
    need_certificates=yes
  fi
fi
if [ "${need_certificates}" = yes ]; then
  "${deployment_source}/generate_tls.sh"
fi

worker_tmp=${secret_directory}/worker.env
gateway_tmp=${secret_directory}/gateway.env
read_secret 'Личный OAuth Яндекс Мессенджера'
messenger_oauth=${secret_value}
read_secret 'Ключ OpenAI API'
openai_key=${secret_value}
unset secret_value
transport_token=$(openssl rand -hex 32)

# Shell-builtin printf записывает секреты без внешнего процесса и argv.
# Готовые env-файлы устанавливаются root:root с правами 0600.
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
  printf 'WORKSHEET_CODEX_TLS_CERT=%s/worker/server.crt\n' \
    "${config_target}"
  printf 'WORKSHEET_CODEX_TLS_KEY=%s/worker/server.key\n' \
    "${config_target}"
  printf 'WORKSHEET_CODEX_TRANSPORT_TOKEN=%s\n' "${transport_token}"
  printf 'WORKSHEET_CODEX_API_KEY=%s\n' "${openai_key}"
} > "${worker_tmp}"
{
  printf 'WORKSHEET_MESSENGER_BACKEND=cleanroom\n'
  printf 'WORKSHEET_MESSENGER_TOKEN=%s\n' "${messenger_oauth}"
  printf 'WORKSHEET_CODEX_WORKER_URL=%s/v1/codex-worksheet-jobs\n' \
    'https://127.0.0.1:8443'
  printf 'WORKSHEET_CODEX_TASK_RPC_TIMEOUT=1860\n'
  printf 'WORKSHEET_CODEX_CHAT_RPC_TIMEOUT=1860\n'
  printf 'WORKSHEET_CODEX_WORKER_CA=%s/ca-api.crt\n' "${config_target}"
  printf 'WORKSHEET_CODEX_TRANSPORT_TOKEN=%s\n' "${transport_token}"
} > "${gateway_tmp}"
chmod 0600 "${worker_tmp}" "${gateway_tmp}"
install -m 0600 -o root -g root "${worker_tmp}" \
  "${config_target}/worker.env"
install -m 0600 -o root -g root "${gateway_tmp}" \
  "${config_target}/gateway.env"
unset messenger_oauth openai_key transport_token

# Проверяем units. Оператор запустит сервисы после smoke-проверки.
install -m 0644 -o root -g root \
  "${deployment_source}/worksheet-worker.service" \
  "${unit_target}/worksheet-worker.service"
install -m 0644 -o root -g root \
  "${deployment_source}/worksheet-gateway.service" \
  "${unit_target}/worksheet-gateway.service"
systemctl daemon-reload
systemd-analyze verify "${unit_target}/worksheet-worker.service" \
  "${unit_target}/worksheet-gateway.service"
printf '%s\n' 'Установка подготовлена. Сервисы ещё не запущены.'
