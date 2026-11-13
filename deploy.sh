#!/usr/bin/env bash
# Обновление бота на сервере одной командой:
#   bash deploy.sh               — обновить до ветки main
#   BRANCH=имя bash deploy.sh    — обновить до другой ветки (например, до PR)
#   bash deploy.sh rollback      — откатиться на коммит, который был до последнего деплоя
# Перед обновлением делает бэкап базы, .env и локальных правок в backups/<дата>/.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
DIR="$(pwd)"
BRANCH="${BRANCH:-main}"

# Сервис systemd, который запускает бота из этой папки (можно задать вручную: SERVICE=имя).
if [[ -z "${SERVICE:-}" ]]; then
  SERVICE="$(grep -lsE "WorkingDirectory=${DIR}/?$" /etc/systemd/system/*.service | head -1 | xargs -r basename | sed 's/\.service$//')"
fi
[[ -n "${SERVICE:-}" ]] || { echo "❌ Не нашёл systemd-сервис для $DIR. Запусти так: SERVICE=имя bash deploy.sh"; exit 1; }
echo "▶ Папка: $DIR | сервис: $SERVICE | ветка: $BRANCH"

restart_and_show() {
  sudo systemctl restart "$SERVICE"
  sleep 6
  systemctl --no-pager --lines=0 status "$SERVICE" | head -5 || true
  echo "──── последние логи ────"
  sudo journalctl -u "$SERVICE" -n 30 --no-pager || true
}

if [[ "${1:-}" == "rollback" ]]; then
  PREV="$(cat .last_deploy_from 2>/dev/null || true)"
  [[ -n "$PREV" ]] || { echo "❌ Нет сохранённого коммита для отката"; exit 1; }
  echo "↩ Откат на $PREV"
  git checkout -q "$PREV"
  restart_and_show
  exit 0
fi

# 1) Бэкап
TS="$(date +%Y%m%d-%H%M%S)"
BK="backups/$TS"
mkdir -p "$BK"
if command -v sqlite3 >/dev/null && [[ -f credits.db ]]; then
  sqlite3 credits.db ".backup '$BK/credits.db'"
else
  cp -a credits.db* "$BK/" 2>/dev/null || true
fi
cp -a .env cards.json "$BK/" 2>/dev/null || true
git rev-parse HEAD > .last_deploy_from
git diff > "$BK/local_changes.patch" || true
echo "✔ Бэкап: $DIR/$BK"

# 2) Локальные правки на сервере не теряем: прячем в stash (патч тоже лежит в бэкапе)
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "⚠ На сервере были локальные правки — сохраняю в git stash ($TS)"
  git stash push -m "server-local-$TS" >/dev/null
fi

# 3) Обновление кода
git fetch -q origin "$BRANCH"
git checkout -q "$BRANCH" 2>/dev/null || git checkout -q -b "$BRANCH" "origin/$BRANCH"
git reset -q --hard "origin/$BRANCH"
echo "✔ Код: $(git log -1 --format='%h %s')"

# 4) Зависимости и проверка синтаксиса
PY="python3"
for v in venv .venv env; do [[ -x "$v/bin/python" ]] && PY="$v/bin/python" && break; done
"$PY" -m pip install -q -r requirements.txt
"$PY" -m py_compile bot.py db.py utils/*.py
"$PY" -c "from utils import deck; n=len(deck.all_cards()); assert n == 78, n; print('✔ Колода:', n, 'карт')"

# 5) Перезапуск
restart_and_show
echo "Готово. Если что-то пошло не так: bash deploy.sh rollback"
