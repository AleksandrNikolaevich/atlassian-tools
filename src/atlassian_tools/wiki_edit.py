"""Правка страниц вики в их родном формате (формат хранения Confluence).

Страница не пересобирается из markdown, а редактируется: оформление, макросы и диаграммы остаются как
были. Сетевые команды (fetch, push, new) запускает человек из сети с доступом к вики; правку файла и
просмотр изменений можно делать без сети.

    wiki-edit fetch <документ.md | wiki_page_id>   # взять страницу в _wiki-edit/<id>.xml
    # … правка файла _wiki-edit/<id>.xml (вручную или ИИ-агентом) …
    wiki-edit diff <id>      # что изменилось, в читаемом виде
    wiki-edit check <id>     # XML корректен? макросы на месте?
    wiki-edit push <id>      # записать в вики (спросит подтверждение)
    wiki-edit new --parent <id> --template <id> --title "Название"
    wiki-edit status         # какие страницы сейчас в работе

push не запишет страницу, если её успели изменить в вики после fetch. После push выполните
`wiki-sync pull` — правка придёт в локальную копию. Рабочая папка — WIKI_EDIT_DIR (по умолчанию _wiki-edit).
"""
import argparse
import datetime
import difflib
import html.entities
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter

from . import config
from .confluence import Wiki, WikiError, page_url

WORK = config.folder("WIKI_EDIT_DIR", "_wiki-edit")
KEEP_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}


def files(key):
    base = os.path.join(WORK, key)
    return base + ".xml", base + ".orig.xml", base + ".json"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def load(key):
    xml, orig, meta = files(key)
    if not os.path.isfile(xml) or not os.path.isfile(meta):
        raise WikiError(f"страница {key} не взята в работу — выполните fetch")
    return read(xml), read(orig), json.loads(read(meta))


def resolve(arg):
    """Аргумент команды (wiki_page_id, ссылка на страницу или путь к документу копии) -> (id, путь или None)."""
    if arg.isdigit() or arg.startswith("new-"):
        return arg, None
    m = re.search(r"pageId=(\d+)|/pages/(\d+)", arg)
    if m:
        return m.group(1) or m.group(2), None
    path = os.path.abspath(arg)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            m = re.search(r'^wiki_page_id:\s*"?(\d+)', f.read(3000), re.M)
        if m:
            return m.group(1), config.rel(path)
    raise WikiError(f"не найдена страница: {arg} (укажите wiki_page_id, ссылку или документ с wiki_page_id)")


# ───────────────────────── проверка правки ─────────────────────────

def validate(new, orig):
    """Возвращает (ошибки, предупреждения). Ошибки запрещают запись."""
    errors, warnings = [], []
    text = re.sub(r"&([A-Za-z][A-Za-z0-9]*);",
                  lambda m: m.group(0) if m.group(1) in KEEP_ENTITIES
                  else (f"&#{html.entities.name2codepoint[m.group(1)]};" if m.group(1) in html.entities.name2codepoint
                        else m.group(0)), new)
    try:
        ET.fromstring(f'<root xmlns:ac="urn:ac" xmlns:ri="urn:ri" xmlns:at="urn:at">{text}</root>')
    except ET.ParseError as e:
        line, col = e.position
        lines = text.split("\n")
        around = lines[line - 1][max(0, col - 60):col + 60] if 0 < line <= len(lines) else ""
        errors.append(f"XML повреждён ({e.msg}), строка {line}, позиция {col}: …{around}…")

    def macros(src):
        return Counter(re.findall(r'<ac:structured-macro\b[^>]*\bac:name="([^"]+)"', src))

    before, after = macros(orig), macros(new)
    for name in sorted(set(before) | set(after)):
        if after[name] < before[name]:
            warnings.append(f"макросов «{name}» стало меньше: было {before[name]}, стало {after[name]}")
        elif after[name] > before[name]:
            warnings.append(f"макросов «{name}» стало больше: было {before[name]}, стало {after[name]}")
    ids = Counter(re.findall(r'ac:macro-id="([^"]+)"', new))
    dup = [i for i, n in ids.items() if n > 1]
    if dup:
        errors.append(f"повторяется ac:macro-id ({len(dup)} шт.): у скопированного макроса атрибут ac:macro-id "
                      f"нужно удалить — вики выдаст новый. Первый: {dup[0]}")
    old_files = set(re.findall(r'ri:filename="([^"]+)"', orig))
    for name in sorted(set(re.findall(r'ri:filename="([^"]+)"', new)) - old_files):
        warnings.append(f"новое вложение «{name}»: файл нужно загрузить на страницу в вики вручную до записи")
    if orig and len(new) < len(orig) * 0.7:
        warnings.append(f"страница стала заметно короче: {len(orig)} → {len(new)} символов")
    return errors, warnings


