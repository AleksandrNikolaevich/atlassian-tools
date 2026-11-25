"""Локальная копия вики Confluence в markdown: выгрузка пространства и перенос правок.

В вики не пишет. Запускать из сети, где вики доступна (VPN).

    wiki-sync check               # что изменилось в вики по сравнению с копией
    wiki-sync pull --dry-run      # показать, что перенесётся, файлы не трогать
    wiki-sync pull                # первый раз — выгрузить всё; дальше — перенести правки и новые страницы
    wiki-sync pull 123 456        # только правки указанных страниц
    wiki-sync accept 123 --all    # отметить правки перенесёнными (без переноса текста)
    wiki-sync clean               # предложить удаление лишних документов и картинок

Область синхронизации (настройки в .env проекта):
  WIKI_SPACE=KEY        всё пространство KEY;
  WIKI_ROOTS=123,456    или только эти страницы с дочерними на всю глубину;
  WIKI_EXCLUDE=789      страницы, которые не выгружать, вместе с дочерними;
  WIKI_DIR=wiki         папка копии от корня проекта.

Каждая страница — документ с front matter (title, wiki_page_id, wiki_version, wiki_parent_id, wiki_url).
Страница с дочерними — папка с README.md, без дочерних — файл <slug>.md, картинки — в _assets/ рядом.
Правки, сделанные в документах, при pull сохраняются: изменения из вики накладываются слиянием.

check: код возврата 0 — расхождений нет, 1 — есть, 2 — ошибка доступа к вики.
"""
import argparse
import datetime
import os
import re
import shutil
import sys
import urllib.parse

from . import config, wiki_pull
from .confluence import Wiki, WikiError, diff_url, page_url

OUT = config.folder("WIKI_DIR", "wiki")
PREFIX = config.rel(OUT) + "/"
MD_LINK_RE = re.compile(r'(!?)\[((?:[^\]\\]|\\.)*)\]\(([^)\s]+)\)|<img\s[^>]*src="([^"]+)"[^>]*>')


# ───────────────────────── документы копии ─────────────────────────

def read_docs():
    """wiki_page_id -> {path, title, version, parent} для всех документов копии."""
    docs = {}
    for path in wiki_pull.md_files(OUT) if os.path.isdir(OUT) else []:
        with open(os.path.join(OUT, path), encoding="utf-8") as f:
            text = f.read(3000)
        if not text.startswith("---\n"):
            continue
        fields = {}
        for line in text[4:text.find("\n---", 4)].split("\n"):
            m = re.match(r'([\w-]+):\s*"?(.*?)"?\s*$', line)
            if m:
                fields[m.group(1)] = m.group(2).replace('\\"', '"')
        if fields.get("wiki_page_id"):
            docs[fields["wiki_page_id"]] = {"path": path, "title": fields.get("title", ""),
                                            "version": fields.get("wiki_version"),
                                            "parent": fields.get("wiki_parent_id")}
    return docs


def set_version(rel_path, version):
    path = os.path.join(OUT, rel_path)
    with open(path, encoding="utf-8") as f:
        head, body = f.read().split("\n---", 1)
    head = wiki_pull.set_field(head, "wiki_version", version)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{head}\n---{body}")


def scope_pages(wiki):
    """Страницы области синхронизации: id -> сведения. Исключённые поддеревья отброшены."""
    roots, space, exclude = config.ids("WIKI_ROOTS"), config.get("WIKI_SPACE"), set(config.ids("WIKI_EXCLUDE"))
    if roots:
        pages = wiki.pages(roots)
        missing = [r for r in roots if r not in pages]
        if missing:
            raise WikiError(f"страницы из WIKI_ROOTS не найдены или к ним нет доступа: {', '.join(missing)}")
        found, rejected = wiki.descendants(roots, exclude)
        pages.update(found)
        for pid in rejected:
            print(f"Внимание: вики не отдала дочерние страницы {page_url(wiki.base, pid)}", file=sys.stderr)
    elif space:
        pages = wiki.space_pages(space)
        if not pages:
            raise WikiError(f"в пространстве {space} нет страниц или к нему нет доступа")
    else:
        raise WikiError("не задана область синхронизации: впишите в .env WIKI_SPACE=<ключ пространства> "
                        "или WIKI_ROOTS=<id страницы>,<id страницы>")
    return {pid: p for pid, p in pages.items() if pid not in exclude and not exclude.intersection(p["ancestors"])}


def listing(items, render, limit=50):
    out = ["- " + render(item) for item in items[:limit]]
    if len(items) > limit:
        out.append(f"- … и ещё {len(items) - limit}")
    return out


