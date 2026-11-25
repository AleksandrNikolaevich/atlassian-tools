"""Перенос правок из вики в markdown-документы (команда `wiki-sync pull`).

Для каждой изменённой страницы берутся две её версии из вики: та, с которой документ сверен
(wiki_version), и текущая. Обе конвертируются в markdown одним и тем же конвертером, и разница между
ними накладывается на документ трёхсторонним слиянием (git merge-file). Поэтому:
  * в документ попадает только то, что изменили в вики;
  * правки, сделанные в репозитории, сохраняются;
  * если одно и то же место меняли и там, и там — в документе остаются метки конфликта.

Новые страницы раскладываются по дереву вики: страница с дочерними — папка с README.md, без дочерних —
файл <slug>.md; страница, чьего родителя нет в копии, — в корень копии.
"""
import difflib
import os
import re
import subprocess
import tempfile
import urllib.parse
from collections import Counter

from bs4 import BeautifulSoup

from .confluence import WikiError, page_url
from .convert import (Converter, Page, front_matter, relink_existing, resolve_anchors, resolve_links, slugify, tidy,
                      unique_slug)

CONFLICT_RE = re.compile(r"^(<<<<<<<|>>>>>>>) ", re.M)
LINK_RE = re.compile(r'(\]\(|src=")([^)"\s]+)')


class PullError(Exception):
    pass


def join(*parts):
    return "/".join(p for p in parts if p)


def new_ctx(base, paths, collect):
    """Контекст конвертера: paths — wiki_page_id -> путь документа, на который можно сослаться."""
    return {"base": base, "stats": Counter(), "extra_paths": paths,
            "images": {}, "next_image": 1, "collect": collect, "downloads": {}}


def prepare(html, base_url):
    """HTML-фрагмент страницы из API -> элемент для конвертера: якоря Bookmark<N>, абсолютные адреса."""
    soup = BeautifulSoup(f'<div class="wiki-content">{html}</div>', "lxml")
    content = soup.find("div", class_="wiki-content")
    for el in content.select(".copy-heading-link-container, script, style"):
        el.decompose()
    anchors = {}
    for n, h in enumerate(content.find_all(re.compile(r"^h[1-6]$")), 1):
        if h.get("id"):
            anchors[h["id"]] = f"Bookmark{n}"
            h["id"] = f"Bookmark{n}"
    for a in content.find_all("a", href=True):
        href = a["href"]
        if href.startswith("#"):
            if urllib.parse.unquote(href[1:]) in anchors:
                a["href"] = "#" + anchors[urllib.parse.unquote(href[1:])]
        elif not href.startswith("mailto:"):
            a["href"] = urllib.parse.urljoin(base_url + "/", href)
    for img in content.find_all("img"):
        if img.get("src"):
            img["src"] = urllib.parse.urljoin(base_url + "/", img["src"])
    return content


def to_markdown(pid, title, html, doc_path, slug, ctx):
    """Тело документа («# заголовок» и текст) из HTML одной версии страницы."""
    page = Page(pid, title)
    page.slug, page.path = slug, doc_path
    page.md = Converter(page, ctx).convert(prepare(html, ctx["base"]))
    referenced = {(m.group(1), m.group(2))
                  for m in re.finditer(r"@@LM@@pid:(\d+):([^@]*?)(?:@@U@@.*?)?@@LE@@", page.md, flags=re.S)
                  if m.group(2) and not m.group(2).startswith("f=")}
    resolve_anchors(page, referenced)
    resolve_links(page, {pid: page}, ctx)
    return tidy(f"# {title}\n\n{page.md}")


def split_doc(text):
    end = text.find("\n---", 4)
    return text[:end], text[end + 4:].lstrip("\n")


def set_field(head, key, value):
    line = f'{key}: "' + str(value).replace('"', '\\"') + '"'
    if re.search(rf"^{key}:.*$", head, re.M):
        return re.sub(rf"^{key}:.*$", lambda m: line, head, count=1, flags=re.M)
    return re.sub(r"^(wiki_page_id:.*)$", lambda m: m.group(1) + "\n" + line, head, count=1, flags=re.M)


