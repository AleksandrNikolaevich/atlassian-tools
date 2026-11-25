"""Локальная копия релизов и задач Jira в markdown: читать задачи можно без VPN.

В Jira не пишет. Только стандартная библиотека Python. Запускать из сети, где Jira доступна (VPN).

    jira-sync check                  # проверить доступ и показать, какие релизы выгрузятся
    jira-sync pull                   # выгрузить релизы и их задачи (изменённые с прошлого раза)
    jira-sync pull PROJ-123 PROJ-456 # выгрузить отдельные задачи
    jira-sync pull --force           # перевыгрузить все задачи заново

Выгружаются все невыпущенные релизы проекта и последние выпущенные (JIRA_RELEASED, по умолчанию 5).
Результат — папка JIRA_DIR (по умолчанию _jira/) в корне проекта; в git её добавлять не нужно:

    _jira/README.md                 список релизов — начинать отсюда
    _jira/releases/<релиз>.md       задачи релиза
    _jira/issues/<KEY>.md           задача: поля, описание, подзадачи, связи, вложения, комментарии
    _jira/attachments/<KEY>/…       вложения задач

Доступ к Jira — один из трёх способов (переменные окружения или .env проекта):
  JIRA_USER + JIRA_PASSWORD  логин и пароль;
  JIRA_TOKEN                 персональный токен Jira;
  JIRA_COOKIE                cookie открытой в браузере сессии, например "JSESSIONID=…".
Если ни один не задан, используются WIKI_USER и WIKI_PASSWORD.
Настройки: JIRA_URL — адрес Jira, JIRA_PROJECT — ключ проекта (нужен для выгрузки релизов), JIRA_RELEASED (5),
JIRA_VERSIONS — регулярное выражение: выгружать только релизы с подходящим названием,
JIRA_MAX_ATTACHMENT_MB (100) — вложения больше не скачиваются, на них остаётся ссылка в Jira.
"""
import argparse
import datetime
import html
import json
import os
import re
import sys
import urllib.parse
from html.parser import HTMLParser

from . import config
from .jira import JiraClient, JiraError

OUT = config.folder("JIRA_DIR", "_jira")
STATE = os.path.join(OUT, ".state.json")
BATCH = 50
LIST_FIELDS = "summary,status,issuetype,priority,assignee,updated,parent,subtasks"
FULL_FIELDS = ("summary,status,issuetype,priority,resolution,assignee,reporter,created,updated,fixVersions,"
               "versions,components,labels,description,comment,attachment,subtasks,issuelinks,parent")


class Jira(JiraClient):
    def __init__(self, insecure=False):
        base = config.get("JIRA_URL")
        if not base:
            raise JiraError("не задан адрес Jira: впишите в .env JIRA_URL=https://…")
        env = os.environ.get
        user, password, token, cookie = (env(f"JIRA_{key}") for key in ("USER", "PASSWORD", "TOKEN", "COOKIE"))
        if not (user or token or cookie) and env("WIKI_USER") and env("WIKI_PASSWORD"):
            user, password = env("WIKI_USER"), env("WIKI_PASSWORD")
        if not ((user and password) or token or cookie):
            raise JiraError("не задан доступ к Jira: впишите в .env JIRA_USER и JIRA_PASSWORD, JIRA_TOKEN или "
                            "JIRA_COOKIE (или WIKI_USER и WIKI_PASSWORD, если учётная запись общая)")
        super().__init__(base, user=user, password=password, token=token, cookie=cookie, insecure=insecure)


# ───────────────────────── HTML Jira → markdown ─────────────────────────

VOID = {"br", "img", "hr", "input", "col", "meta", "link", "wbr"}
BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "pre", "blockquote", "hr", "dl"}


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = ("root", {}, [])
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = (tag, dict(attrs), [])
        self.stack[-1][2].append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1][2].append(data)