def readable_diff(new, orig, title):
    from . import storage_view  # нужен beautifulsoup4: не грузим его для команд, которым он не нужен
    old_md, new_md = storage_view.to_md(orig, title), storage_view.to_md(new, title)
    return "".join(difflib.unified_diff(old_md.splitlines(True), new_md.splitlines(True),
                                        "вики сейчас", "после записи", n=2))


# ───────────────────────── команды ─────────────────────────

def cmd_fetch(args):
    pid, path = resolve(args.page)
    xml, orig, meta = files(pid)
    if os.path.isfile(xml) and read(xml) != read(orig) and not args.force:
        raise WikiError(f"в {config.rel(xml)} есть незаписанная правка — выполните push или fetch --force")
    wiki = Wiki(args.insecure)
    data = wiki.get(f"/rest/api/content/{pid}", expand="body.storage,version,space")
    body = data["body"]["storage"]["value"]
    write(xml, body)
    write(orig, body)
    write(meta, json.dumps({"id": pid, "title": data["title"], "version": data["version"]["number"],
                            "space": (data.get("space") or {}).get("key"), "path": path,
                            "fetched": f"{datetime.datetime.now():%Y-%m-%d %H:%M}"}, ensure_ascii=False, indent=1))
    print(f"«{data['title']}», версия {data['version']['number']} → {config.rel(xml)}")
    print("Правьте этот файл. Файл .orig.xml не трогайте: с ним сравнивается правка.")
    return 0


def cmd_new(args):
    wiki = Wiki(args.insecure)
    parent = wiki.get(f"/rest/api/content/{args.parent}", expand="space,version")
    template = wiki.get(f"/rest/api/content/{args.template}", expand="body.storage") if args.template else None
    key = "new-" + re.sub(r"[^\w]+", "-", args.title.lower(), flags=re.U).strip("-")[:50]
    xml, orig, meta = files(key)
    if os.path.isfile(xml) and not args.force:
        raise WikiError(f"{config.rel(xml)} уже есть — допишите его или запустите с --force")
    write(xml, template["body"]["storage"]["value"] if template else "<p></p>")
    write(orig, "")
    write(meta, json.dumps({"id": key, "new": True, "title": args.title, "parent": str(args.parent),
                            "space": parent["space"]["key"], "template": args.template,
                            "fetched": f"{datetime.datetime.now():%Y-%m-%d %H:%M}"}, ensure_ascii=False, indent=1))
    source = f" из шаблона «{template['title']}»" if template else ""
    print(f"Заготовка «{args.title}» под «{parent['title']}»{source} → {config.rel(xml)}")
    print(f"Заполните файл и выполните: wiki-edit push {key}")
    return 0


def report(key, show_diff):
    new, orig, meta = load(key)
    errors, warnings = validate(new, orig)
    print(f"«{meta['title']}»" + (" — новая страница" if meta.get("new") else f", версия {meta['version']}"))
    if new == orig:
        print("Правок нет.")
        return new, orig, meta, errors, warnings
    if show_diff:
        diff = readable_diff(new, orig, meta["title"])
        print(diff if diff else "Текст в читаемом виде не изменился (менялась только разметка).")
    for w in warnings:
        print(f"ВНИМАНИЕ: {w}")
    for e in errors:
        print(f"ОШИБКА: {e}")
    return new, orig, meta, errors, warnings


