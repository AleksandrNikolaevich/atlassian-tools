"""HTML страницы Confluence (body.export_view) -> markdown.

Конвертер общий для любых пространств: таблицы с объёмными ячейками становятся набором записей
(«### Строка 1» и поля «- **Колонка:** значение»), короткие — обычными markdown-таблицами; ссылки на
страницы, которые есть в локальной копии, — относительными ссылками на документы.

Ссылки и якоря собираются в два прохода: при конвертации ставятся маркеры @@LS@@…@@LM@@…@@LE@@,
@@A:…@@ и @@H:…@@, затем resolve_anchors и resolve_links заменяют их на markdown.
"""
import os
import re
import unicodedata
import urllib.parse
from collections import Counter

from bs4 import Comment, NavigableString, Tag

BLOCK_TAGS = ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "pre",
              "blockquote", "hr", "fieldset", "li", "tr", "td", "th", "tbody", "iframe", "dl"]
SKIP_TAGS = {"script", "style", "input", "colgroup", "col", "button", "select", "textarea", "svg"}
EMOTICONS = {"tick": "✅", "cross": "❌", "warning": "⚠️", "information": "ℹ️", "question": "❓",
             "plus": "➕", "minus": "➖", "light-on": "💡", "yellow-star": "⭐", "forbidden": "⛔"}
HEADER_BG = {"grey", "gray", "#f4f5f7", "#c1c7d0", "#dfe1e6", "#ebecf0", "#f0f0f0"}
NUM_COL_NAMES = {"№", "", "#", "n", "№ п/п", "п/п", "no"}
IMAGE_EXT = {"png": "png", "jpg": "jpg", "jpeg": "jpg", "gif": "gif", "svg": "svg", "webp": "webp"}


# ───────────────────────── общие функции ─────────────────────────

def slugify(title, maxlen=70):
    """«Счета (стартовый экран)» -> «счета-стартовый-экран»."""
    t = unicodedata.normalize("NFC", title).replace("\xa0", " ")
    t = re.sub(r"[^\w]+", "-", t.lower(), flags=re.U).strip("-_")
    if len(t) > maxlen:
        t = t[:maxlen].rsplit("-", 1)[0]
    return t or "без-названия"


def unique_slug(slug, taken):
    base, n = slug, 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    taken.add(slug)
    return slug


def anchor_slugs(text):
    """Якорь заголовка в двух диалектах: (GitHub/VS Code, GitLab)."""
    t = re.sub(r"<[^>]+>", "", text)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"[`*~\\]", "", t).strip().lower()
    t = re.sub(r"[^\w\- ]+", "", t, flags=re.U)
    github = t.replace(" ", "-")
    gitlab = re.sub(r"-{2,}", "-", github)
    return github, gitlab


def yaml_str(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def front_matter(fields):
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}: {yaml_str(value)}")
    lines.append("---")
    return "\n".join(lines)


def md_escape(text):
    """Экранирует символы, которые markdown воспринял бы как разметку."""
    text = text.replace("\\", "\\\\").replace("*", "\\*")
    text = re.sub(r"<(?=[A-Za-zА-Яа-яёЁ/!?_])", r"\\<", text)
    return text


def md_link(text, dest):
    if text.replace("\\", "") == dest and re.fullmatch(r"https?://[^\s<>]+", dest):
        return f"<{dest}>"
    dest = dest.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    text = text.replace("\n", " ").replace("[", "\\[").replace("]", "\\]")
    return f"[{text}]({dest})"


def rel_link(from_md, to_md):
    """Относительная ссылка между md-файлами (пути от корня копии)."""
    rel = os.path.relpath(to_md, os.path.dirname(from_md) or ".")
    return rel.replace(os.sep, "/")


_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*)?")
_SESSION_RE = re.compile(r"\b(JSESSIONID|session-cookie)=[A-Za-z0-9_.\-]{16,}")
_ATL_RE = re.compile(r"\batl_token=[0-9a-f]{16,}")


