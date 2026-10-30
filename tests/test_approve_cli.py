"""Owner approval CLI parsing and loopback transport (no hub dependencies)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class CliApprovalTests(unittest.TestCase):
    def parse(self, *args):
        from drover.cli import parser
        return parser().parse_args(["approve", *args])

    def test_top_level_dispatch_preserves_all_arguments_and_exit_status(self):
        from drover.__main__ import main
        argv = ["approve", "T-1", "T-2", "--epic", "E-1", "--filter", "repo=r,kind=fix",
                "--from-file", "ids.txt", "--explicit", "--reason", "reviewed"]
        with patch("drover.cli.main", return_value=2) as approve_main:
            self.assertEqual(main(iter(argv)), 2)
        approve_main.assert_called_once_with(argv)

    def test_top_level_approve_executes_owner_client(self):
        import contextlib
        import io
        from drover.__main__ import main
        with patch("drover.cli.OwnerClient") as client, contextlib.redirect_stdout(io.StringIO()) as output:
            client.return_value.request.return_value = {"results": [{"id": "T-1", "result": "approved"}]}
            with patch("sys.argv", ["drover", "approve", "T-1", "T-2", "--explicit"]):
                self.assertEqual(main(), 0)
        client.return_value.request.assert_called_once_with(
            "/tasks/approve", {"ids": ["T-1", "T-2"], "reason": None, "explicit": True})
        self.assertEqual(json.loads(output.getvalue())["results"][0]["result"], "approved")

    def test_top_level_and_approve_help_document_selection(self):
        import contextlib
        import io
        from drover.__main__ import main
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main(["--help"]), 0)
        self.assertIn("drover approve", output.getvalue())
        self.assertIn("--explicit", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as exit:
            main(["approve", "--help"])
        self.assertEqual(exit.exception.code, 0)
        for flag in ("--epic", "--filter", "--from-file", "--explicit", "--reason"):
            self.assertIn(flag, output.getvalue())

    def test_parser_supports_all_selection_modes(self):
        a = self.parse("T-1", "T-2", "--epic", "E-1", "--filter", "repo=r,kind=fix", "--filter", "priority=P1",
                       "--from-file", "ids.txt", "--explicit", "--reason", "reviewed")
        self.assertEqual(a.ids, ["T-1", "T-2"])
        self.assertEqual(a.epic, "E-1")
        self.assertEqual(a.filter, [{"repo": "r", "kind": "fix"}, {"priority": "P1"}])
        self.assertTrue(a.explicit)
        self.assertEqual(a.reason, "reviewed")
        self.assertEqual(a.from_file, [Path("ids.txt")])
        import contextlib, io
        for argv in (("--filter", "bad=x"), ("--filter", "repo="), ("--filter", "P1")):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.parse(*argv)

    def test_selector_excludes_explicit_tasks_even_with_explicit_switch(self):
        from drover.cli import approve
        class Client:
            def __init__(self): self.calls = []
            def request(self, path, body=None):
                self.calls.append((path, body))
                if body is None:
                    return [dict(id=tid, status="proposed", epic_id="E-1", repo="r", kind="fix", priority="P1",
                                 requires_explicit_approval=explicit)
                            for tid, explicit in (("T-1", False), ("T-2", True), ("T-3", True))]
                return {"results": [{"id": tid, "result": "approved"} for tid in body["ids"]]}
        client = Client()
        result = approve(self.parse("T-2", "--epic", "E-1", "--filter", "repo=r,priority=P1", "--explicit"), client)
        self.assertEqual(client.calls[1][1]["ids"], ["T-1"])
        self.assertNotIn("explicit", client.calls[1][1])
        self.assertEqual(client.calls[2][1]["ids"], ["T-2"])
        self.assertTrue(client.calls[2][1]["explicit"])
        self.assertEqual(result["results"][-1]["id"], "T-3")
        self.assertEqual(result["results"][-1]["result"], "skipped")

    def test_file_inputs_and_deduplication(self):
        from drover.cli import approve
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "plain", Path(tmp) / "json"
            a.write_text("T-1\nT-2, T-1,\n", encoding="utf-8-sig")
            b.write_text('["T-2", "T-3"]')
            client = Mock()
            client.request.return_value = {"results": []}
            approve(self.parse("T-1", "--from-file", str(a), "--from-file", str(b)), client)
            self.assertEqual(client.request.call_args.args[1]["ids"], ["T-1", "T-2", "T-3"])
            self.assertFalse(client.request.call_args.args[1]["explicit"])
            b.write_text('[3]')
            with self.assertRaises(ValueError):
                approve(self.parse("--from-file", str(b)), client)

    def test_invalid_or_empty_selection_never_posts(self):
        from drover.cli import approve
        from unittest.mock import Mock
        for argv in ((), ("--epic", "E-1", "--explicit"), ("--filter", "repo=a", "--filter", "repo=b")):
            client = Mock()
            with self.assertRaises(ValueError):
                approve(self.parse(*argv), client)
            client.request.assert_not_called()
        client = Mock()
        client.request.return_value = []
        self.assertEqual(approve(self.parse("--epic", "E-empty"), client), {"results": []})
        self.assertEqual(client.request.call_count, 1)

    def test_loopback_owner_transport_has_no_credentials_proxy_or_redirect(self):
        from drover.cli import OwnerClient, NoRedirects
        from unittest.mock import Mock
        import io
        with patch("drover.cli.config.get", return_value={"hub": {"ui_port": 8765}}), patch("drover.cli.build_opener") as opener:
            response = io.BytesIO(b'{"results": []}')
            opener.return_value.open.return_value.__enter__.return_value = response
            client = OwnerClient()
            self.assertEqual(client.request("/tasks/approve", {"ids": ["T-1"]}), {"results": []})
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/v1/tasks/approve")
            self.assertEqual(request.get_header("Origin"), "http://127.0.0.1:8765")
            self.assertIsNone(request.get_header("Authorization"))
            self.assertEqual(json.loads(request.data), {"ids": ["T-1"]})
            self.assertEqual(opener.call_args.args[0].proxies, {})
            self.assertIsInstance(opener.call_args.args[1], NoRedirects)
            self.assertIsNone(opener.call_args.args[1].redirect_request(Mock(), None, 302, "", {}, "https://remote/"))

    def test_main_prints_per_id_results_and_reports_usage_errors(self):
        import contextlib
        import io
        from drover.cli import main
        with patch("drover.cli.OwnerClient") as client:
            client.return_value.request.return_value = {"results": [{"id": "T-1", "result": "approved"}]}
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["approve", "T-1"]), 0)
            self.assertEqual(json.loads(output.getvalue())["results"][0]["id"], "T-1")
            client.return_value.request.reset_mock()
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(main(["approve"]), 2)
            self.assertIn("provide task IDs", error.getvalue())
            client.return_value.request.assert_not_called()
