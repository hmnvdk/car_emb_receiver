# AA2 — Android Auto head unit (Python)

В **git попадают только** этот файл **`README.md`** и каталог **`source/`** (код, `certs/`, `proto_gen/`, `run.sh` и т.д.). Остальное в корне рабочей копии (локальные скрипты синхронизации и т.п.) в репозиторий **не** коммитится.

## Запуск

```bash
cd source
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./run.sh --help
# пример (книжный экран):
./run.sh --video-debug -r 720x1280 --video-preset 720x1280 --video-scale stretch
```

В **закрытом** репозитории в git лежит вся папка `source/certs/` (включая ключи): клонируйте репозиторий — можно работать на другой машине без ручной подстановки `.key`.

## Скрипт синхронизации с GitHub (`sync.sh`)

Файлы **`sync.sh`**, **`sync.local.conf.example`**, **`sync.secrets.example`** в этот репозиторий **не входят** — держите их у себя в каталоге **над** `source/` (например `~/Desktop/AA2/sync.sh` рядом с клоном) или восстановите из бэка.

1. Положите **`sync.sh`** и примеры конфигов рядом с папкой `source/` (корень проекта на диске).
2. **`./sync.sh setup`** — создаст `sync.local.conf` и `sync.secrets`.
3. В **`sync.secrets`** — `GITHUB_USER` и **`GITHUB_TOKEN`** для HTTPS.
4. **`./sync.sh pull`** / **`./sync.sh "сообщение коммита"`** — из каталога, где лежит `.git` (обычно родитель `source/`).

Подробности по токену и SSH — в комментариях к локальным `sync.*.example`.

## Git вручную

```bash
cd …/корень-репозитория   # где лежит README.md и source/
git add README.md source .gitignore
git status
git commit -m "…"
git push
```

Откат: `git log --oneline`, `git revert <hash>`; метки: `git tag -a v0.1 -m "…"`, `git push origin v0.1`.
