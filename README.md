# AA2 — Android Auto head unit (Python)

## Запуск

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./run.sh --help
# пример (книжный экран):
./run.sh --video-debug -r 720x1280 --video-preset 720x1280 --video-scale stretch
```

В **закрытом** репозитории в git лежит вся папка `certs/` (включая ключи): клонируйте репозиторий или скопируйте каталог — можно работать на другой машине без ручной подстановки `.key`.

## Скрипт `sync.sh` (коммит + push)

1. Один раз: **`./sync.sh setup`** — создаст **`sync.local.conf`** (URL репозитория) и **`sync.secrets`** (логин и токен). Либо скопируйте примеры вручную.

2. В **`sync.local.conf`** укажите `REMOTE_URL` (HTTPS `https://github.com/…/….git`).

3. В **`sync.secrets`** укажите `GITHUB_USER` и **`GITHUB_TOKEN`** (Personal Access Token). Эти файлы в git **не** коммитятся; скрипт записывает токен в локальный **`credential.helper`** для этого репозитория (файл `.git-credentials-local`, тоже не в git).

4. Забрать с GitHub: `./sync.sh pull`

5. Отправить: `./sync.sh push "сообщение"` или `./sync.sh "сообщение"`

Скрипт выполняет `git add -A`, при наличии правок — `commit`, затем `push` на `origin/$BRANCH`.

Если используете **SSH** (`git@github.com:…`), токен в `sync.secrets` не нужен — оставьте переменные пустыми или не создавайте `sync.secrets`.

## Git и GitHub (вручную)

Первый push (создайте пустой репозиторий на GitHub, без README):

```bash
cd ~/Desktop/AA2
git remote add origin https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПО.git
git branch -M main
git push -u origin main
```

Дальше — обычный цикл:

```bash
git status
git add -p   # или git add файл(ы)
git commit -m "Кратко: что сделано"
git push
```

Откат без потери истории:

```bash
git log --oneline -5
git revert <hash_коммита>   # новый коммит, отменяющий выбранный
```

Вернуть рабочую копию к последнему коммиту (осторожно: теряются несохранённые правки):

```bash
git checkout -- .
git restore .
```

Метки для «снимков»:

```bash
git tag -a v0.1 -m "Рабочий портрет"
git push origin v0.1
```

Вернуться к тегу: `git checkout v0.1` (detached HEAD) или завести ветку: `git switch -c fix-from-v0.1 v0.1`.