def redact(md):
    """Заменяет на заглушки токены и сессии, попавшие в вики вместе с примерами запросов."""
    out, fence = [], False
    for line in md.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
        lt = "<" if fence else "\\<"
        line = _JWT_RE.sub(lambda m: f"{lt}token>", line)
        line = _SESSION_RE.sub(lambda m: f"{m.group(1)}={lt}session>", line)
        out.append(_ATL_RE.sub("atl_token=redacted", line))
    return "\n".join(out)


def tidy(md):
    """Финальная чистка markdown: пустые строки, хвостовые пробелы, слипшееся выделение."""
    md = redact(md)
    md = re.sub(r"(?<!\\)\*\*\*\*", "", md)
    md = re.sub(r"(<img [^>]+>)(?=<img )", r"\1 ", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


class Page:
    def __init__(self, pid, title):
        self.id, self.title = pid, title
        self.slug = None
        self.path = None        # md-путь от корня копии
        self.md = None          # markdown с маркерами
        self.anchors = {}       # имя закладки -> якорь
        self.heading_keys = {}  # заголовок без пробелов -> якорь (для ссылок из вики)


# ───────────────────────── конвертация страницы ─────────────────────────

def classes(el):
    return el.get("class") or []


def is_block(el):
    return el.name in BLOCK_TAGS or el.find(BLOCK_TAGS) is not None


def wrap(inner, mark):
    if not inner.strip():
        return inner
    out = []
    for line in inner.split("\n"):
        m = re.match(r"^(\s*)(.*?)(\s*)$", line, re.S)
        core = m.group(2)
        if not core or re.fullmatch(r"(@@A:[^@]+@@|<img [^>]+>|!\[[^\]]*\]\([^)]*\))+", core):
            out.append(line)
            continue
        if mark == "**":
            core = core.replace("**", "")
        out.append(f"{m.group(1)}{mark}{core}{mark}{m.group(3)}")
    return "\n".join(out)


def clean_inline(text):
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    lines = [ln for ln in lines if ln]
    text = "\\\n".join(lines)
    text = re.sub(r"(?<!\\)\*\*\*\*", "", text)
    if re.fullmatch(r"[-+#>]|\d+[.)]", text):
        text = "\\" + text
    elif re.match(r"(#{1,6}|[-+]|\d+[.)])\s", text) and "\n" not in text[:3]:
        if text[0] in "#-+":
            text = "\\" + text
    return text


def plain(text):
    """Убирает разметку выделения — для заголовков и ключей."""
    text = re.sub(r"@@A:[^@]+@@", "", text)
    text = re.sub(r"(?<!\\)(\*\*|~~)", "", text)
    return re.sub(r"\s+", " ", text.replace("\\\n", " ")).strip()


def indent(text, n):
    pad = " " * n
    return "\n".join(pad + ln if ln else ln for ln in text.split("\n"))


def is_list_block(block):
    return bool(re.match(r"(- |\d+\. )", block))


class Converter:
    """ctx: base — адрес вики; images, next_image, collect, downloads — картинки страницы; stats."""

    def __init__(self, page, ctx):
        self.page, self.ctx = page, ctx
        self.hl = 1
        self.header_history = []
        self.stats = ctx["stats"]

    # ── строчные элементы ──
    def inline_children(self, el):
        parts = []
        for ch in el.children:
            if isinstance(ch, Comment):
                continue
            if isinstance(ch, NavigableString):
                parts.append(md_escape(re.sub(r"\s+", " ", str(ch))))
            elif isinstance(ch, Tag):
                parts.append(self.inline(ch))
        return "".join(parts)

    def anchor_mark(self, el):
        name = el.get("id") or el.get("name")
        if name and (name.startswith("Bookmark") or "confluence-anchor-link" in classes(el) or el.name == "a"):
            return f"@@A:{name}@@"
        return ""

    def inline(self, el):
        name, cls = el.name, classes(el)
        if name == "br":
            return "\n"
        if name == "img":
            return self.image(el)
        if name in SKIP_TAGS or "nh-number" in cls or "hidden" in cls or "expand-control" in cls:
            return ""
        if "jira-issue" in cls:
            return self.jira(el)
        if "aui-lozenge" in cls or "status-macro" in cls:
            text = el.get_text(" ", strip=True)
            return f"`{text}`" if text else ""
        if name == "a":
            return self.link(el)
        if name == "code":
            text = re.sub(r"\s+", " ", el.get_text()).strip()
            return f"`{text}`" if text else ""
        if name == "time":
            return md_escape(el.get_text(strip=True) or el.get("datetime", ""))
        inner = self.anchor_mark(el) + self.inline_children(el)
        if name in ("strong", "b"):
            return wrap(inner, "**")
        if name in ("em", "i"):
            return wrap(inner, "*")
        if name in ("s", "del", "strike"):
            return wrap(inner, "~~")
        return inner

    def jira(self, el):
        key = el.get("data-jira-key") or ""
        a = el.find("a", class_="jira-issue-key")
        summary = el.find(class_="summary")
        status = el.find(class_="aui-lozenge")
        text = md_link(key or a.get_text(strip=True), a["href"]) if a else key
        if summary and summary.get_text(strip=True):
            text += " — " + md_escape(summary.get_text(" ", strip=True))
        if status and status.get_text(strip=True):
            text += f" (`{status.get_text(' ', strip=True)}`)"
        return text

    def link(self, el):
        href = (el.get("href") or "").strip()
        inner = self.inline_children(el)
        if not href:
            return self.anchor_mark(el) + inner
        text = re.sub(r"\s+", " ", inner).strip()
        if "user-mention" in classes(el) or "/display/~" in href:
            return text
        if not text:
            return ""
        bare = re.sub(r"^https?://", "", href).rstrip("/")
        if plain(text).replace("\\", "") == bare and "/" not in bare and not bare.startswith("www."):
            return text  # автоссылка Confluence на «children.id» и т.п.
        return f"@@LS@@{text}@@LM@@{self.link_target(href)}@@LE@@"

    def link_target(self, href):
        if href.startswith("#"):
            return f"pid:{self.page.id}:{urllib.parse.unquote(href[1:])}"
        base = self.ctx["base"]
        if urllib.parse.urlsplit(href).netloc == urllib.parse.urlsplit(base).netloc:
            url, _, frag = href.partition("#")
            m = re.search(r"pageId=(\d+)", url) or re.search(r"/pages/(\d+)", url)
            if m:
                pid = m.group(1)
                canon = f"{base}/pages/viewpage.action?pageId={pid}" + (f"#{frag}" if frag else "")
                frag = urllib.parse.unquote(frag)
                return f"pid:{pid}:{'f=' + frag if frag else ''}@@U@@{canon}"
        return f"url:{href}"

    def image(self, el):
        """Картинка-вложение страницы: путь _assets/<slug>/NN.ext, файл скачивается, если ctx['collect']."""
        cls = classes(el)
        for c in cls:
            if c.startswith("emoticon-"):
                return EMOTICONS.get(c[9:], "")
        if "icon" in cls or "emoticon" in cls:
            return ""
        url = el.get("src") or ""
        # вложение, добавленное после сохранения страницы, вики отдаёт адресом без номера страницы:
        # /download/attachments/embedded-page/<пространство>/<название страницы>/<файл>
        e = re.search(r"^(.*?)/download/attachments/embedded-page/[^/]+/([^?#]+)", url)
        if e and urllib.parse.unquote(e.group(2)).startswith(self.page.title + "/"):
            name = urllib.parse.unquote(e.group(2))[len(self.page.title) + 1:]
            url = f"{e.group(1)}/download/attachments/{self.page.id}/{urllib.parse.quote(name)}"
        m = re.search(r"/download/(attachments|thumbnails)/(\d+)/([^?#]+)(?:\?([^#]*))?", url)
        if not m:
            return ""  # значки интерфейса и внешние картинки
        name = urllib.parse.unquote(m.group(3))
        ext = IMAGE_EXT.get(name.rsplit(".", 1)[-1].lower() if "." in name else "")
        if not ext:
            return ""
        version = urllib.parse.parse_qs(m.group(4) or "").get("version", [""])[0]
        key = f"{m.group(2)}/{name}@{version}"
        images = self.ctx["images"]
        if key not in images:
            images[key] = f"_assets/{self.page.slug}/{self.ctx['next_image']:02d}.{ext}"
            self.ctx["next_image"] += 1
            if self.ctx["collect"]:
                self.ctx["downloads"][images[key]] = url.replace("/download/thumbnails/", "/download/attachments/")
        width = el.get("width")
        if width and width.isdigit():
            return f'<img src="{images[key]}" width="{width}" alt="">'
        return f"![]({images[key]})"

    # ── блочные элементы ──
    def blocks(self, node, nested=False):
        out, buf = [], []

        def flush():
            text = clean_inline("".join(buf))
            buf.clear()
            if text:
                out.append(text)

        for ch in node.children:
            if isinstance(ch, Comment):
                continue
            if isinstance(ch, NavigableString):
                buf.append(md_escape(re.sub(r"\s+", " ", str(ch))))
            elif ch.name in SKIP_TAGS:
                continue
            elif is_block(ch):
                flush()
                out.extend(self.block(ch, nested))
            else:
                buf.append(self.inline(ch))
        flush()
        return out

    def block(self, el, nested):
        name, cls = el.name, classes(el)
        if "hidden" in cls or "toc-macro" in cls or "plugin_pagetree" in cls:
            return []
        if re.fullmatch(r"h[1-6]", name):
            return self.heading(el, nested)
        if name in ("ul", "ol"):
            return self.list(el)
        if name == "table":
            return self.table(el, nested)
        if name == "pre":
            return self.code(el)
        if name == "hr":
            return ["---"]
        if name == "blockquote":
            return self.quote(self.blocks(el, True))
        if name == "iframe":
            src = el.get("src")
            return [md_link("встроенное содержимое", src)] if src else []
        if "confluence-information-macro" in cls:
            body = el.find(class_="confluence-information-macro-body") or el
            title = el.find(class_="title")
            inner = self.blocks(body, True)
            if title and title.get_text(strip=True):
                inner.insert(0, f"**{md_escape(title.get_text(' ', strip=True))}**")
            return self.quote(inner)
        if "expand-container" in cls:
            title = el.find(class_="expand-control-text")
            body = el.find(class_="expand-content") or el
            head = [f"**{md_escape(title.get_text(' ', strip=True))}**"] if title else []
            return head + self.blocks(body, nested)
        if "panel" in cls and el.find(class_="panelHeader", recursive=False):
            head = el.find(class_="panelHeader", recursive=False)
            body = el.find(class_="panelContent", recursive=False) or el
            return [f"**{md_escape(head.get_text(' ', strip=True))}**"] + self.blocks(body, nested)
        return self.blocks(el, nested)

    def quote(self, blocks):
        if not blocks:
            return []
        text = "\n\n".join(blocks)
        return ["\n".join(("> " + ln) if ln else ">" for ln in text.split("\n"))]

    def heading(self, el, nested):
        raw = self.anchor_mark(el) + self.inline_children(el)
        names = re.findall(r"@@A:([^@]+)@@", raw)
        text = plain(raw).replace("\\\n", " ")
        img_re = r"<img [^>]+>|!\[[^\]]*\]\([^)]*\)"
        images = [" ".join(re.findall(img_re, text))] if re.search(img_re, text) else []
        text = re.sub(img_re, "", text).strip()
        if not text:
            return images
        if nested:
            return ["".join(f"@@A:{n}@@" for n in names) + f"**{text}**"] + images
        level = min(6, int(el.name[1]) + 1, self.hl + 1)
        self.hl = level
        mark = f"@@H:{','.join(names)}@@\n" if names else ""
        return [f"{mark}{'#' * level} {text}"] + images

    def list(self, el):
        ordered = el.name == "ol"
        try:
            n = int(el.get("start") or 1)
        except ValueError:
            n = 1
        items = []
        for ch in el.children:
            if not isinstance(ch, Tag):
                continue
            if ch.name != "li":
                extra = self.block(ch, True) if is_block(ch) else []
                if extra and items:
                    items[-1] += "\n" + indent("\n\n".join(extra), 2)
                else:
                    items.extend(extra)
                continue
            blocks = self.blocks(ch, True)
            if not blocks:
                continue
            marker = f"{n}. " if ordered else "- "
            if "data-inline-task-id" in ch.attrs:
                marker = "- [x] " if "checked" in classes(ch) else "- [ ] "
            body = blocks[0]
            for b in blocks[1:]:
                body += ("\n" if is_list_block(b) else "\n\n") + b
            items.append(marker + indent(body, len(marker) if ordered else 2)[len(marker) if ordered else 2:])
            n += 1
        return ["\n".join(items)] if items else []

    def code(self, el):
        text = el.get_text().replace("\xa0", " ").strip("\n")
        if not text.strip():
            return []
        params = el.get("data-syntaxhighlighter-params") or ""
        m = re.search(r"brush:\s*(\w+)", params)
        lang = m.group(1) if m else ("json" if text.lstrip()[:1] in "{[" else "")
        if lang in ("plain", "text", "none"):
            lang = ""
        fence = "````" if "```" in text else "```"
        return [f"{fence}{lang}\n{text}\n{fence}"]

    # ── таблицы ──
    def grid(self, table):
        rows = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
        grid, spans = [], {}
        for tr in rows:
            cells = iter(tr.find_all(["td", "th"], recursive=False))
            row, ci = [], 0
            while True:
                if ci in spans:
                    cell, left = spans[ci]
                    row.append((cell, "row"))
                    if left > 1:
                        spans[ci] = (cell, left - 1)
                    else:
                        del spans[ci]
                    ci += 1
                    continue
                cell = next(cells, None)
                if cell is None:
                    break

                def span(attr):
                    try:
                        return max(1, min(int(cell.get(attr) or 1), 100))
                    except ValueError:
                        return 1

                cs, rs = span("colspan"), span("rowspan")
                for k in range(cs):
                    row.append((cell, "col" if k else None))
                    if rs > 1:
                        spans[ci + k] = (cell, rs - 1)
                ci += cs
            grid.append(row)
        ncols = max((len(r) for r in grid), default=0)
        for r in grid:
            r.extend([(None, None)] * (ncols - len(r)))
        return grid, ncols

    def kept_img(self, cell):
        return any("icon" not in classes(i) and "emoticon" not in classes(i) for i in cell.find_all("img"))

    def cell_empty(self, cell):
        return cell is None or (not cell.get_text().replace("\xa0", " ").strip() and not self.kept_img(cell))

    def headerish(self, cell):
        if cell is None:
            return False
        if cell.name == "th":
            return True
        bg = (cell.get("bgcolor") or "").lower()
        if bg not in HEADER_BG and not any(c in ("highlight-grey", "highlight-#f4f5f7") for c in classes(cell)):
            return False
        text = re.sub(r"\s+", "", cell.get_text())
        bold = re.sub(r"\s+", "", "".join(s.get_text() for s in cell.find_all(["strong", "b"])))
        return bool(text) and len(bold) >= len(text) * 0.8

    def cell_text(self, cell):
        return plain(" ".join(self.blocks(cell, True))) if cell is not None else ""

    def cell_inline(self, cell, limit=400):
        """Содержимое ячейки одной строкой для pipe-таблицы или None, если ячейка сложная."""
        if cell is None:
            return ""
        if cell.find(["table", "pre", "h1", "h2", "h3", "h4", "h5", "h6"]) or self.kept_img(cell):
            return None
        if any(li.find_parent("li") for li in cell.find_all("li")):
            return None
        blocks = self.blocks(cell, True)
        lines = []
        for b in blocks:
            for ln in b.replace("\\\n", "\n").split("\n"):
                ln = re.sub(r"^\s*- ", "• ", ln.strip())
                if ln:
                    lines.append(ln)
        text = re.sub(r"^\\(?=[-+#>]$)", "", "<br>".join(lines))
        if len(re.sub(r"@@LM@@.*?@@LE@@", "", text)) > limit:
            return None
        return text.replace("|", "\\|")

    def field(self, key, cell):
        """Пункт «- **Ключ:** значение»; сложное значение уходит под пункт с отступом."""
        blocks = self.blocks(cell, True) if cell is not None else []
        head = f"- **{key}:**" if key else "-"
        if not blocks:
            return f"{head} —" if key else None
        first, rest = blocks[0], blocks[1:]
        if "\n" in first or first.startswith(("|", "```", ">", "- ", "#")) or re.match(r"\d+\. ", first):
            body = "\n\n".join(blocks)
            sep = "\n" if is_list_block(first) and key else "\n\n"
            return f"{head}{sep}{indent(body, 2)}" if key else "- " + indent(body, 2)[2:]
        out = f"{head} " + re.sub(r"^\\(?=[-+#>]$)", "", first)
        for b in rest:
            out += ("\n" if is_list_block(b) else "\n\n") + indent(b, 2)
        return out

    def table(self, table, nested):
        if "confluence-highlight-macro-table" in classes(table):
            body = table.find(class_="macro-body") or table
            return self.quote(self.blocks(body, True))
        grid, ncols = self.grid(table)
        grid = [r for r in grid if not all(self.cell_empty(c) for c, _ in r)]
        if not grid or ncols == 0:
            return []
        self.stats["tables"] += 1
        if ncols == 1:
            out = []
            for row in grid:
                out.extend(self.blocks(row[0][0], nested))
            return out
        if self.is_kv(grid, ncols):
            self.stats["tables_kv"] += 1
            items = []
            for row in grid:
                if row[1][1] == "col":  # строка-заголовок на всю ширину
                    items.append(f"- **{plain(self.cell_text(row[0][0]))}**")
                    continue
                key = plain(self.cell_text(row[0][0])).rstrip(":").strip() or "—"
                items.append(self.field(key, row[1][0]))
            return ["\n".join(items)]

        # заголовочные строки
        nhead = 0
        for row in grid:
            cells = {id(c): c for c, _ in row if c is not None}.values()
            if len(cells) > 1 and all(self.headerish(c) for c in cells) or \
                    (nhead > 0 and all(self.headerish(c) for c in cells) and any(s == "row" for _, s in row)):
                nhead += 1
            else:
                break
        if nhead == len(grid):
            nhead = 1 if len(grid) > 1 else 0
        headers = None
        if nhead:
            headers = []
            for ci in range(ncols):
                parts = []
                for row in grid[:nhead]:
                    t = self.cell_text(row[ci][0])
                    if t and t not in parts:
                        parts.append(t)
                headers.append(": ".join(parts))
            self.header_history.append(headers)
        else:
            headers = self.infer_headers(ncols)
        body = grid[nhead:]
        if not body:
            return [" · ".join(h for h in headers if h)] if headers else []

        def is_group(row):
            return len({id(c) for c, _ in row}) == 1 or \
                (sum(1 for c, s in row if not self.cell_empty(c) and s != "col") == 1 and
                 all(c is None or c.name == "th" for c, _ in row) and ncols > 2)

        # простая таблица -> pipe
        simple, pipe_rows = True, []
        for row in body:
            if is_group(row):
                first = next((c for c, _ in row if not self.cell_empty(c)), None)
                pipe_rows.append([f"**{self.cell_text(first)}**"] + [""] * (ncols - 1))
                continue
            vals = []
            for c, s in row:
                v = "" if s == "col" else self.cell_inline(c)
                if v is None:
                    simple = False
                    break
                vals.append(v)
            if not simple:
                break
            pipe_rows.append(vals)
        if simple:
            self.stats["tables_pipe"] += 1
            head = [h.replace("|", "\\|") for h in headers] if headers else [""] * ncols
            keep = [ci for ci in range(ncols) if head[ci] or any(r[ci] for r in pipe_rows)]
            lines = ["| " + " | ".join(head[ci] for ci in keep) + " |",
                     "|" + "|".join("---" for _ in keep) + "|"]
            lines += ["| " + " | ".join(r[ci] for ci in keep) + " |" for r in pipe_rows]
            return ["\n".join(lines)]

        # сложная таблица -> записи
        self.stats["tables_records"] += 1
        if headers is None and len(body) == 1:
            items = [self.field("", c) for c, s in body[0] if s != "col" and not self.cell_empty(c)]
            return ["\n".join(i for i in items if i)]
        return self.records(body, headers, ncols, nested, is_group)

    def is_kv(self, grid, ncols):
        if ncols != 2:
            return False
        first = grid[0]
        if first[0][0] is not None and first[1][0] is not None and first[1][1] is None \
                and first[0][0].name == "th" and first[1][0].name == "th":
            return False
        keys = [r[0][0] for r in grid]
        marked = sum(1 for k in keys if self.headerish(k))
        if marked * 2 < len(keys):
            return False
        return all(len(self.cell_text(k)) <= 80 for k in keys)

    def infer_headers(self, ncols):
        """Таблица без строки заголовка — продолжение таблицы шагов выше: берём её заголовки."""
        def scenario(h):
            return any(re.search(r"шаг|действи|ответ сист|step|action", x.lower()) for x in h)

        for h in reversed(self.header_history):
            if len(h) == ncols and scenario(h):
                return list(h)
        return None

    def records(self, body, headers, ncols, nested, is_group):
        names = headers or [""] * ncols
        low = [n.lower() for n in names]
        num_col = low[0] in NUM_COL_NAMES and sum(
            1 for r in body if re.fullmatch(r"[\d.]*", self.cell_text(r[0][0]))) >= len(body) * 0.7
        elem_col = next((i for i, n in enumerate(low) if n.startswith(("элемент", "наименование", "название",
                                                                         "element", "name", "title"))), None)
        is_steps = any(re.search(r"шаг|действи|ответ сист|step|action", n) for n in low)
        out, idx = [], 0
        level = self.hl + 1
        for row in body:
            if is_group(row):
                first = next((c for c, _ in row if not self.cell_empty(c)), None)
                out.append(f"**{self.cell_text(first)}**")
                continue
            idx += 1
            skip = set()
            num = str(idx)
            if num_col:
                skip.add(0)
                num = self.cell_text(row[0][0]).rstrip(".") or str(idx)
            title_col = elem_col
            if title_col is None and not is_steps:
                title_col = 1 if num_col else 0
            title = ""
            if title_col is not None and title_col < ncols and row[title_col][1] != "col":
                cand = self.cell_inline(row[title_col][0], 110)
                if cand and "<br>" not in cand and "@@" not in cand:
                    title = plain(cand).replace("\\|", "|")
                    skip.add(title_col)
            if is_steps:
                label = f"Шаг {num}"
            elif elem_col is not None and low[elem_col].startswith("элемент"):
                label = f"Элемент {num}" + (f". {title}" if title else "")
            elif title:
                label = title
            else:
                label = f"Строка {num}"
            if title and is_steps:
                skip.discard(title_col)
            fields = []
            for ci, (cell, span) in enumerate(row):
                if ci in skip or span == "col" or self.cell_empty(cell):
                    continue
                if span == "row" and self.cell_inline(cell, 200) is None:
                    fields.append(f"- **{names[ci]}:** см. предыдущую запись" if names[ci]
                                  else "- см. предыдущую запись")
                    continue
                item = self.field(names[ci], cell)
                if item:
                    fields.append(item)
            if nested or level > 6:
                out.append(f"- **{label}**" + ("\n" + indent("\n".join(fields), 2) if fields else ""))
            else:
                out.append(f"{'#' * level} {label}" + ("\n\n" + "\n".join(fields) if fields else ""))
        if nested or level > 6:
            merged, run = [], []
            for b in out:
                if b.startswith("- **"):
                    run.append(b)
                else:
                    if run:
                        merged.append("\n".join(run))
                        run = []
                    merged.append(b)
            if run:
                merged.append("\n".join(run))
            return merged
        return out

    def convert(self, content):
        return "\n\n".join(self.blocks(content))


# ───────────────────────── второй проход: якоря и ссылки ─────────────────────────

def nospace(text):
    return re.sub(r"[^\w]+", "", plain(text).lower(), flags=re.U)


def resolve_anchors(page, referenced):
    """Заменяет маркеры @@H/@@A на якоря; заполняет page.anchors и page.heading_keys."""
    lines, out, used, fence = page.md.split("\n"), [], Counter(), False
    pending = []
    for line in lines:
        if line.lstrip().startswith("```"):
            fence = not fence
        m = re.fullmatch(r"@@H:([^@]*)@@", line)
        if m and not fence:
            pending = m.group(1).split(",")
            continue
        h = re.match(r"(#{1,6}) (.+)$", line) if not fence else None
        if h:
            github, gitlab = anchor_slugs(h.group(2))
            n = used[github]
            used[github] += 1
            if n:
                github, gitlab = f"{github}-{n}", f"{gitlab}-{n}"
            page.heading_keys.setdefault(nospace(h.group(2)), gitlab)
            if pending:
                if github != gitlab and any((page.id, name) in referenced for name in pending):
                    out.append(f'<a id="{gitlab}"></a>')
                    out.append("")
                for name in pending:
                    page.anchors[name] = gitlab
                pending = []
        out.append(line)
    md = "\n".join(out)

    def inline_anchor(m):
        name = m.group(1)
        if (page.id, name) in referenced:
            aid = re.sub(r"[^\w\-]+", "-", name.lower(), flags=re.U).strip("-")
            page.anchors[name] = aid
            return f'<a id="{aid}"></a>'
        return ""

    page.md = re.sub(r"@@A:([^@]+)@@", inline_anchor, md)


def resolve_links(page, pages, ctx):
    """Маркеры ссылок -> markdown. Страницы из pages и ctx['extra_paths'] становятся относительными ссылками."""
    stats = ctx["stats"]

    def repl(m):
        text, target = m.group(1), m.group(2)
        if target.startswith("url:"):
            return md_link(text, target[4:])
        target, _, wiki_url = target.partition("@@U@@")
        _, pid, name = target.split(":", 2)
        dest_path = pages[pid].path if pid in pages else ctx["extra_paths"].get(pid)
        if dest_path is None:
            if wiki_url:
                stats["links_wiki_external"] += 1
                return md_link(text, wiki_url)
            stats["links_broken"] += 1
            return text
        anchor = ""
        dest = pages.get(pid)
        if dest is not None and name:
            if name.startswith("f="):
                key = nospace(name[2:])
                best = max((k for k in dest.heading_keys if k and key.endswith(k)), key=len, default=None)
                anchor = dest.heading_keys[best] if best else ""
            else:
                anchor = dest.anchors.get(name, "")
        if pid == page.id:
            if not anchor:
                stats["links_self_unresolved"] += 1
                return text
            return md_link(text, f"#{anchor}")
        stats["links_internal"] += 1
        return md_link(text, rel_link(page.path, dest_path) + (f"#{anchor}" if anchor else ""))

    page.md = re.sub(r"@@LS@@(.*?)@@LM@@(.*?)@@LE@@", repl, page.md, flags=re.S)


def relink_existing(out, keep, new_paths, base):
    """В документах keep заменяет ссылки на вики, ведущие на добавленные страницы, относительными."""
    link_re = re.compile(r"\(" + re.escape(base) + r"/pages/viewpage\.action\?pageId=(\d+)(?:#[^)\s]*)?\)")
    for path in keep.values():
        full = os.path.join(out, path)
        with open(full, encoding="utf-8") as f:
            text = f.read()

        def repl(m):
            if m.group(1) not in new_paths:
                return m.group(0)
            return "(" + rel_link(path, new_paths[m.group(1)]) + ")"

        updated = link_re.sub(repl, text)
        if updated != text:
            with open(full, "w", encoding="utf-8") as f:
                f.write(updated)