class Markdown:
    """Переводит HTML, который Jira отдаёт в renderedFields, в markdown со ссылками на локальные файлы."""

    def __init__(self, link, alt=lambda src: None):
        self.link = link  # href/src из Jira -> адрес в markdown
        self.alt = alt    # src картинки -> подпись (имя вложения)

    def convert(self, source):
        if not source:
            return ""
        tree = Tree()
        tree.feed(source)
        tree.close()
        text = self.blocks(tree.root[2])
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def blocks(self, nodes):
        out, run = [], []
        for node in nodes:
            if isinstance(node, tuple) and node[0] in BLOCK:
                out.append(self.inline_text(run))
                run = []
                out.append(self.block(node))
            else:
                run.append(node)
        out.append(self.inline_text(run))
        return "\n\n".join(part for part in out if part.strip())

    def inline_text(self, nodes):
        text = "".join(self.inline(n) for n in nodes)
        return "\n".join(line.strip() for line in text.split("\n")).strip()

    def block(self, node):
        tag, attrs, kids = node
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            return "#" * min(int(tag[1]) + 2, 6) + " " + self.inline_text(kids).replace("\n", " ")
        if tag in ("ul", "ol"):
            return self.list(node)
        if tag == "table":
            return self.table(node)
        if tag == "pre":
            return "```\n" + self.raw(node).strip("\n") + "\n```"
        if tag == "blockquote":
            return "\n".join("> " + line if line else ">" for line in self.blocks(kids).split("\n"))
        if tag == "hr":
            return "---"
        return self.blocks(kids)

    def list(self, node):
        items, n = [], 0
        for kid in node[2]:
            if not (isinstance(kid, tuple) and kid[0] == "li"):
                continue
            n += 1
            mark = f"{n}. " if node[0] == "ol" else "- "
            body = re.sub(r"\n\n+", "\n", self.blocks(kid[2])) or " "
            lines = body.split("\n")
            items.append(mark + lines[0] + "".join("\n" + " " * len(mark) + line for line in lines[1:]))
        return "\n".join(items)

    def table(self, node):
        rows = []

        def walk(nodes):
            for kid in nodes:
                if not isinstance(kid, tuple):
                    continue
                if kid[0] == "tr":
                    rows.append([self.cell(c) for c in kid[2] if isinstance(c, tuple) and c[0] in ("td", "th")])
                elif kid[0] in ("thead", "tbody", "tfoot"):
                    walk(kid[2])
        walk(node[2])
        rows = [r for r in rows if r]
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(lines)

    def cell(self, node):
        text = self.blocks(node[2])
        return re.sub(r"\n+", "<br>", text).replace("|", "\\|")

    def raw(self, node):
        if isinstance(node, str):
            return node
        if node[0] == "br":
            return "\n"
        return "".join(self.raw(k) for k in node[2])

    def inline(self, node):
        if isinstance(node, str):
            return re.sub(r"\s+", " ", node)
        tag, attrs, kids = node
        if tag in BLOCK:
            return "\n" + self.blocks([node]) + "\n"
        if tag == "br":
            return "\n"
        if tag == "img":
            if "emoticon" in attrs.get("class", ""):
                return attrs.get("alt", "")
            alt = self.alt(attrs.get("src", "")) or attrs.get("alt") or os.path.basename(urllib.parse.unquote(attrs.get("src", ""))) or "картинка"
            return f"![{alt}]({self.link(attrs.get('src', ''))})"
        inner = "".join(self.inline(k) for k in kids)
        if tag in ("b", "strong"):
            return wrap(inner, "**")
        if tag in ("i", "em", "cite"):
            return wrap(inner, "*")
        if tag in ("del", "s", "strike"):
            return wrap(inner, "~~")
        if tag in ("code", "tt", "kbd"):
            return wrap(inner, "`")
        if tag == "a":
            href = attrs.get("href", "")
            if not href or href.startswith(("#", "javascript:")) or "user-hover" in attrs.get("class", ""):
                return inner
            if inner.strip().startswith("![") and inner.strip().endswith(")"):
                return inner  # картинка-превью со ссылкой на оригинал: оставляем саму картинку
            return f"[{inner.strip() or href}]({self.link(href)})"
        if tag in ("script", "style"):
            return ""
        return inner


