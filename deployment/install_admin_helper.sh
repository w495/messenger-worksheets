#!/usr/bin/env sh
# shellcheck enable=all
# shfmt -p -s -i 2 -kp -sr -ci -d

# Установку выполняет root: файлы приложения и список sudo-команд
# не должны становиться доступны для записи оператору или worker.
set -eu
umask 077

effective_uid=$(id -u)
if [ "${effective_uid}" -ne 0 ]; then
  echo 'Run this installer with sudo.' >&2
  exit 1
fi
if ! id -u w495 > /dev/null 2>&1; then
  echo 'Expected operator account w495 is missing.' >&2
  exit 1
fi

# Короткие постоянные пути используют все последующие шаги.
# $0 работает и при прямом запуске, и при запуске через sh или bash.
deployment_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH='' cd -- "${deployment_dir}/.." && pwd)
base=/opt/messenger-worksheets
app=${base}/app
config=/etc/messenger-worksheets
account=worksheet-chat-admin
account_home=/var/lib/messenger-worksheets-chat-admin
package=${repo_root}/messenger-worksheets

# Домашний каталог административного worker недоступен другим UID.
if ! id -u "${account}" > /dev/null 2>&1; then
  useradd --system --user-group --home-dir "${account_home}" \
    --no-create-home --shell /usr/sbin/nologin "${account}"
fi
install -d -m 0700 -o "${account}" -g "${account}" "${account_home}"

# У каждого профиля собственные CA, сертификат и приватный ключ.
# Генератор создаёт недостающие наборы без копирования ключей
# между worksheet-worker, w495 и worksheet-chat-admin.
if [ ! -s "${config}/ca-api.crt" ] ||
  [ ! -s "${config}/ca-local-user.crt" ] ||
  [ ! -s "${config}/ca-sandbox-admin.crt" ] ||
  [ ! -s "${config}/worker/server.key" ] ||
  [ ! -s "${config}/local-user/server.key" ] ||
  [ ! -s "${config}/sandbox-admin/server.key" ]; then
  "${deployment_dir}/generate_tls.sh"
fi
instance_config=${deployment_dir}/instances.local.json
if [ ! -f "${instance_config}" ]; then
  echo 'Missing deployment/instances.local.json' >&2
  exit 1
fi

# Сначала устанавливается единственный пользовательский клиент.
# Проверка identity и invite использует уже установленный пакет
# и зависимости с проверенными хешами из lock-файла.
install -d -m 0755 -o root -g root "${app}/messenger_user_client"
install -m 0644 -o root -g root \
  "${package}/messenger_user_client/__init__.py" \
  "${package}/messenger_user_client/client.py" \
  "${app}/messenger_user_client/"
install -m 0644 -o root -g root \
  "${package}/requirements.txt" "${package}/requirements.lock" "${app}/"
"${base}/venv/bin/python" -m pip install \
  --disable-pip-version-check --require-hashes \
  --requirement "${app}/requirements.lock"
PYTHONPATH=${app} "${base}/venv/bin/python" \
  "${deployment_dir}/provision_instances.py" \
  --config "${instance_config}"

# Проверка unit-файлов предшествует их установке. Список фиксирован,
# поэтому инсталлятор не принимает произвольные системные службы.
systemd-analyze verify \
  "${deployment_dir}/worksheet-worker.service" \
  "${deployment_dir}/worksheet-worker-local-user.service" \
  "${deployment_dir}/worksheet-worker-sandbox-admin.service" \
  "${deployment_dir}/worksheet-gateway-api.service" \
  "${deployment_dir}/worksheet-gateway-local-user.service" \
  "${deployment_dir}/worksheet-gateway-sandbox-admin.service" \
  "${deployment_dir}/worksheet-gateway-bot.service"

# Обновляются только перечисленные модули; их владельцем остаётся
# root. Старый общий gateway отключается, чтобы два процесса
# не читали и не обрабатывали одни и те же сообщения чата.
for module in bot_api bot_gateway codex_rpc codex_worker model_http \
  model_cards model_catalog model_presentation text_messages \
  personal_messenger worksheet thread_context chat_commands \
  request_options process_supervisor cloud_gateway; do
  install -m 0644 -o root -g root \
    "${package}/messenger_worksheets/${module}.py" \
    "${app}/messenger_worksheets/${module}.py"
