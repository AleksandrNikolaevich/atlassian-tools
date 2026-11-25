"""Правка описания (поле description) задачи Jira в её родной разметке (Jira wiki markup).

Описание не пересобирается из markdown, а редактируется как есть: оформление, таблицы и ссылки на
вложения остаются как были. Сетевые команды (fetch, push) запускает человек из сети с доступом к Jira;
правку файла и просмотр изменений можно делать без сети.

    jira-edit fetch <KEY>    # взять описание задачи в _jira-edit/<KEY>.txt
    # … правка файла _jira-edit/<KEY>.txt (вручную или ИИ-агентом) …
    jira-edit diff <KEY>     # что изменилось
    jira-edit check <KEY>    # разметка цела? вложения на месте?
    jira-edit push <KEY>     # записать в Jira (спросит подтверждение)
    jira-edit status         # какие задачи сейчас в работе

push не запишет описание, если его успели изменить в Jira после fetch. После push выполните
`jira-sync pull <KEY>` — правка придёт в локальную копию. Доступ к Jira — как у jira-sync (файл .env).
Рабочая папка — JIRA_EDIT_DIR (по умолчанию _jira-edit).
"""
import argparse
import datetime
import difflib
import json
import os
import re
import sys

from . import config
from .jira_sync import Jira, JiraError

WORK = config.folder("JIRA_EDIT_DIR", "_jira-edit")
MAX_LENGTH = 32767  # предел длины текстового поля Jira
BLOCK_MACROS = ("code", "noformat", "panel", "quote", "color")


def files(key):
    base = os.path.join(WORK, key)
    return base + ".txt", base + ".orig.txt", base + ".json"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def normalize(text):
    return (text or "").replace("\r\n", "\n")


def issue_key(arg):
    """Аргумент (ключ, ссылка на задачу или путь к _jira/issues/<KEY>/<KEY>.md) -> ключ задачи."""
    m = re.search(r"([A-Z][A-Z0-9_]+-\d+)", os.path.basename(arg.rstrip("/")).upper())
    if not m:
        raise JiraError(f"не похоже на ключ задачи Jira: {arg} (ожидается, например, PROJ-1234)")
    return m.group(1)


def load(key):
    text, orig, meta = files(key)
    if not os.path.isfile(text) or not os.path.isfile(meta):
        raise JiraError(f"задача {key} не взята в работу — выполните fetch")
    return read(text), read(orig), json.loads(read(meta))


# ───────────────────────── проверка правки ─────────────────────────

def validate(new, orig):
    """Возвращает (ошибки, предупреждения). Ошибки запрещают запись."""
    errors, warnings = [], []
    if len(new) > MAX_LENGTH:
        errors.append(f"описание длиннее {MAX_LENGTH} символов ({len(new)}): Jira его не примет")
    for name in BLOCK_MACROS:
        n = len(re.findall(r"\{" + name + r"(?::[^}]*)?\}", new))
        if n % 2:
            errors.append(f"макрос {{{name}}} не закрыт: открывающих и закрывающих тегов {n}, должно быть чётное")

    def attachments(src):
        return set(re.findall(r"!([^!\s|][^!|\n]*?)(?:\|[^!\n]*)?!", src)) | set(re.findall(r"\[\^([^\]\n]+)\]", src))

    for name in sorted(attachments(new) - attachments(orig)):
        if not name.startswith(("http://", "https://")):
            warnings.append(f"новая ссылка на вложение «{name}»: файл нужно приложить к задаче в Jira вручную")
    if orig and len(new) < len(orig) * 0.7:
        warnings.append(f"описание стало заметно короче: {len(orig)} → {len(new)} символов")
    if orig and not new.strip():
        warnings.append("описание станет пустым")
    return errors, warnings


def text_diff(new, orig):
    return "".join(difflib.unified_diff(orig.splitlines(True), new.splitlines(True),
                                        "Jira сейчас", "после записи", n=2))


# ───────────────────────── команды ─────────────────────────

