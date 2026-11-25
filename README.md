# atlassian-tools

Локальная копия Confluence и Jira в markdown и правка страниц и задач в их родном формате. Работает с
Confluence и Jira Server / Data Center.

| Команда | Что делает |
|---|---|
| `wiki-sync` | Выгружает пространство вики (или его часть) в markdown и затем переносит правки из вики. В вики не пишет |
| `wiki-edit` | Правит страницу вики в формате хранения Confluence: макросы и оформление сохраняются |
| `jira-sync` | Выгружает релизы и задачи Jira в markdown вместе с вложениями. В Jira не пишет |
| `jira-edit` | Правит описание задачи Jira в её разметке |

Вместе с командами идут скиллы для ИИ-агентов (`skills/`). Они объясняют агенту, как читать копию и
как править страницы и задачи: агент только готовит правку, а записывает её человек.

## Установка

Для установки с GitHub нужны Git и [pipx](https://pipx.pypa.io/stable/how-to/install-pipx.html).
Пакет поддерживает Python 3.9 и новее. Для актуального `pipx` используйте Python 3.11 или новее.

### macOS

Если у вас есть Homebrew, установите `pipx`:

```sh
brew install pipx
pipx ensurepath
```

### Windows (PowerShell)

Установите [Python](https://www.python.org/downloads/windows/) с командой `py` и
[Git for Windows](https://git-scm.com/install/windows), если они ещё не установлены.
Откройте PowerShell и выполните:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

### Установка CLI

После `ensurepath` закройте терминал и откройте его снова. Команда установки из основной
ветки GitHub одинакова для PowerShell и macOS:

```sh
pipx install "git+https://github.com/AleksandrNikolaevich/atlassian-tools.git"
```

Если пакет уже установлен из локальной папки, добавьте `--force` к этой команде, чтобы заменить
установку версией с GitHub. Чтобы установить конкретную версию, укажите опубликованный тег
после `@`, например `@v0.1.0`.

Из локальной копии запускайте команду из корня репозитория:

```sh
pipx install .
```

Для установки библиотеки в активное виртуальное окружение используйте:

```sh
python -m pip install "atlassian-tools @ git+https://github.com/AleksandrNikolaevich/atlassian-tools.git"
```

Проверить установку можно без подключения к серверам:

```sh
wiki-sync --help
wiki-edit --help
jira-sync --help
jira-edit --help
```

### Плагин для Claude Code

Скиллы подключаются как плагин:

```
/plugin marketplace add AleksandrNikolaevich/atlassian-tools
/plugin install atlassian-tools@atlassian-tools
```

Чтобы плагин подключался у всех, кто работает с проектом, добавьте в `.claude/settings.json` проекта:

```json
{
  "extraKnownMarketplaces": {
    "atlassian-tools": {
      "source": { "source": "github", "repo": "AleksandrNikolaevich/atlassian-tools" }
    }
  },
  "enabledPlugins": { "atlassian-tools@atlassian-tools": true }
}
```

Другим агентам (Codex и т. п.) скопируйте папки `skills/confluence` и `skills/jira` туда, где агент ищет
скиллы, например в `.agents/skills/`. Скиллы написаны в открытом формате Agent Skills.

## Настройка проекта

1. Скопируйте [.env.example](.env.example) в корень проекта под именем `.env` и заполните.
2. Добавьте в `.gitignore` проекта:

   ```
   .env
   _wiki-edit/
   _jira/
   _jira-edit/
   ```

   Папку копии вики (`wiki/`) можно коммитить: тогда у команды одна копия, и правки из вики видны в истории.
   Можно и игнорировать: тогда каждый выгружает копию сам.

Команды ищут корень проекта от текущей папки вверх: это первая папка, где есть `.env` или `.git`.
Указать корень явно можно переменной `ATLASSIAN_ROOT`. Все пути в настройках считаются от корня. Одна
установка обслуживает сколько угодно проектов, у каждого свой `.env`.

### Доступ

Для вики подходит один из способов, используется первый заданный:

| Способ | Переменные | Когда подходит |
|---|---|---|
| Логин и пароль | `WIKI_USER`, `WIKI_PASSWORD` | Обычный вход; ничего не истекает |
| Персональный токен | `WIKI_TOKEN` | Если вики разрешает создавать токены |
| Cookie сессии | `WIKI_COOKIE=JSESSIONID=…` | Вход через SSO или капча. Значение берётся из браузера: инструменты разработчика → Cookies. Сессия истекает |

Для Jira те же способы с префиксом `JIRA_`. Если учётная запись общая, Jira использует `WIKI_USER` и
`WIKI_PASSWORD`. Пароль лежит в `.env` открытым текстом: не пересылайте файл.

Если сервер с самоподписанным сертификатом, добавьте к любой команде ключ `--insecure`.

### Что выгружать из вики

| Переменная | Значение |
|---|---|
| `WIKI_SPACE=KEY` | Всё пространство |
| `WIKI_ROOTS=123,456` | Только эти страницы со всеми дочерними. Используется вместо `WIKI_SPACE` |
| `WIKI_EXCLUDE=789` | Эти страницы не выгружать вместе с дочерними: архивы, личные заметки, шаблоны |
| `WIKI_DIR=wiki` | Папка копии |

ID страницы виден в адресе (`pageId=123456`) или в меню «…» → «Информация о странице».

## Вики

### Копия: `wiki-sync`

```sh
wiki-sync check            # что изменилось в вики; ничего не меняет
wiki-sync pull --dry-run   # что перенесётся; файлы не трогает
wiki-sync pull             # перенести
```

Первый `pull` выгружает всю область. Каждая страница становится документом: страница с дочерними — папка
с `README.md`, без дочерних — файл `<название>.md`. Вложенность повторяет дерево вики, картинки лежат в
`_assets/` рядом с документом. Во front matter документа записаны `title`, `wiki_page_id`,
`wiki_version`, `wiki_parent_id` и `wiki_url`.

Следующие запуски переносят только изменения:

- **Правки страниц** накладываются на документ трёхсторонним слиянием: локальные правки сохраняются. Если
  одно и то же место меняли и в вики, и локально, в документе остаются метки конфликта `<<<<<<<`.
- **Новые страницы** раскладываются по дереву. Если у листового документа появилась дочерняя страница,
  документ сам превращается в папку, а ссылки на него обновляются.
- **Переименованные и перенесённые страницы** обновляются во front matter, но сами файлы не
  переименовываются и не перемещаются.
- **Удалённые страницы** и страницы, ушедшие из области синхронизации, `pull` показывает и удаляет только
  после ответа «да». То же с картинками, на которые больше нет ссылок. Отдельно уборка запускается
  командой `wiki-sync clean`.

Документ с незакоммиченными правками `pull` пропускает: закоммитьте их или добавьте `--force`. Прерванный
`pull` можно запустить заново: уже выгруженные страницы повторно не скачиваются.

Остальные команды:

| Команда | Что делает |
|---|---|
| `wiki-sync pull <id> <id>` | Перенести правки только этих страниц |
| `wiki-sync pull <id> --theirs` | Заменить документ версией из вики целиком, без слияния |
| `wiki-sync pull --no-new` / `--no-clean` | Не добавлять новые страницы / не предлагать уборку |
| `wiki-sync accept <id> \| --all` | Отметить правки страницы перенесёнными, не меняя текст |
| `wiki-sync check --report отчёт.md` | Сохранить отчёт сверки в файл |

### Правка страницы: `wiki-edit`

Страница правится в её родном формате (XML Confluence), а не пересобирается из markdown. Поэтому
оформление, макросы и диаграммы остаются как были.

```sh
wiki-edit fetch wiki/раздел/страница.md   # или wiki_page_id, или ссылка на страницу
# правка файла _wiki-edit/<id>.xml — вручную или агентом
wiki-edit diff <id>                       # правка в читаемом виде; без сети
wiki-edit check <id>                      # XML цел, макросы на месте; без сети
wiki-edit push <id>                       # запись в вики после подтверждения
wiki-sync pull                            # правка приходит в копию
```

- `push` не запишет страницу, если её изменили в вики после `fetch`. Тогда нужно сделать
  `fetch --force` и повторить правку.
- `push` не запишет и повреждённый XML или макрос со скопированным `ac:macro-id`.
- Предупреждения (пропал макрос, страница заметно сократилась, ссылка на новое вложение) запись не
  запрещают, но их нужно прочитать.
- Картинку нужно загрузить на страницу в вики вручную до `push`.
- Откат делается в самой вики: «История страницы» → восстановить прежнюю версию.

Новая страница: `wiki-edit new --parent <id> --title "Название"`. С ключом `--template <id>` заготовкой
станет содержимое страницы-образца. `wiki-edit status` показывает, какие страницы сейчас в работе.

## Jira

### Копия: `jira-sync`

```sh
jira-sync check                  # доступ и список релизов, которые выгрузятся
jira-sync pull                   # релизы и их задачи; повторно — только изменённые
jira-sync pull PROJ-1 PROJ-2     # отдельные задачи
```

Выгружаются все невыпущенные релизы проекта `JIRA_PROJECT` и последние выпущенные (`JIRA_RELEASED`, по
умолчанию 5), с родительскими задачами и подзадачами. Результат лежит в `_jira/`:

- `README.md` — список релизов;
- `releases/<релиз>.md` — задачи релиза;
- `issues/<KEY>.md` — задача: поля, описание, подзадачи, связи, вложения, комментарии;
- `attachments/<KEY>/` — вложения.

Ключи `pull`: `--released N`, `--match <регулярное выражение>` (только подходящие релизы), `--version
<название>` (только этот релиз), `--force` (перевыгрузить всё), `--no-attachments`. Вложения больше
`JIRA_MAX_ATTACHMENT_MB` (100) не скачиваются.

### Правка описания: `jira-edit`

```sh
jira-edit fetch PROJ-1          # описание в _jira-edit/PROJ-1.txt
# правка в разметке Jira (wiki markup)
jira-edit diff PROJ-1
jira-edit check PROJ-1          # макросы {code}/{panel}/… закрыты, длина в пределах
jira-edit push PROJ-1           # запись после подтверждения; --quiet — без рассылки уведомлений
jira-sync pull PROJ-1
```

## Ограничения

- Поддерживаются Confluence и Jira Server / Data Center (REST API `/rest/api`, `/rest/api/2`). Cloud не
  проверялся: у него другой вход (email + API-токен через Basic) и адреса.
- Конвертер HTML → markdown рассчитан на обычные страницы: текст, таблицы, списки, информационные
  макросы, код, раскрывающиеся блоки, картинки. Содержимое сложных плагинов (draw.io, Gliffy, отчёты)
  в копию не попадает: в вики оно остаётся нетронутым.
- Таблицы с объёмными ячейками превращаются в набор записей (`### Шаг 1`, поля `- **Колонка:**
  значение`), чтобы их удобно было читать и сравнивать.
- В тексте страниц токены и сессии (`JSESSIONID=…`, JWT) заменяются заглушками.

## Использование из Python

`JiraClient` и `ConfluenceClient` принимают подключение явно. Настройки для них задаёт вызывающий код.
Пример ниже читает токены из окружения:

```python
import os

from atlassian_tools.confluence import ConfluenceClient
from atlassian_tools.jira import JiraClient

jira = JiraClient("https://jira.example.org", token=os.environ["JIRA_TOKEN"])
issues = jira.search("project = PROJ ORDER BY key", fields="summary,status")

wiki = ConfluenceClient("https://wiki.example.org/confluence", token=os.environ["WIKI_TOKEN"])
pages = wiki.pages(["123456"])
```

Для обоих клиентов доступны `user` и `password`, `token` или `cookie`. Приоритет такой же, как у CLI.
Проверка TLS включена по умолчанию. Ошибки запросов возвращаются как исключения `JiraError` и
`WikiError`. У `WikiError` поле `code` содержит HTTP-статус, если он известен.

| Клиент | Основные методы |
|---|---|
| Jira | `get(path, **params)`, `search(jql, fields, expand="")`, `comments(key)`, `download(url, path)`, `request(method, path, payload=None)` |
| Confluence | `get(path, **params)`, `search(cql, expand)`, `pages(ids)`, `space_pages(key)`, `children(parent_ids)`, `descendants(roots, skip=())`, `download(url)`, `send(method, path, payload)` |

Поисковые методы собирают страницы результатов. `pages(ids)` возвращает сведения по ID. Для Jira
`download` сохраняет вложение по указанному пути, для Confluence возвращает байты. Передавайте
в `download` адреса вложений своего сервера.

## Разработка

Из корня этого репозитория:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests
```

В Windows PowerShell из корня репозитория:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Тесты клиентов используют искусственные ответы и проходят без доступа к Jira и Confluence.

Модули в `src/atlassian_tools/`:

| Модуль | Что в нём |
|---|---|
| `config` | Корень проекта и `.env` для CLI |
| `jira`, `confluence` | Клиенты REST API |
| `convert` | HTML → markdown |
| `wiki_pull` | Слияние правок и раскладка новых страниц |
| `storage_view` | Читаемый вид формата хранения для `wiki-edit diff` |
| `wiki_sync`, `wiki_edit`, `jira_sync`, `jira_edit` | Команды |

Сетевые модули используют только стандартную библиотеку. `beautifulsoup4` и `lxml` нужны CLI для
конвертации.

## Сборка и распространение

```sh
python -m pip install build
python -m build
```

В `dist/` появятся wheel и архив исходников. Коллега может установить wheel командой
`pipx install ./dist/atlassian_tools-0.1.0-py3-none-any.whl` или через `python -m pip install`
в окружение своего проекта. Подставьте имя wheel нужной версии.

Для выпуска версии обновите `version` в `pyproject.toml` и опубликуйте соответствующий Git-тег
на GitHub. После публикации тега пользователи смогут установить эту версию:

```sh
pipx install "git+https://github.com/AleksandrNikolaevich/atlassian-tools.git@v0.1.0"
```