def cmd_diff(args):
    key, _ = resolve(args.page)
    *_, errors, _ = report(key, True)
    return 1 if errors else 0


def cmd_check(args):
    key, _ = resolve(args.page)
    new, orig, _, errors, warnings = report(key, False)
    if new != orig and not errors:
        print("Ошибок нет." + (" Проверьте предупреждения выше." if warnings else ""))
    return 1 if errors else 0


def cmd_push(args):
    key, _ = resolve(args.page)
    new, orig, meta, errors, warnings = report(key, not args.yes)
    if new == orig:
        return 0
    if errors:
        raise WikiError("запись отменена: исправьте ошибки выше")
    wiki = Wiki(args.insecure)
    if not meta.get("new"):
        current = wiki.get(f"/rest/api/content/{key}", expand="version")
        if current["version"]["number"] != meta["version"]:
            who = (current["version"].get("by") or {}).get("displayName", "")
            raise WikiError(f"страницу изменили в вики после fetch: версия {meta['version']} → "
                            f"{current['version']['number']} ({who}). Выполните fetch --force и повторите правку")
    if not args.yes:
        target = f"Создать страницу «{meta['title']}»" if meta.get("new") else f"Записать «{meta['title']}»"
        answer = input(f"\n{target} в вики {wiki.base}? Введите «да»: ").strip().lower()
        if answer not in ("да", "yes", "y", "д"):
            print("Запись отменена, файл с правкой сохранён.")
            return 1
    body = {"storage": {"value": new, "representation": "storage"}}
    if meta.get("new"):
        result = wiki.send("POST", "/rest/api/content", {
            "type": "page", "title": meta["title"], "space": {"key": meta["space"]},
            "ancestors": [{"id": meta["parent"]}], "body": body})
        print(f"Страница создана: {page_url(wiki.base, result['id'])}")
    else:
        message = args.message or "Правка из репозитория"
        result = wiki.send("PUT", f"/rest/api/content/{key}", {
            "id": key, "type": "page", "title": meta["title"],
            "version": {"number": meta["version"] + 1, "message": message}, "body": body})
        print(f"Записано: версия {result['version']['number']} — {page_url(wiki.base, key)}")
    for path in files(key):
        os.remove(path)
    print("Теперь выполните: wiki-sync pull")
    return 0


def cmd_status(args):
    names = sorted(n[:-5] for n in (os.listdir(WORK) if os.path.isdir(WORK) else []) if n.endswith(".json"))
    if not names:
        print("Страниц в работе нет.")
    for key in names:
        new, orig, meta = load(key)
        state = "новая страница" if meta.get("new") else ("есть правка" if new != orig else "без правок")
        print(f"{key}: «{meta['title']}» — {state}, взята {meta['fetched']}")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="wiki-edit", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--insecure", action="store_true", help="не проверять TLS-сертификат вики")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch", help="взять страницу вики в работу")
    p.add_argument("page", help="wiki_page_id, ссылка на страницу или документ копии .md")
    p.add_argument("--force", action="store_true", help="перезаписать рабочую копию, даже если в ней есть правка")
    p = sub.add_parser("new", help="заготовка новой страницы")
    p.add_argument("--parent", required=True, help="wiki_page_id родительской страницы")
    p.add_argument("--template", help="wiki_page_id страницы-образца: её содержимое станет заготовкой")
    p.add_argument("--title", required=True, help="название новой страницы")
    p.add_argument("--force", action="store_true")
    for name, text in (("diff", "показать правку в читаемом виде"), ("check", "проверить правку")):
        p = sub.add_parser(name, help=text)
        p.add_argument("page", help="wiki_page_id или документ копии .md")
    p = sub.add_parser("push", help="записать правку в вики")
    p.add_argument("page", help="wiki_page_id или документ копии .md")
    p.add_argument("-m", "--message", help="комментарий к версии страницы в вики")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    sub.add_parser("status", help="страницы в работе")
    args = ap.parse_args()
    try:
        code = {"fetch": cmd_fetch, "new": cmd_new, "diff": cmd_diff, "check": cmd_check, "push": cmd_push,
                "status": cmd_status}[args.cmd](args)
    except WikiError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