def wrap(text, mark):
    core = text.strip()
    if not core:
        return text
    lead, trail = text[:len(text) - len(text.lstrip())], text[len(text.rstrip()):]
    return f"{lead}{mark}{core}{mark}{trail}"


# ───────────────────────── файлы копии ─────────────────────────

def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n")


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", name).strip(" .-")
    return name[:120] or "файл"


def dest(path):
    """Адрес для ссылки markdown: в угловых скобках, если в нём пробелы или скобки."""
    return f"<{path}>" if re.search(r"[\s()<>]", path) else path


def cell(text):
    return (text or "").replace("|", "\\|").replace("\n", " ")


def date(value, time=True):
    if not value:
        return ""
    try:
        d = datetime.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
        return d.strftime("%Y-%m-%d %H:%M" if time else "%Y-%m-%d")
    except ValueError:
        return value[:10]


def name_of(value, key="name"):
    return (value or {}).get(key) or ""


def size(n):
    return f"{n / 1048576:.1f} МБ" if n >= 1048576 else f"{max(1, n // 1024)} КБ"


class Mirror:
    def __init__(self, jira, project, versions):
        self.jira, self.project = jira, project
        self.versions = versions                      # id -> версия Jira (только выгружаемые)
        self.slugs = release_slugs(versions.values())  # id -> имя файла релиза без .md
        self.max_bytes = float(os.environ.get("JIRA_MAX_ATTACHMENT_MB", "100")) * 1048576
        self.known = set()                            # ключи задач, у которых есть файл в копии
        issues_dir = os.path.join(OUT, "issues")
        if os.path.isdir(issues_dir):
            self.known = {n[:-3] for n in os.listdir(issues_dir) if n.endswith(".md")}

    def issue_link(self, key, from_dir="issues"):
        if key in self.known:
            return f"{key}.md" if from_dir == "issues" else f"../issues/{key}.md"
        return f"{self.jira.base}/browse/{key}"

    # ── задача ──

    def render_issue(self, issue, attachments):
        key, f, rf = issue["key"], issue["fields"], issue.get("renderedFields") or {}
        local = {}  # id вложения -> путь от папки issues/
        names = {str(a["id"]): a["filename"] for a in attachments}
        for a in attachments:
            if a.get("local"):
                local[str(a["id"])] = a["local"]

        def link(href):
            href = html.unescape(href)
            m = re.search(r"/secure/(?:attachment|thumbnail)/(\d+)/", href)
            if m and m.group(1) in local:
                return dest(local[m.group(1)])
            m = re.search(r"/browse/([A-Z][A-Z0-9_]+-\d+)(?:[?#]|$)", href)
            if m and m.group(1) in self.known:
                return self.issue_link(m.group(1))
            return dest(urllib.parse.urljoin(self.jira.base + "/", href))

        def alt(src):
            m = re.search(r"/secure/(?:attachment|thumbnail)/(\d+)/", src)
            return m and names.get(m.group(1))

        md = Markdown(link, alt)
        lines = [f"# {key}. {f.get('summary', '')}", "",
                 "[Все релизы](../README.md)" + "".join(
                     f" · [{v['name']}]({dest('../releases/' + self.slugs[v['id']] + '.md')})"
                     for v in f.get("fixVersions") or [] if v["id"] in self.slugs), ""]

        def versions(items):
            return ", ".join(f"[{v['name']}]({dest('../releases/' + self.slugs[v['id']] + '.md')})"
                             if v["id"] in self.slugs else v["name"] for v in items or [])

        parent = f.get("parent")
        meta = [
            ("Тип", name_of(f.get("issuetype"))),
            ("Статус", name_of(f.get("status"))),
            ("Приоритет", name_of(f.get("priority"))),
            ("Резолюция", name_of(f.get("resolution"))),
            ("Исполнитель", name_of(f.get("assignee"), "displayName")),
            ("Автор", name_of(f.get("reporter"), "displayName")),
            ("Создана", date(f.get("created"))),
            ("Обновлена", date(f.get("updated"))),
            ("Релиз (Fix Version)", versions(f.get("fixVersions"))),
            ("Затронутые версии", versions(f.get("versions"))),
            ("Компоненты", ", ".join(c["name"] for c in f.get("components") or [])),
            ("Метки", ", ".join(f.get("labels") or [])),
            ("Родительская задача", parent and f"[{parent['key']}]({self.issue_link(parent['key'])}) "
                                               f"{parent['fields'].get('summary', '')}"),
            ("Jira", f"{self.jira.base}/browse/{key}"),
        ]
        lines += [f"- **{k}:** {v}" for k, v in meta if v]

        lines += ["", "## Описание", "", md.convert(rf.get("description")) or "*Описания нет.*"]

        subtasks = f.get("subtasks") or []
        if subtasks:
            lines += ["", "## Подзадачи", "", "| Задача | Название | Статус |", "|---|---|---|"]
            lines += [f"| [{s['key']}]({self.issue_link(s['key'])}) | {cell(s['fields'].get('summary'))} | "
                      f"{cell(name_of(s['fields'].get('status')))} |" for s in subtasks]

        links = []
        for li in f.get("issuelinks") or []:
            other = li.get("outwardIssue") or li.get("inwardIssue")
            if not other:
                continue
            kind = li["type"]["outward" if "outwardIssue" in li else "inward"]
            links.append(f"- {kind}: [{other['key']}]({self.issue_link(other['key'])}) "
                         f"{other['fields'].get('summary', '')} — {name_of(other['fields'].get('status'))}")
        if links:
            lines += ["", "## Связи", ""] + links

        if attachments:
            lines += ["", "## Вложения", ""]
            for a in sorted(attachments, key=lambda a: a.get("created", "")):
                where = dest(a["local"]) if a.get("local") else dest(a["content"])
                note = "" if a.get("local") else " (не скачано — открыть в Jira)"
                lines.append(f"- [{a['filename']}]({where}){note} — {size(a.get('size', 0))}, "
                             f"{name_of(a.get('author'), 'displayName')}, {date(a.get('created'))}")
                if a.get("local") and a.get("mimeType", "").startswith("image/"):
                    lines.append(f"\n  ![{a['filename']}]({where})\n")

        comments = issue.get("_comments", [])
        if comments:
            lines += ["", f"## Комментарии ({len(comments)})"]
            for c in comments:
                lines += ["", f"### {name_of(c.get('author'), 'displayName')}, {date(c.get('created'))}", "",
                          md.convert(c.get("renderedBody") or html.escape(c.get("body") or ""))]
        return "\n".join(lines)

    def save_issue(self, issue, with_attachments):
        key, f = issue["key"], issue["fields"]
        comment = f.get("comment") or {}
        rendered = (issue.get("renderedFields") or {}).get("comment") or {}
        comments = comment.get("comments") or []
        if comment.get("total", len(comments)) > len(comments):
            issue["_comments"] = self.jira.comments(key)
        else:
            bodies = {str(c.get("id")): c.get("body") for c in rendered.get("comments") or []}
            issue["_comments"] = [dict(c, renderedBody=bodies.get(str(c.get("id")))) for c in comments]

        attachments = f.get("attachment") or []
        folder = os.path.join(OUT, "attachments", key)
        for a in attachments:
            name = f"{a['id']}-{safe_name(a['filename'])}"
            path = os.path.join(folder, name)
            if not os.path.exists(path) and with_attachments and a.get("size", 0) <= self.max_bytes:
                os.makedirs(folder, exist_ok=True)
                try:
                    self.jira.download(a["content"], path)
                except JiraError as e:
                    print(f"  {key}: {e}", file=sys.stderr)
            if os.path.exists(path):
                a["local"] = f"../attachments/{key}/{name}"
        write(os.path.join(OUT, "issues", f"{key}.md"), self.render_issue(issue, attachments))

    # ── релизы ──

    def render_release(self, version, issues):
        status = ("выпущен" + (f" {version['releaseDate']}" if version.get("releaseDate") else "")
                  if version.get("released") else "не выпущен")
        if version.get("archived"):
            status += ", в архиве"
        counts = {}
        for i in issues:
            s = name_of(i["fields"].get("status"))
            counts[s] = counts.get(s, 0) + 1
        lines = [f"# Релиз {version['name']}", "", "[Все релизы](../README.md)", "",
                 f"- **Статус:** {status}"]
        if version.get("startDate"):
            lines.append(f"- **Начало:** {version['startDate']}")
        if version.get("releaseDate") and not version.get("released"):
            lines.append(f"- **Дата релиза:** {version['releaseDate']}")
        lines.append(f"- **Задач:** {len(issues)}" + (" (" + ", ".join(
            f"{s} — {n}" for s, n in sorted(counts.items(), key=lambda x: -x[1])) + ")" if counts else ""))
        lines.append(f"- **Jira:** {self.jira.base}/projects/{self.project}/versions/{version['id']}")
        if version.get("description"):
            lines += ["", version["description"]]
        lines += ["", "| Задача | Тип | Название | Статус | Приоритет | Исполнитель |", "|---|---|---|---|---|---|"]
        by_key = {i["key"]: i for i in issues}
        children = {}
        for i in issues:
            p = (i["fields"].get("parent") or {}).get("key")
            if p in by_key:
                children.setdefault(p, []).append(i)

        def row(i, sub):
            f = i["fields"]
            return (f"| {'↳ ' if sub else ''}[{i['key']}]({self.issue_link(i['key'], 'releases')}) | "
                    f"{cell(name_of(f.get('issuetype')))} | {cell(f.get('summary'))} | "
                    f"{cell(name_of(f.get('status')))} | {cell(name_of(f.get('priority')))} | "
                    f"{cell(name_of(f.get('assignee'), 'displayName'))} |")
        for i in issues:
            if (i["fields"].get("parent") or {}).get("key") in by_key:
                continue
            lines.append(row(i, False))
            lines += [row(c, True) for c in children.get(i["key"], [])]
        return "\n".join(lines)

    def render_index(self, counts, extra):
        def order(v):
            return v.get("releaseDate") or v.get("startDate") or "9999"
        unreleased = sorted((v for v in self.versions.values() if not v.get("released")), key=order)
        released = sorted((v for v in self.versions.values() if v.get("released")), key=order, reverse=True)
        lines = [f"# Jira: релизы проекта {self.project}", "",
                 f"Локальная копия {self.jira.base}, выгружена {datetime.datetime.now():%Y-%m-%d %H:%M}. "
                 "Обновить (нужен доступ к Jira): `jira-sync pull`."]
        for title, items in (("Невыпущенные", unreleased), ("Выпущенные", released)):
            if not items:
                continue
            lines += ["", f"## {title}", "", "| Релиз | Начало | Дата релиза | Задач |", "|---|---|---|---|"]
            lines += [f"| [{cell(v['name'])}]({dest('releases/' + self.slugs[v['id']] + '.md')}) | "
                      f"{v.get('startDate', '')} | {v.get('releaseDate', '')} | {counts.get(v['id'], 0)} |"
                      for v in items]
        if extra:
            lines += ["", "## Задачи вне выгруженных релизов", ""]
            lines += [f"- [{k}](issues/{k}.md) {s}" for k, s in sorted(extra.items(), key=lambda x: issue_order(x[0]))]
        return "\n".join(lines)