# ───────────────────────── команды ─────────────────────────

def cmd_check(wiki, args):
    docs = read_docs()
    scope = scope_pages(wiki)
    outside = [pid for pid in docs if pid not in scope]
    found = wiki.pages(outside) if outside else {}
    changed, renamed, moved, missing, out_of_scope = [], [], [], [], []
    for pid, doc in sorted(docs.items(), key=lambda kv: kv[1]["path"]):
        page = scope.get(pid)
        if page is None:
            (out_of_scope if pid in found else missing).append(doc)
            continue
        if page["version"] != doc["version"]:
            changed.append((pid, doc, page))
        if page["title"].replace("\xa0", " ").strip() != doc["title"].strip():
            renamed.append((doc, page))
        if doc["parent"] and page["parent"] and page["parent"] != doc["parent"]:
            moved.append((pid, doc, page))
    new = sorted(((pid, page) for pid, page in scope.items() if pid not in docs), key=lambda kv: kv[1]["title"])

    out = [f"# Сверка с вики — {datetime.datetime.now():%Y-%m-%d %H:%M}", "",
           f"Страниц в области синхронизации: {len(scope)}, документов в копии ({PREFIX}): {len(docs)}. "
           f"Изменено: {len(changed)}, переименовано: {len(renamed)}, перенесено: {len(moved)}, "
           f"не найдено: {len(missing)}, вне области: {len(out_of_scope)}, новых страниц: {len(new)}."]
    if changed:
        out += ["", "## Изменены в вики", ""]
        for pid, doc, page in changed:
            out.append(f"- `{PREFIX}{doc['path']}` — версия {doc['version'] or '—'} → {page['version']}, "
                       f"{page['when']}, {page['by']}  ")
            out.append(f"  [что изменилось]({diff_url(wiki.base, pid, doc['version'], page['version'])}) · id {pid}"
                       if doc["version"] else f"  id {pid}")
    if renamed:
        out += ["", "## Переименованы в вики", ""]
        out += listing(renamed, lambda it: f"`{PREFIX}{it[0]['path']}` — было «{it[0]['title']}», стало «{it[1]['title']}»")
    if moved:
        out += ["", "## Перенесены в вики под другого родителя", ""]
        out += listing(moved, lambda it: f"`{PREFIX}{it[1]['path']}` — родитель {it[1]['parent']} → {it[2]['parent']} "
                                         f"({page_url(wiki.base, it[2]['parent'])})")
    if missing:
        out += ["", "## Не найдены в вики (удалены или нет доступа)", ""]
        out += listing(missing, lambda doc: f"`{PREFIX}{doc['path']}` — «{doc['title']}»")
    if out_of_scope:
        out += ["", "## Вне области синхронизации (перенесены или исключены)", ""]
        out += listing(out_of_scope, lambda doc: f"`{PREFIX}{doc['path']}` — «{doc['title']}»")
    if new:
        out += ["", "## Новые страницы в вики, которых нет в копии", ""]
        out += listing(new, lambda it: f"[{it[1]['title']}]({page_url(wiki.base, it[0])}) — {it[1]['when']}, {it[1]['by']}")
    report = "\n".join(out) + "\n"
    print(report)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report)
    return 1 if changed or renamed or moved or missing or out_of_scope or new else 0


def cmd_accept(wiki, args):
    docs = read_docs()
    ids = sorted(docs) if args.all else args.ids
    unknown = [pid for pid in ids if pid not in docs]
    if unknown:
        raise WikiError(f"нет документов с wiki_page_id: {', '.join(unknown)}")
    if not ids:
        raise WikiError("укажите идентификаторы страниц или --all")
    pages = wiki.pages(ids)
    for pid in ids:
        if pid in pages and pages[pid]["version"] != docs[pid]["version"]:
            set_version(docs[pid]["path"], pages[pid]["version"])
            print(f"{PREFIX}{docs[pid]['path']}: версия {docs[pid]['version'] or '—'} → {pages[pid]['version']}")
    return 0