def merge3(ours, base, theirs, labels):
    """Трёхстороннее слияние через git merge-file: (текст, число конфликтов)."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            paths.append(os.path.join(tmp, name))
            with open(paths[-1], "w", encoding="utf-8") as f:
                f.write(text)
        cmd = ["git", "merge-file", "-p"]
        for label in labels:
            cmd += ["-L", label]
        try:
            res = subprocess.run(cmd + paths, capture_output=True)
        except FileNotFoundError as e:
            raise PullError("для слияния правок нужен git — установите его") from e
        if res.returncode < 0 or res.returncode > 127:
            raise PullError(f"git merge-file завершился с ошибкой: {res.stderr.decode('utf-8', 'replace').strip()}")
        return res.stdout.decode("utf-8"), res.returncode


def is_dirty(root, path):
    """Есть ли в закоммиченном файле незакоммиченные правки (вне git и для новых файлов — нет)."""
    try:
        res = subprocess.run(["git", "status", "--porcelain", "--", path], cwd=root, capture_output=True, text=True)
    except FileNotFoundError:
        return False
    return res.returncode == 0 and any(line and not line.startswith("??") for line in res.stdout.split("\n"))


def fetch_version(wiki, pid, version=None):
    params = {"expand": "body.export_view,version,ancestors"}
    if version is not None:
        params.update(status="historical", version=version)
    data = wiki.get(f"/rest/api/content/{pid}", **params)
    html = ((data.get("body") or {}).get("export_view") or {}).get("value")
    if html is None:
        raise PullError("вики не вернула содержимое страницы (body.export_view)")
    return data, html


def download(wiki, full, downloads):
    for rel, url in downloads.items():
        dst = os.path.join(os.path.dirname(full), rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(wiki.download(url))


# ───────────────────────── изменённые страницы ─────────────────────────

def pull_page(wiki, root, pid, doc, docs, page_info, opts):
    """Переносит правки одной страницы. Возвращает строку отчёта."""
    path, shown = doc["path"], opts.prefix + doc["path"]
    full = os.path.join(root, path)
    if not opts.force and not opts.dry_run and is_dirty(root, path):
        return f"ПРОПУЩЕН {shown}: в файле есть незакоммиченные правки (закоммитьте их или запустите с --force)"
    with open(full, encoding="utf-8") as f:
        head, ours = split_doc(f.read())
    slug_hits = Counter(re.findall(r"_assets/([^/\"\s)]+)/", ours))
    slug = slug_hits.most_common(1)[0][0] if slug_hits else (
        os.path.basename(os.path.dirname(path)) if path.endswith("/README.md") else os.path.basename(path)[:-3])
    asset_dir = os.path.join(os.path.dirname(full), "_assets", slug)
    existing = [int(m.group(1)) for n in (os.listdir(asset_dir) if os.path.isdir(asset_dir) else [])
                for m in [re.match(r"(\d+)\.", n)] if m]

    ctx = new_ctx(wiki.base, {p: d["path"] for p, d in docs.items()}, False)
    new_data, new_html = fetch_version(wiki, pid)
    new_title = new_data.get("title", doc["title"])
    notes = []
    if opts.theirs:
        ctx["next_image"], ctx["collect"] = max(existing, default=0) + 1, True
        theirs = to_markdown(pid, new_title, new_html, path, slug, ctx)
        merged, conflicts = theirs, 0
        notes.append("текст заменён версией из вики целиком, без слияния")
    else:
        if not doc["version"]:
            return f"ПРОПУЩЕН {shown}: нет wiki_version — сверьте документ вручную и выполните wiki-sync accept {pid}"
        old_data, old_html = fetch_version(wiki, pid, doc["version"])
        base = to_markdown(pid, old_data.get("title", doc["title"]), old_html, path, slug, ctx)
        if existing and len(ctx["images"]) != len(existing):
            notes.append(f"картинок в прежней версии страницы {len(ctx['images'])}, а в копии {len(existing)} — "
                         "проверьте картинки в изменённых местах")
        ctx["next_image"], ctx["collect"] = max(existing + [len(ctx["images"])], default=0) + 1, True
        theirs = to_markdown(pid, new_title, new_html, path, slug, ctx)
        if base == theirs:
            merged, conflicts = ours, 0
            notes.append("текст страницы не изменился (правили оформление, метки или вложения)")
        else:
            merged, conflicts = merge3(ours, base, theirs,
                                       ["репозиторий", f"вики, версия {doc['version']}", f"вики, версия {page_info['version']}"])
    if conflicts:
        notes.append(f"КОНФЛИКТОВ: {conflicts} — найдите метки <<<<<<< в файле и выберите нужный вариант")

    head = set_field(head, "wiki_version", page_info["version"])
    if new_title != doc["title"]:
        head = set_field(head, "title", new_title)
        notes.append(f"страница переименована: «{doc['title']}» → «{new_title}» (файл не переименован)")
    if page_info["parent"] and doc["parent"] and page_info["parent"] != doc["parent"]:
        head = set_field(head, "wiki_parent_id", page_info["parent"])
        notes.append("страница перенесена под другого родителя (файл не перемещён)")

    downloads = {rel: url for rel, url in ctx["downloads"].items() if rel in merged}
    summary = f"{shown}: версия {doc['version'] or '—'} → {page_info['version']}, {page_info['when']}, {page_info['by']}"
    if opts.dry_run:
        diff = "".join(difflib.unified_diff(ours.splitlines(True), merged.splitlines(True), "репозиторий", "после переноса", n=2))
        notes.append(f"новых картинок: {len(downloads)}")
        return summary + "".join(f"\n  - {n}" for n in notes) + ("\n" + diff if diff else "\n  (текст не меняется)")
    download(wiki, full, downloads)
    if downloads:
        notes.append(f"новых картинок: {len(downloads)}")
    with open(full, "w", encoding="utf-8") as f:
        f.write(f"{head}\n---\n\n{merged}")
    return summary + "".join(f"\n  - {n}" for n in notes)


def pull(wiki, root, docs, pages, ids, opts):
    """Переносит правки страниц ids, печатая отчёт. Возвращает число страниц с конфликтами и ошибками."""
    problems = 0
    for pid in ids:
        try:
            line = pull_page(wiki, root, pid, docs[pid], docs, pages[pid], opts)
        except (PullError, WikiError) as e:  # ошибка одной страницы не должна останавливать остальные
            line = f"ОШИБКА {opts.prefix}{docs[pid]['path']}: {e}"
        if line.startswith(("ОШИБКА", "ПРОПУЩЕН")) or "КОНФЛИКТОВ" in line:
            problems += 1
        print(line, flush=True)
    return problems


# ───────────────────────── новые страницы ─────────────────────────

def md_files(root):
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(("_", "."))]
        for name in files:
            if name.endswith(".md"):
                yield os.path.relpath(os.path.join(base, name), root).replace(os.sep, "/")


def promote_to_folder(root, path):
    """Документ получил дочерний: имя.md -> имя/README.md, картинки и ссылки переносятся следом."""
    folder, slug = os.path.dirname(path), os.path.basename(path)[:-3]
    new, old_assets, new_assets = join(folder, slug, "README.md"), join(folder, "_assets", slug), join(folder, slug, "_assets", slug)
    os.makedirs(os.path.join(root, folder, slug), exist_ok=True)
    os.rename(os.path.join(root, path), os.path.join(root, new))
    if os.path.isdir(os.path.join(root, old_assets)):
        os.makedirs(os.path.dirname(os.path.join(root, new_assets)), exist_ok=True)
        os.rename(os.path.join(root, old_assets), os.path.join(root, new_assets))

    for doc in md_files(root):
        base_dir = folder if doc == new else os.path.dirname(doc)  # ссылки в перенесённом файле писались от старой папки
        with open(os.path.join(root, doc), encoding="utf-8") as f:
            text = f.read()

        def repl(m):
            dest = m.group(2)
            if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", dest) or dest.startswith("#"):
                return m.group(0)
            target, sep, frag = dest.partition("#")
            abs_target = os.path.normpath(os.path.join(base_dir, urllib.parse.unquote(target))).replace(os.sep, "/")
            if abs_target == path:
                abs_target = new
            elif abs_target.startswith(old_assets + "/"):
                abs_target = new_assets + abs_target[len(old_assets):]
            elif doc != new:
                return m.group(0)
            rel = os.path.relpath(abs_target, os.path.dirname(doc) or ".").replace(os.sep, "/")
            rel = rel.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
            return m.group(1) + rel + sep + frag

        updated = LINK_RE.sub(repl, text)
        if updated != text:
            with open(os.path.join(root, doc), "w", encoding="utf-8") as f:
                f.write(updated)
    return new


def plan_new(root, docs, new_pages):
    """Куда положить новые страницы: pid -> {path, slug, promote}, родители раньше детей."""
    parents = {page["parent"] for page in new_pages.values()}
    order = sorted(new_pages, key=lambda pid: (len(new_pages[pid]["ancestors"]), new_pages[pid]["title"], pid))
    # папки, где уже лежат дочерние документы страницы (её саму не удалось выгрузить в прошлый раз)
    homes = {}
    for doc in docs.values():
        unit = os.path.dirname(doc["path"]) if doc["path"].endswith("/README.md") else doc["path"][:-3]
        if doc["parent"] and os.path.dirname(unit):
            homes.setdefault(doc["parent"], os.path.dirname(unit))
    plan, taken = {}, {}
    for pid in order:
        page = new_pages[pid]
        home = homes.get(pid)
        if home and not os.path.exists(os.path.join(root, home, "README.md")):
            plan[pid] = {"path": join(home, "README.md"), "slug": os.path.basename(home), "promote": None}
            continue
        parent, promote = page["parent"], None
        if parent in plan:
            folder = os.path.dirname(plan[parent]["path"])
        elif parent in docs:
            ppath = docs[parent]["path"]
            if ppath.endswith("/README.md"):
                folder = os.path.dirname(ppath)
            else:
                promote, folder = ppath, ppath[:-3]
        else:
            folder = ""  # родителя нет в копии — страница верхнего уровня
        names = taken.setdefault(folder, {n[:-3] if n.endswith(".md") else n
                                          for n in (os.listdir(os.path.join(root, folder))
                                                    if os.path.isdir(os.path.join(root, folder)) else [])})
        slug = unique_slug(slugify(page["title"]), names)
        path = join(folder, slug, "README.md") if pid in parents else join(folder, f"{slug}.md")
        plan[pid] = {"path": path, "slug": slug, "promote": promote}
    return plan


def import_new(wiki, root, docs, new_pages, opts):
    """Создаёт документы для новых страниц вики, печатая отчёт. Возвращает число проблем."""
    plan, problems = plan_new(root, docs, new_pages), 0
    if opts.dry_run:
        for pid, item in plan.items():
            note = f" (документ {opts.prefix}{item['promote']} станет папкой)" if item["promote"] else ""
            print(f"НОВЫЙ {opts.prefix}{item['path']} — «{new_pages[pid]['title']}»{note}")
        return problems
    for item in plan.values():  # сначала превращаем родителей в папки: от этого зависят пути и ссылки
        if item["promote"] and os.path.isfile(os.path.join(root, item["promote"])):
            new_parent = promote_to_folder(root, item["promote"])
            for doc in docs.values():
                if doc["path"] == item["promote"]:
                    doc["path"] = new_parent
            print(f"{opts.prefix}{item['promote']} → {opts.prefix}{new_parent}: у документа появился дочерний")
    paths = {p: d["path"] for p, d in docs.items()}
    paths.update({pid: item["path"] for pid, item in plan.items()})
    created = {}
    for n, (pid, item) in enumerate(plan.items(), 1):
        page = new_pages[pid]
        try:
            ctx = new_ctx(wiki.base, paths, True)
            data, html = fetch_version(wiki, pid)
            title = data.get("title", page["title"])
            body = to_markdown(pid, title, html, item["path"], item["slug"], ctx)
            full = os.path.join(root, item["path"])
            os.makedirs(os.path.dirname(full), exist_ok=True)
            download(wiki, full, ctx["downloads"])
            head = front_matter({"title": title, "wiki_page_id": pid, "wiki_version": page["version"],
                                 "wiki_parent_id": page["parent"], "wiki_url": page_url(wiki.base, pid)})
            with open(full, "w", encoding="utf-8") as f:
                f.write(f"{head}\n\n{body}")
            created[pid] = item["path"]
            print(f"[{n}/{len(plan)}] НОВЫЙ {opts.prefix}{item['path']} — «{title}», {page['when']}, {page['by']}; "
                  f"картинок: {len(ctx['downloads'])}", flush=True)
        except (PullError, WikiError) as e:
            problems += 1
            print(f"[{n}/{len(plan)}] ОШИБКА новая страница «{page['title']}» ({pid}): {e}", flush=True)
    if created:  # ссылки на вики, ведущие на добавленные страницы, становятся относительными
        relink_existing(root, {p: d["path"] for p, d in docs.items()}, created, wiki.base)
    return problems