def release_slugs(versions):
    slugs, taken = {}, set()
    for v in sorted(versions, key=lambda v: int(v["id"])):
        slug = safe_name(v["name"]).replace(" ", "-")
        if slug.lower() in taken:
            slug = f"{slug}-{v['id']}"
        taken.add(slug.lower())
        slugs[v["id"]] = slug
    return slugs


def issue_order(key):
    project, _, num = key.rpartition("-")
    return project, int(num) if num.isdigit() else 0


def select_versions(jira, project, released, match, names):
    versions = jira.request("GET", f"/rest/api/2/project/{urllib.parse.quote(project)}/versions")
    if names:  # релизы указаны явно — только они
        by_name = {v["name"]: v for v in versions}
        missing = [n for n in names if n not in by_name]
        if missing:
            raise JiraError("в проекте нет релизов: " + ", ".join(missing))
        return {by_name[n]["id"]: by_name[n] for n in names}
    if match:
        versions = [v for v in versions if re.search(match, v["name"])]
    chosen = [v for v in versions if not v.get("released") and not v.get("archived")]
    done = sorted((v for v in versions if v.get("released")),
                  key=lambda v: v.get("releaseDate") or "", reverse=True)
    chosen += done[:released]
    return {v["id"]: v for v in chosen}


# ───────────────────────── команды ─────────────────────────

