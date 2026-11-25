"""Настройки: корень проекта и переменные из его файла .env.

Корень проекта — ближайшая к текущей папке папка с файлом .env или папкой .git (или ATLASSIAN_ROOT).
Пути из настроек считаются от корня. Переменные окружения имеют приоритет над .env.
"""
import os
import re
import sys


def find_root():
    if os.environ.get("ATLASSIAN_ROOT"):
        return os.path.abspath(os.environ["ATLASSIAN_ROOT"])
    start = d = os.getcwd()
    while True:
        if os.path.isfile(os.path.join(d, ".env")) or os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return start
        d = parent


ROOT = find_root()


def load_env():
    """Подхватывает переменные из .env в корне проекта; заданные в окружении имеют приоритет."""
    path = os.path.join(ROOT, ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m and not line.lstrip().startswith("#"):
                value = m.group(2).strip("\"'")
                if os.environ.get(m.group(1), value) != value:
                    print(f"Внимание: {m.group(1)} задана и в терминале, и в .env — используется значение "
                          f"из терминала. Чтобы работал .env, выполните: unset {m.group(1)}", file=sys.stderr)
                os.environ.setdefault(m.group(1), value)


load_env()


def get(name, default=None):
    return os.environ.get(name) or default


def ids(name):
    """Список идентификаторов страниц из переменной через запятую или пробел."""
    return [x for x in re.split(r"[\s,;]+", os.environ.get(name, "")) if x]


def folder(name, default):
    """Абсолютный путь папки из настройки (путь в настройке — от корня проекта)."""
    return os.path.normpath(os.path.join(ROOT, get(name, default)))


def rel(path):
    """Путь от корня проекта — для сообщений."""
    return os.path.relpath(path, ROOT)
