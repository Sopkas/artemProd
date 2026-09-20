#!/usr/bin/env bash
# Установка службы «Помощник претензионщика» на Linux с systemd (S6-01).
# Запускать от root на чистом сервере (Ubuntu 22.04/24.04 или Debian 12):
#   sudo bash deploy/install.sh [git-ref]
# Скрипт идемпотентен: повторный запуск обновляет код и зависимости, не трогая данные и /etc.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Sopkas/artemProd.git}"
REF="${1:-main}"
APP_USER=claims
BASE=/opt/claims-assistant
APP="$BASE/app"
VENV="$BASE/venv"
DATA=/var/lib/claims-assistant
CONF=/etc/claims-assistant

require() { command -v "$1" >/dev/null 2>&1 || { echo "Нужна команда: $1" >&2; exit 1; }; }
require git
require systemctl

PYTHON="${PYTHON:-}"
for candidate in python3.13 python3.12 python3; do
  if [[ -z "$PYTHON" ]] && command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
      PYTHON="$candidate"
    fi
  fi
done
[[ -n "$PYTHON" ]] || { echo "Нужен Python 3.12+ (apt install python3.12 python3.12-venv)" >&2; exit 1; }

# 1. Отдельный пользователь без входа и каталоги с правами только для него.
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$BASE" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP" "$DATA/uploads" "$DATA/backups" "$CONF"
chown -R "$APP_USER:$APP_USER" "$DATA"
chmod 750 "$DATA" "$DATA/uploads" "$DATA/backups"

# 2. Код: клон или обновление до указанного ref.
if [[ -d "$APP/.git" ]]; then
  git -C "$APP" fetch --tags --prune origin
else
  git clone "$REPO_URL" "$APP"
fi
git -C "$APP" checkout --quiet "$REF"
if git -C "$APP" show-ref --verify --quiet "refs/remotes/origin/$REF"; then
  git -C "$APP" merge --ff-only "origin/$REF"
fi
chown -R "$APP_USER:$APP_USER" "$APP"

# 3. Отдельное окружение и зависимости из зафиксированных версий.
[[ -x "$VENV/bin/python" ]] || "$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$APP/requirements.txt"
"$VENV/bin/python" -m pip install --quiet --no-build-isolation --no-deps -e "$APP"
"$VENV/bin/python" -m pip check
chown -R "$APP_USER:$APP_USER" "$VENV"

# 4. Настройки: шаблон кладётся один раз, значения заполняет администратор.
if [[ ! -f "$CONF/env" ]]; then
  install -o root -g "$APP_USER" -m 0640 "$APP/deploy/env.example" "$CONF/env"
  echo "Заполните $CONF/env (TELEGRAM_BOT_TOKEN, ALLOWED_TELEGRAM_IDS) и запустите скрипт снова."
fi

# 5. Юниты systemd: бот и ежедневная копия.
install -m 0644 "$APP/deploy/claims-assistant.service" /etc/systemd/system/
install -m 0644 "$APP/deploy/claims-assistant-backup.service" /etc/systemd/system/
install -m 0644 "$APP/deploy/claims-assistant-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable claims-assistant.service claims-assistant-backup.timer >/dev/null

# 6. Проверка настроек тем же кодом, что и служба; без токена — остановиться здесь.
if grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$CONF/env"; then
  su -s /bin/sh "$APP_USER" -c "set -a; . '$CONF/env'; set +a; cd '$APP' && '$VENV/bin/python' -m claims_assistant --check"
  systemctl restart claims-assistant.service
  systemctl start claims-assistant-backup.timer
  echo "Служба запущена. Журнал: journalctl -u claims-assistant -f"
else
  echo "Токен не задан — служба установлена, но не запущена."
fi
