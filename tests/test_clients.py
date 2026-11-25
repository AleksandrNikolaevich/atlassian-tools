"""Проверки клиентов без доступа к серверам."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from atlassian_tools.confluence import ConfluenceClient
from atlassian_tools.jira import JiraClient


class JsonResponse(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, body):
        super().__init__(json.dumps(body).encode())


class AtlassianClientTests(unittest.TestCase):
    def test_jira_search_uses_explicit_connection_and_fetches_all_pages(self):
        def respond(request, **kwargs):
            url = urlsplit(request.full_url)
            self.assertEqual(url.netloc, "jira.example.test")
            self.assertEqual(url.path, "/jira/rest/api/2/search")
            self.assertEqual(request.get_header("Authorization"), "Bearer explicit")
            self.assertEqual(request.get_header("Accept"), "application/json")
            query = parse_qs(url.query)
            self.assertEqual(query["jql"], ["project = DEMO"])
            self.assertEqual(query["fields"], ["summary"])
            issue = {"0": "DEMO-1", "1": "DEMO-2"}[query["startAt"][0]]
            return JsonResponse({"issues": [{"key": issue}], "total": 2})

        with patch.dict(os.environ, {"JIRA_URL": "https://other.example.test", "JIRA_TOKEN": "wrong"}, clear=True):
            with patch("urllib.request.urlopen", side_effect=respond):
                jira = JiraClient("https://jira.example.test/jira/", token="explicit")
                issues = jira.search("project = DEMO", "summary")

        self.assertEqual(issues, [{"key": "DEMO-1"}, {"key": "DEMO-2"}])

    def test_confluence_walks_tree_with_only_explicit_exclusions(self):
        def respond(request, **kwargs):
            url = urlsplit(request.full_url)
            self.assertEqual(url.netloc, "wiki.example.test")
            self.assertEqual(url.path, "/wiki/rest/api/content/search")
            self.assertEqual(request.get_header("Cookie"), "JSESSIONID=explicit")
            query = parse_qs(url.query)
            pages = {
                "type = page and (parent = 100)": [
                    {"id": "850517994", "title": "Ordinary page", "ancestors": [{"id": "100"}]},
                    {"id": "200", "title": "Skip this page", "ancestors": [{"id": "100"}]},
                ],
                "type = page and (parent = 850517994)": [
                    {"id": "300", "title": "Child page", "ancestors": [{"id": "850517994"}]},
                ],
                "type = page and (parent = 300)": [],
            }[query["cql"][0]]
            return JsonResponse({"results": pages, "limit": 100})

        with patch("urllib.request.urlopen", side_effect=respond):
            wiki = ConfluenceClient("https://wiki.example.test/wiki/", cookie="explicit")
            pages, rejected = wiki.descendants_new([], ["100"], ignored={"200"})

        self.assertEqual(set(pages), {"850517994", "300"})
        self.assertEqual(pages["300"]["parent"], "850517994")
        self.assertEqual(rejected, [])

    def test_client_import_does_not_load_project_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text("ATLASSIAN_TEST_SETTING=unexpected\n")
            env = dict(os.environ, ATLASSIAN_ROOT=directory)
            env.pop("ATLASSIAN_TEST_SETTING", None)
            result = subprocess.run(
                [sys.executable, "-c",
                 "from atlassian_tools.confluence import ConfluenceClient; "
                 "from atlassian_tools.jira import JiraClient; "
                 "import os; print(os.environ.get('ATLASSIAN_TEST_SETTING'))"],
                env=env, text=True, capture_output=True, check=True,
            )
        self.assertEqual(result.stdout.strip(), "None")