done
install -m 0644 -o root -g root \
  "${package}/messenger_codex/bridge.py" \
  "${app}/messenger_codex/bridge.py"
for service in worksheet-worker worksheet-worker-local-user \
  worksheet-worker-sandbox-admin worksheet-gateway-api \
  worksheet-gateway-local-user worksheet-gateway-sandbox-admin \
  worksheet-gateway-bot worksheet-gateway; do
  install -m 0644 -o root -g root \
    "${deployment_dir}/${service}.service" \
    "/etc/systemd/system/${service}.service"
done
systemctl daemon-reload
systemctl disable --now worksheet-gateway.service

# Диагностические команды и сам helper также принадлежат root.
# Необязательная приватная аватарка не входит в публичный репозиторий.
install -d -m 0755 -o root -g root "${base}/admin"
for script in check_bot check_messenger diagnose_openai \
  diagnose_sandbox doctor_openai login_codex smoke_test_worker \
  smoke_test_commands; do
  install -m 0755 -o root -g root "${deployment_dir}/${script}.py" \
    "${base}/admin/${script}.py"
done
if [ -f "${repo_root}/assets/bot-avatar.png" ]; then
  install -d -m 0755 -o root -g root "${base}/assets"
  install -m 0644 -o root -g root "${repo_root}/assets/bot-avatar.png" \
    "${base}/assets/bot-avatar.png"
fi
install -m 0755 -o root -g root "${deployment_dir}/worksheet-admin" \
  /usr/local/sbin/worksheet-admin

# Временный sudoers удаляется и при ошибке, и при сигнале.
# В разрешениях нет шаблонов аргументов: только точные операции
# helper. visudo проверяет файл до установки с правами 0440.
sudoers_tmp=''
cleanup() {
  cleanup_status=$?
  if [ -n "${sudoers_tmp}" ]; then
    rm -f -- "${sudoers_tmp}"
  fi
  trap - 0
  exit "${cleanup_status}"
}
trap cleanup 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
sudoers_tmp=$(mktemp)
sudoers_prefix='w495 ALL=(root) NOPASSWD:'
sudoers_command=/usr/local/sbin/worksheet-admin
for operation in status check-messenger-api check-messenger-local \
  check-bot check-messenger-api-cleanroom \
  check-messenger-local-cleanroom check-tls-isolation \
  diagnose-openai diagnose-sandbox doctor-openai login-codex \
  smoke-api smoke-local set-native-codex login-sandbox-admin \
  smoke-sandbox smoke-sandbox-task smoke-sandbox-link smoke-controls \
  smoke-models set-profile-gpt-5.3-codex-high \
  start-worker-api restart-worker-api stop-worker-api \
  start-worker-local restart-worker-local stop-worker-local \
  start-worker-sandbox restart-worker-sandbox stop-worker-sandbox \
  start-gateway-api stop-gateway-api start-gateway-local \
  stop-gateway-local start-gateway-sandbox stop-gateway-sandbox \
  activate-cleanroom-api activate-cleanroom-sandbox \
  start-gateway-bot restart-gateway-bot stop-gateway-bot \
  activate-sandbox rollback-local activate-bot rollback-userbot \
  start-api-instance stop-api-instance start-local-instance \
  stop-local-instance enable-instances enable-sandbox-instance \
  enable-local-instance enable-bot-instance logs-worker-api \
  logs-worker-local logs-worker-sandbox logs-gateway-api \
  logs-gateway-local logs-gateway-sandbox logs-gateway-bot revoke; do
  printf '%s %s %s\n' "${sudoers_prefix}" \
    "${sudoers_command}" "${operation}"
done > "${sudoers_tmp}"
chmod 0440 "${sudoers_tmp}"
visudo -cf "${sudoers_tmp}"
install -m 0440 -o root -g root "${sudoers_tmp}" \
  /etc/sudoers.d/messenger-worksheets-admin

# Доступ сохраняется до явного запуска операции revoke.
# Удаление временного файла выполняет общий обработчик выхода.
echo 'Restricted worksheet-admin access installed for w495.'
