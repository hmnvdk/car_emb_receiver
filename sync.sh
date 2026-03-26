#!/bin/bash
# Коммит + push в GitHub по данным из sync.local.conf (не коммитится в git).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONF="$SCRIPT_DIR/sync.local.conf"
EXAMPLE="$SCRIPT_DIR/sync.local.conf.example"

usage() {
    echo "Использование:"
    echo "  $0 setup              — создать sync.local.conf из примера"
    echo "  $0 pull               — git pull"
    echo "  $0 push [сообщение]   — git add -A, commit (если есть изменения), push"
    echo "  $0 \"краткий текст\"    — то же, что push с сообщением коммита"
    echo ""
    echo "Перед первым использованием: $0 setup  и пропишите REMOTE_URL в sync.local.conf"
}

ensure_conf() {
    if [[ ! -f "$CONF" ]]; then
        echo "Нет $CONF — выполните:  $0 setup"
        exit 1
    fi
    # shellcheck disable=SC1090
    source "$CONF"
    if [[ -z "${REMOTE_URL:-}" ]]; then
        echo "В $CONF не задан REMOTE_URL"
        exit 1
    fi
    BRANCH="${BRANCH:-main}"
}

ensure_remote() {
    ensure_conf
    if git remote get-url origin >/dev/null 2>&1; then
        local cur
        cur="$(git remote get-url origin)"
        if [[ "$cur" != "$REMOTE_URL" ]]; then
            echo "Ошибка: в git origin = $cur"
            echo "        в $CONF REMOTE_URL = $REMOTE_URL"
            echo "Выполните один раз: git remote set-url origin \"$REMOTE_URL\""
            echo "или поправьте REMOTE_URL в конфиге."
            exit 1
        fi
    else
        git remote add origin "$REMOTE_URL"
        echo "Добавлен remote origin → $REMOTE_URL"
    fi
}

cmd_setup() {
    if [[ -f "$CONF" ]]; then
        echo "Уже есть $CONF — отредактируйте вручную или удалите и запустите setup снова."
        exit 0
    fi
    if [[ ! -f "$EXAMPLE" ]]; then
        echo "Нет файла $EXAMPLE"
        exit 1
    fi
    cp "$EXAMPLE" "$CONF"
    echo "Создан $CONF — откройте и укажите REMOTE_URL (и при необходимости BRANCH)."
    exit 0
}

remote_has_branch() {
    # есть ли refs/heads/$1 на origin после fetch
    git ls-remote --heads origin "$1" 2>/dev/null | grep -q .
}

cmd_pull() {
    ensure_remote
    git fetch origin

    if ! remote_has_branch "$BRANCH"; then
        echo "На GitHub нет ветки «$BRANCH» (или репозиторий ещё пустой)."
        rh=$(git ls-remote --heads origin 2>/dev/null | awk '{print $2}' | sed 's|refs/heads/||' | tr '\n' ' ')
        if [[ -z "${rh// }" ]]; then
            echo "Удалённый репозиторий пуст: сначала отправьте код:"
            echo "  $0 \"первый коммит\""
        else
            echo "Ветки на origin: $rh"
            echo "Задайте в sync.local.conf подходящую ветку, например: BRANCH=\"master\""
        fi
        exit 1
    fi

    git pull origin "$BRANCH"
}

cmd_push() {
    local msg="${1:-}"
    if [[ -z "$msg" ]]; then
        echo "Укажите сообщение коммита, например:  $0 push \"fix: тач\""
        exit 1
    fi
    ensure_remote

    git add -A
    if git diff --cached --quiet; then
        echo "Нет изменений для коммита (рабочая копия чистая)."
    else
        git commit -m "$msg"
    fi

    git push -u origin "$BRANCH"
    echo "Готово: push → origin/$BRANCH"
}

# --- точка входа ---
case "${1:-}" in
    ""|-h|--help|help)
        usage
        exit 0
        ;;
    setup)
        cmd_setup
        ;;
    pull)
        cmd_pull
        ;;
    push)
        shift
        if [[ $# -eq 0 ]]; then
            echo "Нужно сообщение: $0 push \"текст коммита\""
            exit 1
        fi
        cmd_push "$*"
        ;;
    *)
        # Одна строка как сообщение коммита: ./sync.sh "сообщение"
        cmd_push "$*"
        ;;
esac