def settings(args):
    project = config.get("JIRA_PROJECT")
    if not project:
        raise JiraError("не задан проект Jira: впишите в .env JIRA_PROJECT=<ключ проекта> "
                        "(отдельные задачи можно выгрузить и без него: jira-sync pull PROJ-123)")
    released = args.released if args.released is not None else int(os.environ.get("JIRA_RELEASED", "5"))
    match = args.match or os.environ.get("JIRA_VERSIONS") or None
    return project, released, match


def cmd_check(jira, args):
    me = jira.request("GET", "/rest/api/2/myself")
    project, released, match = settings(args)
    versions = select_versions(jira, project, released, match, args.version)
    print(f"Доступ к {jira.base} есть: {me.get('displayName') or me.get('name')}.")
    print(f"Проект {project}, будут выгружены релизы ({len(versions)}):")
    for v in sorted(versions.values(), key=lambda v: (bool(v.get("released")), v.get("releaseDate") or "")):
        state = f"выпущен {v.get('releaseDate', '')}" if v.get("released") else "не выпущен"
        print(f"  {v['name']} — {state}")
    return 0


def cmd_pull(jira, args):
    state = load_state()
    seen = state.get("issues", {})          # ключ -> {updated, summary}
    keys_only = bool(args.keys)
    saved = state.get("versions", {})       # релизы, уже лежащие в копии

    if keys_only:
        project, versions = config.get("JIRA_PROJECT") or args.keys[0].upper().rpartition("-")[0], {}
    else:
        project, released, match = settings(args)
        versions = select_versions(jira, project, released, match, args.version)
        print(f"Релизов: {len(versions)}")
    # с ключами задач или с --version копия дополняется: уже выгруженные релизы остаются
    partial = keys_only or bool(args.version)
    mirror = Mirror(jira, project, dict(saved, **versions) if partial else versions)

    listing, by_release = {}, {}
    if keys_only:
        wanted = {k.upper() for k in args.keys}
        for i in jira.search(f"key in ({','.join(sorted(wanted))})", LIST_FIELDS):
            listing[i["key"]] = i
        missing = wanted - set(listing)
        if missing:
            print("Не найдены или нет доступа: " + ", ".join(sorted(missing)), file=sys.stderr)
    else:
        for vid, v in versions.items():
            issues = jira.search(f'fixVersion = {vid} ORDER BY key', LIST_FIELDS)
            by_release[vid] = issues
            for i in issues:
                listing[i["key"]] = i
            print(f"  {v['name']}: задач {len(issues)}")

    # родительские задачи и подзадачи — чтобы навигация не обрывалась
    related = set()
    for i in listing.values():
        p = (i["fields"].get("parent") or {}).get("key")
        if p:
            related.add(p)
        related.update(s["key"] for s in i["fields"].get("subtasks") or [])
    related -= set(listing)
    if related:
        for i in jira.search(f"key in ({','.join(sorted(related))})", LIST_FIELDS):
            listing[i["key"]] = i

    mirror.known |= set(listing)
    changed = [k for k, i in listing.items()
               if args.force or seen.get(k, {}).get("updated") != i["fields"].get("updated")
               or not os.path.exists(os.path.join(OUT, "issues", f"{k}.md"))]
    changed.sort(key=issue_order)
    print(f"Задач: {len(listing)}, изменились или новые: {len(changed)}")
    for start in range(0, len(changed), BATCH):
        part = changed[start:start + BATCH]
        for issue in jira.search(f"key in ({','.join(part)})", FULL_FIELDS, expand="renderedFields"):
            mirror.save_issue(issue, not args.no_attachments)
            seen[issue["key"]] = {"updated": issue["fields"].get("updated"),
                                  "summary": issue["fields"].get("summary", "")}
        state["issues"] = seen
        save_state(state)  # прерванный pull продолжится с этого места
        print(f"  выгружено {min(start + BATCH, len(changed))} из {len(changed)}")

    if not keys_only:
        releases_dir = os.path.join(OUT, "releases")
        os.makedirs(releases_dir, exist_ok=True)
        for name in os.listdir(releases_dir):  # релизы, которые больше не выгружаются
            if name.endswith(".md") and name[:-3] not in mirror.slugs.values():
                os.remove(os.path.join(releases_dir, name))
        for vid, issues in by_release.items():
            write(os.path.join(releases_dir, f"{mirror.slugs[vid]}.md"), mirror.render_release(versions[vid], issues))
        counts = {vid: len(i) for vid, i in by_release.items()}
        in_releases = {i["key"] for issues in by_release.values() for i in issues}
        if partial:
            counts = dict(state.get("counts", {}), **counts)
            in_releases |= set(state.get("in_releases", []))
        state["versions"] = mirror.versions
        state["counts"] = counts
        state["in_releases"] = sorted(in_releases)

    in_releases = set(state.get("in_releases", []))
    extra = {k: v.get("summary", "") for k, v in seen.items() if k not in in_releases and k in mirror.known}
    write(os.path.join(OUT, "README.md"), mirror.render_index(state.get("counts", {}), extra))
    state["issues"] = seen
    save_state(state)
    print(f"Готово: {config.rel(os.path.join(OUT, 'README.md'))}")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="jira-sync", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--insecure", action="store_true", help="не проверять TLS-сертификат Jira")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in (("check", "проверить доступ и показать, какие релизы выгрузятся"),
                       ("pull", "выгрузить релизы и задачи в _jira/")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--released", type=int, help="сколько последних выпущенных релизов выгружать")
        p.add_argument("--match", help="регулярное выражение: только релизы с подходящим названием")
        p.add_argument("--version", action="append", default=[], help="выгрузить только этот релиз (по точному названию; можно несколько раз)")
        if name == "pull":
            p.add_argument("keys", nargs="*", help="ключи задач (PROJ-123): выгрузить только их")
            p.add_argument("--force", action="store_true", help="перевыгрузить все задачи, даже неизменённые")
            p.add_argument("--no-attachments", action="store_true", help="не скачивать вложения")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    try:
        jira = Jira(args.insecure)
        code = {"check": cmd_check, "pull": cmd_pull}[args.cmd](jira, args)
    except JiraError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        print("\nПрервано. Повторите pull — выгруженные задачи заново не скачиваются.", file=sys.stderr)
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