def cmd_pull(wiki, args):
    args.prefix = PREFIX
    docs = read_docs()
    if args.ids:
        unknown = [pid for pid in args.ids if pid not in docs]
        if unknown:
            raise WikiError(f"нет документов с wiki_page_id: {', '.join(unknown)}")
        pages = wiki.pages(args.ids)
        ids = [pid for pid in args.ids if pid in pages and (pages[pid]["version"] != docs[pid]["version"] or args.theirs)]
        new_pages = {}
    else:
        if args.theirs:
            raise WikiError("--theirs применяется только к указанным страницам: wiki-sync pull <id> --theirs")
        pages = scope_pages(wiki)
        ids = [pid for pid in sorted(docs, key=lambda p: docs[p]["path"])
               if pid in pages and pages[pid]["version"] != docs[pid]["version"]]
        new_pages = {} if args.no_new else {pid: page for pid, page in pages.items() if pid not in docs}
    full = not args.ids and not args.no_clean  # уборка — только при полной синхронизации
    if not docs and new_pages:
        print(f"Первая выгрузка: страниц {len(new_pages)} → {PREFIX}")
    if not ids and not new_pages:
        print("Изменённых и новых страниц нет.")
        if full:
            cleanup(wiki, docs, pages, args.dry_run)
        return 0
    os.makedirs(OUT, exist_ok=True)
    problems = wiki_pull.pull(wiki, OUT, docs, pages, ids, args)
    if new_pages:
        problems += wiki_pull.import_new(wiki, OUT, docs, new_pages, args)
    if args.dry_run:
        print(f"\nПробный запуск: изменённых страниц {len(ids)}, новых {len(new_pages)}; файлы не менялись.")
        if full:
            cleanup(wiki, docs, pages, dry_run=True)
        return 1 if problems else 0
    if full:
        cleanup(wiki, read_docs(), pages)
    print(f"\nИзменённых страниц: {len(ids)}, новых: {len(new_pages)}, требуют внимания: {problems}. "
          "Просмотрите изменения (git diff).")
    return 1 if problems else 0


def cmd_clean(wiki, args):
    deleted = cleanup(wiki, read_docs(), scope_pages(wiki), args.dry_run)
    if deleted:
        print(f"\nУдалено: {deleted}. Просмотрите изменения (git status).")
    return 0


# ───────────────────────── уборка ─────────────────────────

def link_target(doc, dest):
    """Путь от корня копии, на который ведёт относительная ссылка из документа doc; None для внешних ссылок."""
    if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", dest) or dest.startswith("#"):
        return None
    return os.path.normpath(os.path.join(os.path.dirname(doc), urllib.parse.unquote(dest.split("#")[0])))


def orphan_images():
    """Файлы в папках _assets/, на которые не ссылается ни один документ."""
    used = set()
    for doc in wiki_pull.md_files(OUT):
        with open(os.path.join(OUT, doc), encoding="utf-8") as f:
            for m in MD_LINK_RE.finditer(f.read()):
                target = link_target(doc, m.group(3) or m.group(4))
                if target:
                    used.add(target)
    orphans = []
    for base, dirs, files in os.walk(OUT):
        if "_assets" in os.path.relpath(base, OUT).split(os.sep):
            orphans += [os.path.relpath(os.path.join(base, n), OUT) for n in files
                        if not n.startswith(".") and os.path.relpath(os.path.join(base, n), OUT) not in used]
    return sorted(orphans)


def doc_files(path):
    """(что удалить вместе с документом, мешающие удалению дочерние документы)."""
    folder = os.path.dirname(path)
    if path.endswith("/README.md"):
        children = [os.path.relpath(os.path.join(base, n), OUT)
                    for base, dirs, files in os.walk(os.path.join(OUT, folder))
                    for n in files if n.endswith(".md") and os.path.join(base, n) != os.path.join(OUT, path)
                    and "_assets" not in os.path.relpath(base, OUT).split(os.sep)]
        return [folder], children
    assets = os.path.join(folder, "_assets", os.path.basename(path)[:-3])
    return [path] + ([assets] if os.path.isdir(os.path.join(OUT, assets)) else []), []


def remove_docs(paths):
    """Удаляет документы с картинками и снимает ссылки на них в остальных документах (текст остаётся)."""
    removed = set(paths)
    for path in paths:
        for target in doc_files(path)[0]:
            full = os.path.join(OUT, target)
            shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)
    for doc in wiki_pull.md_files(OUT):
        full = os.path.join(OUT, doc)
        with open(full, encoding="utf-8") as f:
            text = f.read()
        updated = MD_LINK_RE.sub(
            lambda m: m.group(2) if m.group(3) and not m.group(1) and link_target(doc, m.group(3)) in removed
            else m.group(0), text)
        if updated != text:
            with open(full, "w", encoding="utf-8") as f:
                f.write(updated)


def confirm(question):
    if not sys.stdin.isatty():
        print(f"{question} — пропущено: подтвердить удаление можно только в терминале.")
        return False
    try:
        return input(f"{question} Введите «да»: ").strip().lower() in ("да", "yes", "y", "д")
    except EOFError:
        print()
        return False