def cmd_fetch(args):
    key = issue_key(args.issue)
    text, orig, meta = files(key)
    if os.path.isfile(text) and read(text) != read(orig) and not args.force:
        raise JiraError(f"в {config.rel(text)} есть незаписанная правка — выполните push или fetch --force")
    jira = Jira(args.insecure)
    data = jira.get(f"/rest/api/2/issue/{key}", fields="summary,description,updated")
    description = normalize(data["fields"].get("description"))
    write(text, description)
    write(orig, description)
    write(meta, json.dumps({"key": data["key"], "summary": data["fields"].get("summary", ""),
                            "updated": data["fields"].get("updated"),
                            "fetched": f"{datetime.datetime.now():%Y-%m-%d %H:%M}"}, ensure_ascii=False, indent=1))
    print(f"{data['key']} «{data['fields'].get('summary', '')}» → {config.rel(text)}")
    print("Правьте этот файл: разметка Jira (wiki markup). Файл .orig.txt не трогайте: с ним сравнивается правка.")
    return 0


def report(key, show_diff):
    new, orig, meta = load(key)
    errors, warnings = validate(new, orig)
    print(f"{key} «{meta['summary']}», описание взято {meta['fetched']}")
    if new == orig:
        print("Правок нет.")
        return new, orig, meta, errors, warnings
    if show_diff:
        print(text_diff(new, orig))
    for w in warnings:
        print(f"ВНИМАНИЕ: {w}")
    for e in errors:
        print(f"ОШИБКА: {e}")
    return new, orig, meta, errors, warnings


def cmd_diff(args):
    *_, errors, _ = report(issue_key(args.issue), True)
    return 1 if errors else 0


def cmd_check(args):
    new, orig, _, errors, warnings = report(issue_key(args.issue), False)
    if new != orig and not errors:
        print("Ошибок нет." + (" Проверьте предупреждения выше." if warnings else ""))
    return 1 if errors else 0


def cmd_push(args):
    key = issue_key(args.issue)
    new, orig, meta, errors, warnings = report(key, not args.yes)
    if new == orig:
        return 0
    if errors:
        raise JiraError("запись отменена: исправьте ошибки выше")
    jira = Jira(args.insecure)
    current = jira.get(f"/rest/api/2/issue/{key}", fields="description,updated")
    if normalize(current["fields"].get("description")) != orig:
        raise JiraError(f"описание задачи изменили в Jira после fetch (обновлена {current['fields'].get('updated')}). "
                        "Выполните fetch --force и повторите правку")
    if not args.yes:
        answer = input(f"\nЗаписать описание {key} «{meta['summary']}» в Jira {jira.base}? Введите «да»: ")
        if answer.strip().lower() not in ("да", "yes", "y", "д"):
            print("Запись отменена, файл с правкой сохранён.")
            return 1
    jira.request("PUT", f"/rest/api/2/issue/{key}?notifyUsers=false" if args.quiet else f"/rest/api/2/issue/{key}",
                 {"fields": {"description": new}})
    print(f"Записано: {jira.base}/browse/{key}")
    for path in files(key):
        os.remove(path)
    print(f"Теперь выполните: jira-sync pull {key}")
    return 0


def cmd_status(args):
    names = sorted(n[:-5] for n in (os.listdir(WORK) if os.path.isdir(WORK) else []) if n.endswith(".json"))
    if not names:
        print("Задач в работе нет.")
    for key in names:
        new, orig, meta = load(key)
        state = "есть правка" if new != orig else "без правок"
        print(f"{key}: «{meta['summary']}» — {state}, взята {meta['fetched']}")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="jira-edit", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--insecure", action="store_true", help="не проверять TLS-сертификат Jira")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch", help="взять описание задачи в работу")
    p.add_argument("issue", help="ключ задачи, например PROJ-1234")
    p.add_argument("--force", action="store_true", help="перезаписать рабочую копию, даже если в ней есть правка")
    for name, text in (("diff", "показать правку"), ("check", "проверить правку")):
        p = sub.add_parser(name, help=text)
        p.add_argument("issue", help="ключ задачи")
    p = sub.add_parser("push", help="записать описание в Jira")
    p.add_argument("issue", help="ключ задачи")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    p.add_argument("--quiet", action="store_true", help="не рассылать уведомление об изменении (нужны права "
                                                         "администратора проекта)")
    sub.add_parser("status", help="задачи в работе")
    args = ap.parse_args()
    try:
        code = {"fetch": cmd_fetch, "diff": cmd_diff, "check": cmd_check, "push": cmd_push,
                "status": cmd_status}[args.cmd](args)
    except JiraError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
