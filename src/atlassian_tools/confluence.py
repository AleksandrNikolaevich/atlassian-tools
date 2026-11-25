"""Клиент REST API Confluence (Server / Data Center). Только стандартная библиотека Python.

Доступ — один из трёх способов (переменные окружения или .env проекта):
  WIKI_USER + WIKI_PASSWORD  логин и пароль: вход выполняется при каждом запуске;
  WIKI_TOKEN                 персональный токен Confluence;
  WIKI_COOKIE                cookie открытой в браузере сессии, например "JSESSIONID=…".
Если задано несколько, используется первый из списка. WIKI_URL — адрес вики.
"""
import base64
import http.client
import http.cookiejar
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

BATCH = 40


class WikiError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class ConfluenceClient:
    """Клиент Confluence REST API с явно заданным подключением."""

    def __init__(self, base_url, *, user=None, password=None, token=None, cookie=None, insecure=False):
        self.base = base_url.rstrip("/")
        self.ssl = ssl._create_unverified_context() if insecure else None
        if user and password:
            self.headers = self.login(user, password)
        elif token:
            self.headers = {"Authorization": "Bearer " + token}
        elif cookie:
            if "=" not in cookie:  # вписали только значение — это значение JSESSIONID
                cookie = f"JSESSIONID={cookie}"
            self.headers = {"Cookie": cookie}
        else:
            raise WikiError("не задан доступ к вики: передайте логин и пароль, токен или cookie")
        self.headers["Accept"] = "application/json"

    def login(self, user, password):
        """Вход по логину и паролю. Возвращает заголовки авторизации для следующих запросов.

        Сначала обычная форма входа Confluence (получаем cookie сессии), затем, если она не подошла,
        Basic-авторизация. Что сработало, проверяется запросом «кто я».
        """
        def known(headers):
            req = urllib.request.Request(f"{self.base}/rest/api/user/current",
                                         headers=dict(headers, Accept="application/json"))
            try:
                with urllib.request.urlopen(req, timeout=60, context=self.ssl) as resp:
                    return json.load(resp).get("type") == "known"
            except urllib.error.HTTPError:
                return False
            except json.JSONDecodeError:
                return False

        try:
            jar = http.cookiejar.CookieJar()
            handlers = [urllib.request.HTTPCookieProcessor(jar)]
            if self.ssl:
                handlers.append(urllib.request.HTTPSHandler(context=self.ssl))
            form = urllib.parse.urlencode({"os_username": user, "os_password": password, "login": "Log in",
                                           "os_destination": "/"}).encode()
            try:
                urllib.request.build_opener(*handlers).open(f"{self.base}/dologin.action", form, timeout=60).close()
            except urllib.error.HTTPError:
                pass  # ответ формы не важен: смотрим, появилась ли рабочая сессия
            session = {"Cookie": "; ".join(f"{c.name}={c.value}" for c in jar)}
            if session["Cookie"] and known(session):
                return session
            basic = {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}
            if known(basic):
                return basic
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            raise WikiError(f"вики недоступна ({self.base}): {e}") from e
        raise WikiError("вход по логину и паролю не удался. Возможные причины: неверный логин или пароль; вики "
                        "просит капчу после неудачных попыток (войдите один раз через браузер); вход идёт через "
                        "единую авторизацию (SSO) — тогда используйте WIKI_COOKIE", 401)

    def get(self, path, **params):
        return self.request("GET", f"{path}?{urllib.parse.urlencode(params)}")

    def send(self, method, path, payload):
        """Запись в вики (PUT/POST). Принимает данные в формате JSON."""
        return self.request(method, path, json.dumps(payload).encode("utf-8"))

    def request(self, method, path, data=None):
        url = f"{self.base}{path}"
        headers = dict(self.headers)
        if data is not None:
            headers.update({"Content-Type": "application/json; charset=utf-8", "X-Atlassian-Token": "no-check"})
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        path = path.split("?")[0]
        try:
            with urllib.request.urlopen(req, timeout=60, context=self.ssl) as resp:
                if "json" not in resp.headers.get("Content-Type", ""):
                    raise WikiError("вики вернула страницу входа вместо данных: сессия истекла или cookie неверна")
                return json.load(resp)
        except urllib.error.HTTPError as e:
            hint = {401: "доступ не принят (токен, пароль или cookie)", 403: "нет прав"}.get(e.code, "")
            try:  # текст ошибки от самой вики, например о некорректном CQL-запросе
                detail = json.loads(e.read().decode("utf-8", "replace")).get("message", "")
            except Exception:
                detail = ""
            raise WikiError(f"вики ответила {e.code} {hint} на {path}" + (f": {detail}" if detail else ""),
                            e.code) from e
        except (urllib.error.URLError, http.client.HTTPException, OSError, json.JSONDecodeError) as e:
            raise WikiError(f"вики недоступна ({self.base}): {e}") from e

    def download(self, url):
        """Содержимое файла (вложения страницы) по адресу в вики."""
        headers = {k: v for k, v in self.headers.items() if k != "Accept"}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120,
                                        context=self.ssl) as resp:
                return resp.read()
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            raise WikiError(f"не удалось скачать {url}: {e}") from e

    def search(self, cql, expand):
        """Все страницы по CQL-запросу с постраничной выдачей."""
        start, out = 0, []
        while True:
            data = self.get("/rest/api/content/search", cql=cql, expand=expand, limit=100, start=start)
            results = data.get("results", [])
            out.extend(results)
            if len(results) < data.get("limit", 100) or not results:
                return out
            start += len(results)

    def pages(self, ids):
        """wiki_page_id -> сведения о странице. Удалённых и недоступных страниц в ответе нет."""
        found = {}
        for i in range(0, len(ids), BATCH):
            cql = f"type = page and id in ({','.join(ids[i:i + BATCH])})"
            for r in self.search(cql, "version,ancestors"):
                found[str(r["id"])] = describe(r)
        for pid in ids:  # поиск видит не все страницы (например, только что созданные) — спрашиваем напрямую
            if pid not in found:
                try:
                    found[pid] = describe(self.get(f"/rest/api/content/{pid}", expand="version,ancestors"))
                except WikiError as e:
                    if e.code not in (403, 404):
                        raise
        return found

    def space_pages(self, key):
        """Все страницы пространства: id -> сведения."""
        return {str(r["id"]): describe(r)
                for r in self.search(f'space = "{key}" and type = page', "version,ancestors")}

    def trashed(self, pid):
        """Сведения о странице, если она лежит в корзине вики; иначе None."""
        try:
            return describe(self.get(f"/rest/api/content/{pid}", status="trashed", expand="version,ancestors"))
        except WikiError as e:
            if e.code in (403, 404):
                return None
            raise

    def descendants_new(self, known, roots, *, ignored=()):
        """Страницы под известными, которых нет среди known, на всю глубину: (id -> сведения, отказы)."""
        seen = set(known)
        roots = tuple(roots)
        excluded = set(roots).union(ignored)
        new, rejected, frontier = {}, [], sorted(seen) + list(roots)
        while frontier:
            found, bad = self.children(frontier)
            rejected += bad
            fresh = {pid: page for pid, page in found.items()
                     if pid not in seen and pid not in excluded}
            new.update(fresh)
            seen.update(fresh)
            frontier = sorted(fresh)
        return new, rejected

    def descendants(self, roots, skip=()):
        """Страницы под roots на всю глубину, без поддеревьев skip: (id -> сведения, отказы)."""
        seen, found, rejected, frontier = set(roots) | set(skip), {}, [], sorted(set(roots) - set(skip))
        while frontier:
            fresh, bad = self.children(frontier)
            rejected += bad
            fresh = {pid: page for pid, page in fresh.items() if pid not in seen}
            found.update(fresh)
            seen.update(fresh)
            frontier = sorted(fresh)
        return found, rejected

    def children(self, parent_ids):
        """Дочерние страницы перечисленных страниц: (id -> сведения, родители, которых вики не приняла).

        На несуществующего или недоступного родителя вики отвечает 400 на весь запрос, поэтому
        пачка при такой ошибке делится пополам, пока не останется один непринятый идентификатор.
        """
        found, rejected = {}, []

        def fetch(ids):
            try:
                # оператор IN для поля parent Confluence Server не поддерживает — перечисляем через OR
                clause = " or ".join(f"parent = {pid}" for pid in ids)
                results = self.search(f"type = page and ({clause})", "version,ancestors")
            except WikiError as e:
                if e.code != 400:
                    raise
                if len(ids) == 1:
                    rejected.append(ids[0])
                    if len(rejected) > 15:  # дело не в отдельных страницах: вики отвергает сам запрос
                        raise WikiError(f"вики не принимает запрос дочерних страниц — {e}", 400) from e
                else:
                    fetch(ids[:len(ids) // 2])
                    fetch(ids[len(ids) // 2:])
                return
            for r in results:
                found[str(r["id"])] = describe(r)

        for i in range(0, len(parent_ids), BATCH):
            fetch(parent_ids[i:i + BATCH])
        return found, rejected


class Wiki(ConfluenceClient):
    def __init__(self, insecure=False):
        from . import config

        base = config.get("WIKI_URL")
        if not base:
            raise WikiError("не задан адрес вики: впишите в .env WIKI_URL=https://…")
        user, password, token, cookie = (config.get(f"WIKI_{key}") for key in ("USER", "PASSWORD", "TOKEN", "COOKIE"))
        if user and not password:
            password = token  # прежняя конфигурация допускала пароль в WIKI_TOKEN
        if not ((user and password) or token or cookie):
            raise WikiError("не задан доступ к вики: впишите в .env WIKI_USER и WIKI_PASSWORD, "
                            "WIKI_TOKEN или WIKI_COOKIE")
        super().__init__(base, user=user, password=password, token=token, cookie=cookie, insecure=insecure)


def describe(result):
    version = result.get("version") or {}
    ancestors = [str(a["id"]) for a in result.get("ancestors") or []]
    return {"title": result.get("title", ""), "version": str(version.get("number", "")),
            "when": (version.get("when") or "")[:16].replace("T", " "),
            "by": (version.get("by") or {}).get("displayName", ""),
            "parent": ancestors[-1] if ancestors else None, "ancestors": ancestors}


def page_url(base, pid):
    return f"{base}/pages/viewpage.action?pageId={pid}"


def diff_url(base, pid, old, new):
    return (f"{base}/pages/diffpagesbyversion.action?pageId={pid}"
            f"&selectedPageVersions={old}&selectedPageVersions={new}")