def cleanup(wiki, docs, scope, dry_run=False):
    """Предлагает удалить документы страниц, которых нет в области синхронизации, и картинки без ссылок.

    Возвращает число удалений.
    """
    outside = sorted((pid for pid in docs if pid not in scope), key=lambda p: docs[p]["path"])
    alive = wiki.pages(outside) if outside else {}
    groups = {"out": [], "trash": [], "gone": []}
    blocked = []
    for pid in outside:
        doc = docs[pid]
        children = doc_files(doc["path"])[1]
        if children:
            blocked.append((doc, len(children)))
        elif pid in alive:
            groups["out"].append((pid, doc, alive[pid]))
        else:
            info = wiki.trashed(pid)
            groups["trash" if info else "gone"].append((pid, doc, info))
    titles = {
        "out": "Страницы есть в вики, но вне области синхронизации (перенесены в другое место или исключены):",
        "trash": "Страницы лежат в корзине вики:",
        "gone": "Страницы не найдены в вики — удалены окончательно ИЛИ к ним закрыт доступ. Проверьте в браузере:",
    }
    deleted = 0
    if blocked:
        print("\nСтраниц нет в области синхронизации, но у документов есть дочерние — разберите вручную:\n")
        print("\n".join(listing(blocked, lambda it: f"{PREFIX}{it[0]['path']} — дочерних документов: {it[1]}", 30)))
    for key, items in groups.items():
        if not items:
            continue
        print(f"\n{titles[key]}\n")
        print("\n".join(listing(items, lambda it: f"{PREFIX}{it[1]['path']} — «{it[1]['title']}»"
                                + (f", удалил(а) {it[2]['by']} {it[2]['when']}" if key == "trash" else "")
                                + (f", {page_url(wiki.base, it[0])}" if key != "trash" else ""), 30)))
        if not dry_run and confirm(f"\nУдалить документы этих страниц ({len(items)}) вместе с картинками?"):
            remove_docs([doc["path"] for _, doc, _ in items])
            deleted += len(items)
    orphans = orphan_images() if os.path.isdir(OUT) else []
    if orphans:
        size = sum(os.path.getsize(os.path.join(OUT, o)) for o in orphans) // 1024
        print(f"\nКартинки, на которые не ссылается ни один документ ({size} КБ):\n")
        print("\n".join(listing(orphans, lambda o: f"{PREFIX}{o}", 30)))
        if not dry_run and confirm(f"\nУдалить эти картинки ({len(orphans)})?"):
            for o in orphans:
                os.remove(os.path.join(OUT, o))
                folder = os.path.dirname(os.path.join(OUT, o))
                if not os.listdir(folder):
                    os.rmdir(folder)
            deleted += len(orphans)
    if not (blocked or any(groups.values()) or orphans):
        print("Лишних документов и картинок нет.")
    return deleted


def main():
    ap = argparse.ArgumentParser(prog="wiki-sync", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--insecure", action="store_true", help="не проверять TLS-сертификат вики")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="показать, что изменилось в вики")
    p.add_argument("--report", help="сохранить отчёт в файл (markdown)")
    p = sub.add_parser("accept", help="отметить правки страниц перенесёнными")
    p.add_argument("ids", nargs="*", help="wiki_page_id страниц")
    p.add_argument("--all", action="store_true", help="все страницы")
    p = sub.add_parser("pull", help="выгрузить новые страницы и перенести правки из вики в документы")
    p.add_argument("ids", nargs="*", help="wiki_page_id страниц (по умолчанию — все изменённые и новые)")
    p.add_argument("--dry-run", action="store_true", help="только показать изменения, файлы не трогать")
    p.add_argument("--force", action="store_true", help="переносить и в файлы с незакоммиченными правками")
    p.add_argument("--no-new", action="store_true", help="не добавлять новые страницы вики")
    p.add_argument("--no-clean", action="store_true", help="не предлагать удаление лишних документов и картинок")
    p.add_argument("--theirs", action="store_true",
                   help="заменить текст документа версией из вики целиком, без слияния (только для указанных страниц)")
    p = sub.add_parser("clean", help="предложить удаление документов удалённых страниц и картинок без ссылок")
    p.add_argument("--dry-run", action="store_true", help="только показать список")
    args = ap.parse_args()
    try:
        wiki = Wiki(args.insecure)
        code = {"check": cmd_check, "accept": cmd_accept, "pull": cmd_pull, "clean": cmd_clean}[args.cmd](wiki, args)
    except WikiError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        print("\nПрервано. Повторите pull — уже выгруженные страницы заново не скачиваются.", file=sys.stderr)
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
