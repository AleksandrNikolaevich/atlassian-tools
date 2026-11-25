import argparse
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from atlassian_tools import jira_sync


class JiraSyncTests(unittest.TestCase):
    def test_release_pull_writes_nested_issues_and_relative_links(self):
        version = {"id": "1", "name": "Release 1"}
        url = "https://jira.example.test/secure/attachment/10/screenshot.png"
        child = {"key": "TASK-2", "fields": {"updated": "today"}}
        parent = {"key": "TASK-1", "fields": {
            "updated": "today", "fixVersions": [version], "subtasks": [child],
            "attachment": [{"id": "10", "filename": "screenshot.png", "content": url,
                            "size": 5, "mimeType": "image/png"}],
        }, "renderedFields": {"description": f'<img src="{url}"> <a href="/browse/TASK-2">child</a>'}}
        child["fields"]["parent"] = {"key": "TASK-1", "fields": {}}
        jira = Mock(base="https://jira.example.test")
        jira.request.return_value = [version]
        jira.search.return_value = [parent, child]
        jira.download.side_effect = lambda url, path: Path(path).write_bytes(b"image")
        args = argparse.Namespace(keys=[], released=5, match=None, version=[], force=False, no_attachments=False)

        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            root = Path(directory)
            with patch.multiple(jira_sync, OUT=directory, STATE=root / ".state.json"), \
                    patch.object(jira_sync.config, "get", return_value="TASK"):
                jira_sync.cmd_pull(jira, args)
                jira_sync.cmd_pull(jira, args)

            self.assertEqual(jira.download.call_count, 1)
            self.assertEqual(jira.search.call_count, 3)
            issue_dir = root / "issues" / "TASK-1"
            self.assertEqual((issue_dir / "attachments/10-screenshot.png").read_bytes(), b"image")
            markdown = (issue_dir / "TASK-1.md").read_text(encoding="utf-8")
            self.assertIn("![screenshot.png](attachments/10-screenshot.png)", markdown)
            self.assertIn("[child](../TASK-2/TASK-2.md)", markdown)
            self.assertIn("[Все релизы](../../README.md)", markdown)
            self.assertIn("[Release 1](../../releases/Release-1.md)", markdown)
            self.assertIn("[TASK-1](../issues/TASK-1/TASK-1.md)",
                          (root / "releases/Release-1.md").read_text(encoding="utf-8"))

    def test_partial_pull_with_no_attachments_uses_files_in_nested_layout(self):
        jira = Mock(base="https://jira.example.test")
        issue = {"key": "TASK-1", "fields": {"updated": "today", "attachment": [
            {"id": "10", "filename": "file.txt", "content": "https://jira.example.test/file.txt", "size": 5},
            {"id": "11", "filename": "remote.txt", "content": "https://jira.example.test/remote.txt", "size": 5},
        ]}, "renderedFields": {"description": '<a href="/browse/TASK-2">other issue</a>'}}
        jira.search.return_value = [issue]
        args = argparse.Namespace(keys=["TASK-1"], version=[], force=False, no_attachments=True)

        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            root = Path(directory)
            issue_dir = root / "issues/TASK-1"
            cache = issue_dir / "attachments/10-file.txt"
            cache.parent.mkdir(parents=True)
            cache.write_bytes(b"cache")
            other_issue = root / "issues/TASK-2/TASK-2.md"
            other_issue.parent.mkdir()
            other_issue.touch()
            with patch.multiple(jira_sync, OUT=directory, STATE=root / ".state.json"):
                jira_sync.save_state({"issues": {"TASK-1": {"updated": "today"}}})
                jira_sync.cmd_pull(jira, args)

            jira.download.assert_not_called()
            self.assertEqual(cache.read_bytes(), b"cache")
            markdown = (issue_dir / "TASK-1.md").read_text(encoding="utf-8")
            self.assertIn("[file.txt](attachments/10-file.txt)", markdown)
            self.assertIn("[remote.txt](https://jira.example.test/remote.txt)", markdown)
            self.assertIn("[other issue](../TASK-2/TASK-2.md)", markdown)
            self.assertIn("[TASK-1](issues/TASK-1/TASK-1.md)", (root / "README.md").read_text(encoding="utf-8"))
