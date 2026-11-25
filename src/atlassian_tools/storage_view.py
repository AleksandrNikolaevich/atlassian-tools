"""Читаемый вид страницы в формате хранения Confluence — для сравнения правок (wiki-edit diff).

Формат хранения — XML с макросами (ac:*, ri:*). Здесь он упрощается до обычного HTML: макросы,
картинки и ссылки заменяются короткими пометками в квадратных скобках, после чего текст конвертируется
в markdown тем же конвертером, что и при синхронизации. Результат нужен только для чтения человеком.
"""
import html
import re

from bs4 import BeautifulSoup

from .wiki_pull import new_ctx, to_markdown


def simplify(storage):
    """Формат хранения -> HTML без тегов ac:/ri:."""
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", lambda m: html.escape(m.group(1)), storage, flags=re.S)
    soup = BeautifulSoup(f"<div>{text}</div>", "lxml")
    root = soup.find("div")

    def label(text):
        tag = soup.new_tag("span")
        tag.string = f"[{text}]"
        return tag

    for image in root.find_all("ac:image"):
        ref = image.find(["ri:attachment", "ri:url"])
        name = (ref.get("ri:filename") or ref.get("ri:value") or "") if ref else ""
        image.replace_with(label(f"картинка: {name}"))
    for link in root.find_all("ac:link"):
        body = link.find(["ac:link-body", "ac:plain-text-link-body"])
        ref = link.find(["ri:page", "ri:attachment", "ri:user"])
        target = (ref.get("ri:content-title") or ref.get("ri:filename") or "пользователь") if ref else ""
        text = body.get_text(" ", strip=True) if body else ""
        link.replace_with(label(f"ссылка: {text} → {target}" if text and target and text != target
                                else f"ссылка: {text or target or link.get('ac:anchor', '')}"))
    while True:  # макросы могут быть вложены — разворачиваем от внутренних к внешним
        macro = next((m for m in root.find_all(["ac:structured-macro", "ac:macro"])
                      if not m.find(["ac:structured-macro", "ac:macro"])), None)
        if macro is None:
            break
        name = macro.get("ac:name", "макрос")
        params = {p.get("ac:name", ""): p.get_text(" ", strip=True)
                  for p in macro.find_all("ac:parameter", recursive=False)}
        shown = params.get("key") or params.get("title") or params.get("") or ""
        rich = macro.find("ac:rich-text-body", recursive=False)
        plain = macro.find("ac:plain-text-body", recursive=False)
        if rich is not None:
            box = soup.new_tag("div")
            head = soup.new_tag("p")
            head.string = f"[макрос {name}" + (f": {shown}" if shown else "") + "]"
            box.append(head)
            for child in list(rich.contents):
                box.append(child.extract())
            macro.replace_with(box)
        elif plain is not None:
            pre = soup.new_tag("pre")
            pre.string = plain.get_text()
            macro.replace_with(pre)
        else:
            macro.replace_with(label(f"макрос {name}" + (f": {shown}" if shown else "")))
    for tag in root.find_all(re.compile(r"^(ac|ri):")):
        if tag.name == "ac:parameter":
            tag.decompose()
        else:
            tag.name = "div" if tag.name.startswith("ac:layout") or tag.name == "ac:task-list" else "span"
    return root.decode_contents()


def to_md(storage, title):
    return to_markdown("0", title, simplify(storage), "страница.md", "страница",
                       new_ctx("https://wiki.invalid", {}, False))
