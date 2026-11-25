"""Клиент REST API Jira."""
import base64
import http.client
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request


SEARCH_BATCH = 50


class JiraError(Exception):
    pass


class JiraClient:
    """Клиент Jira REST API v2 с явно заданным подключением."""

    def __init__(self, base_url, *, user=None, password=None, token=None, cookie=None, insecure=False):
        self.base = base_url.rstrip("/")
        self.ssl = ssl._create_unverified_context() if insecure else None
        if user and password:
            self.headers = self.login(user, password)
        elif token:
            self.headers = {"Authorization": "Bearer " + token}
        elif cookie:
            self.headers = {"Cookie": cookie if "=" in cookie else f"JSESSIONID={cookie}"}
        else:
            raise JiraError("не задан доступ к Jira: передайте логин и пароль, токен или cookie")
        self.headers["Accept"] = "application/json"

    def login(self, user, password):
        """Вход по логину и паролю: сессия через /rest/auth, если не подошла — Basic-авторизация."""
        def known(headers):
            try:
                self.request("GET", "/rest/api/2/myself", headers=headers)
                return True
            except JiraError as e:
                if "недоступна" in str(e):
                    raise
                return False

        try:
            data = self.request("POST", "/rest/auth/1/session", {"username": user, "password": password},
                                headers={"Accept": "application/json"})
            session = {"Cookie": f"{data['session']['name']}={data['session']['value']}"}
            if known(session):
                return session
        except (JiraError, KeyError, TypeError) as e:
            if "недоступна" in str(e):
                raise
        basic = {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}
        if known(basic):
            return basic
        raise JiraError("вход по логину и паролю не удался. Возможные причины: неверный логин или пароль; Jira "
                        "просит капчу после неудачных попыток (войдите один раз через браузер); вход идёт через "
                        "единую авторизацию (SSO) — тогда используйте JIRA_COOKIE или JIRA_TOKEN")

    def get(self, path, **params):
        return self.request("GET", f"{path}?{urllib.parse.urlencode(params)}")

    def request(self, method, path, payload=None, headers=None):
        headers = dict(self.headers if headers is None else headers)
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers.update({"Content-Type": "application/json", "X-Atlassian-Token": "no-check"})
        req = urllib.request.Request(f"{self.base}{path}", data=data, headers=headers, method=method)
        short = path.split("?")[0]
        try:
            with urllib.request.urlopen(req, timeout=60, context=self.ssl) as resp:
                if resp.status == 204:  # запись прошла, тела ответа нет
                    return None
                if "json" not in resp.headers.get("Content-Type", ""):
                    raise JiraError("Jira вернула страницу входа вместо данных: сессия истекла или cookie неверна")
                return json.load(resp)
        except urllib.error.HTTPError as e:
            hint = {401: "доступ не принят (токен, пароль или cookie)", 403: "нет прав"}.get(e.code, "")
            try:
                body = json.loads(e.read().decode("utf-8", "replace"))
                detail = "; ".join(body.get("errorMessages") or []) or body.get("message", "")
            except Exception:
                detail = ""
            raise JiraError(f"Jira ответила {e.code} {hint} на {short}" + (f": {detail}" if detail else "")) from e
        except (urllib.error.URLError, http.client.HTTPException, OSError, json.JSONDecodeError) as e:
            raise JiraError(f"Jira недоступна ({self.base}): {e}") from e

    def download(self, url, path):
        headers = {k: v for k, v in self.headers.items() if k != "Accept"}
        tmp = path + ".part"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300,
                                        context=self.ssl) as resp, open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise JiraError(f"не удалось скачать {url}: {e}") from e
        os.replace(tmp, path)

    def search(self, jql, fields, expand=""):
        start, out = 0, []
        while True:
            data = self.get("/rest/api/2/search", jql=jql, fields=fields, expand=expand, startAt=start,
                            maxResults=SEARCH_BATCH, validateQuery="warn")
            issues = data.get("issues", [])
            out.extend(issues)
            start += len(issues)
            if not issues or start >= data.get("total", 0):
                return out

    def comments(self, key):
        data = self.get(f"/rest/api/2/issue/{key}/comment", expand="renderedBody", maxResults=1000)
        return data.get("comments", [])
