import contextlib
import csv
import io
import json
import os
import shutil
import socket
import tempfile
import unittest
from unittest import mock

from tests.mock_llm import MockLLM
from wontdo import cli
from wontdo.store import Store

LIST = """Pay electricity bill by tomorrow
Learn Rust someday
Waiting for the landlord to fix the tap
Reply to the webinar invite
Book dentist for a check-up
"""


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wontdo-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "data")
        self.file = os.path.join(self.tmp, "tasks.txt")
        with open(self.file, "w", encoding="utf-8") as handle:
            handle.write(LIST)

    def run_cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is None:
                code = cli.main(list(argv))
            else:
                with mock.patch("sys.stdin", io.StringIO(stdin)):
                    code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def history_files(self):
        folder = os.path.join(self.data, "history")
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


class SortWithoutAModel(CliCase):
    def test_markdown_is_the_default(self):
        code, out, err = self.run_cli("sort", self.file, "--demo", "--data-dir", self.data)
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("# The Won't-Do List"))
        for heading in ("## Do now", "## Let it expire", "## Not my problem"):
            self.assertIn(heading, out)
        self.assertIn("keyword rules", out)

    def test_other_formats(self):
        code, out, _ = self.run_cli("sort", self.file, "--demo", "--format", "json", "--data-dir", self.data)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["engine"]["kind"], "demo")
        _, out, _ = self.run_cli("sort", self.file, "--demo", "--format", "csv", "--data-dir", self.data)
        self.assertEqual(next(csv.reader(io.StringIO(out)))[:2], ["bucket", "task"])
        _, out, _ = self.run_cli("sort", self.file, "--demo", "--format", "ics", "--data-dir", self.data)
        self.assertTrue(out.startswith("BEGIN:VCALENDAR"))

    def test_budget(self):
        with open(self.file, "w") as handle:
            handle.write("\n".join("Reply to Anita about item %d" % n for n in range(1, 6)))
        _, out, _ = self.run_cli("sort", self.file, "--demo", "--budget", "10", "--format", "json", "--data-dir", self.data)
        self.assertEqual(json.loads(out)["stats"]["counts"]["now"], 2)
        _, out, _ = self.run_cli("sort", self.file, "--demo", "--budget", "0", "--format", "json", "--data-dir", self.data)
        self.assertEqual(json.loads(out)["stats"]["counts"]["now"], 5)

    def test_reads_stdin(self):
        code, out, _ = self.run_cli("sort", "-", "--demo", "--data-dir", self.data, stdin="Pay rent by tomorrow\nLearn Rust someday")
        self.assertEqual(code, 0)
        self.assertIn("Pay rent by tomorrow", out)
        code, out, _ = self.run_cli("sort", "--demo", "--data-dir", self.data, stdin="Buy milk")  # "-" is the default
        self.assertEqual(code, 0)
        self.assertIn("Buy milk", out)

    def test_save_keeps_the_result_in_history_and_nothing_else_does(self):
        self.run_cli("sort", self.file, "--demo", "--data-dir", self.data)
        self.assertEqual(self.history_files(), [])
        self.run_cli("sort", self.file, "--demo", "--save", "--data-dir", self.data)
        self.assertEqual(len(self.history_files()), 1)
        self.assertEqual(len(Store(self.data).list_history()), 1)

    def test_a_windows_style_file_with_a_bom_and_crlf(self):
        with open(self.file, "wb") as handle:
            handle.write("﻿Pay rent by tomorrow\r\nLearn Rust someday\r\n".encode("utf-8"))
        code, out, _ = self.run_cli("sort", self.file, "--demo", "--format", "json", "--data-dir", self.data)
        self.assertEqual(code, 0)
        self.assertEqual([t["text"] for t in json.loads(out)["tasks"]], ["Pay rent by tomorrow", "Learn Rust someday"])

    def test_problems_are_explained_and_exit_1(self):
        code, _, err = self.run_cli("sort", os.path.join(self.tmp, "missing.txt"), "--demo", "--data-dir", self.data)
        self.assertEqual(code, 1)
        self.assertIn("file not found", err)

        with open(self.file, "w") as handle:
            handle.write("  \n\n")
        code, out, err = self.run_cli("sort", self.file, "--demo", "--data-dir", self.data)
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("Error:"))

        with open(self.file, "wb") as handle:
            handle.write(b"\xff\xfe\x00bad bytes \x80\x81")
        code, _, err = self.run_cli("sort", self.file, "--demo", "--data-dir", self.data)
        self.assertEqual(code, 1)
        self.assertIn("UTF-8", err)


class SortWithAModel(CliCase):
    def setUp(self):
        super().setUp()
        self.mock = MockLLM().start()
        self.addCleanup(self.mock.stop)

    def test_uses_the_model_and_reports_progress(self):
        code, out, err = self.run_cli("sort", self.file, "--base-url", self.mock.url, "--data-dir", self.data)
        self.assertEqual(code, 0, err)
        self.assertIn("Sorted by gemma3:4b via ollama.", out)
        self.assertIn("(5/5)", err)
        self.assertEqual(len(self.mock.calls), 1)

    def test_model_flag(self):
        self.mock.models.append({"name": "qwen2.5:3b", "details": {"parameter_size": "3B"}})
        code, out, _ = self.run_cli("sort", self.file, "--base-url", self.mock.url, "--model", "qwen2.5:3b", "--data-dir", self.data)
        self.assertEqual(code, 0)
        self.assertEqual(self.mock.calls[0][1]["model"], "qwen2.5:3b")
        self.assertIn("qwen2.5:3b", out)

    def test_no_server_means_exit_2_and_a_way_out(self):
        code, out, err = self.run_cli("sort", self.file, "--base-url", "http://127.0.0.1:%d" % free_port(), "--data-dir", self.data)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("No usable model", err)
        self.assertIn("--demo", err)

    def test_missing_model_means_exit_2(self):
        code, _, err = self.run_cli("sort", self.file, "--base-url", self.mock.url, "--model", "ghost:7b", "--data-dir", self.data)
        self.assertEqual(code, 2)
        self.assertIn("ollama pull ghost:7b", err)

    def test_a_flaky_model_falls_back_and_says_so(self):
        self.mock.mode = "garbage"
        code, out, err = self.run_cli("sort", self.file, "--base-url", self.mock.url, "--data-dir", self.data)
        self.assertEqual(code, 0)
        self.assertIn("Do now", out)
        self.assertIn("Note:", err)

    def test_fallback_can_be_turned_off_in_settings(self):
        Store(self.data).update_settings({"demo_fallback": False})
        self.mock.mode = "garbage"
        code, out, err = self.run_cli("sort", self.file, "--base-url", self.mock.url, "--data-dir", self.data)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Error:", err)

    def test_saved_settings_are_used(self):
        Store(self.data).update_settings({"base_url": self.mock.url, "chunk_size": 2})
        code, _, _ = self.run_cli("sort", self.file, "--data-dir", self.data)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.mock.calls), 3)


class Doctor(CliCase):
    def setUp(self):
        super().setUp()
        self.mock = MockLLM().start()
        self.addCleanup(self.mock.stop)

    def test_healthy(self):
        code, out, _ = self.run_cli("doctor", "--base-url", self.mock.url, "--data-dir", self.data)
        self.assertEqual(code, 0)
        self.assertIn("All good.", out)
        self.assertEqual(out.count("✓"), 5)

    def test_broken_model(self):
        self.mock.mode = "garbage"
        code, out, _ = self.run_cli("doctor", "--base-url", self.mock.url, "--data-dir", self.data)
        self.assertEqual(code, 1)
        self.assertIn("Raw reply from the model", out)
        self.assertIn("✗", out)
        self.assertIn("needs fixing", out)

    def test_nothing_running(self):
        code, out, _ = self.run_cli("doctor", "--base-url", "http://127.0.0.1:%d" % free_port(), "--data-dir", self.data)
        self.assertEqual(code, 1)
        self.assertIn("✗ Find a model server", out)

    def test_warning_when_the_model_wants_to_skip_the_bill(self):
        self.mock.mode = "all_expire"
        code, out, _ = self.run_cli("doctor", "--base-url", self.mock.url, "--data-dir", self.data)
        self.assertEqual(code, 0)
        self.assertIn("! ", out)


class Entry(CliCase):
    def test_serve_is_the_default_command(self):
        with mock.patch("wontdo.cli.serve", return_value=0) as serve:
            self.assertEqual(cli.main([]), 0)
            serve.assert_called_once_with("127.0.0.1", 8742, cli.DEFAULT_DATA, open_browser=False)

    def test_serve_flags_without_the_word_serve(self):
        with mock.patch("wontdo.cli.serve", return_value=0) as serve:
            cli.main(["--port", "9100", "--open", "--data-dir", self.data])
            serve.assert_called_once_with("127.0.0.1", 9100, self.data, open_browser=True)

    def test_ctrl_c_exits_130(self):
        with mock.patch("wontdo.cli.serve", side_effect=KeyboardInterrupt):
            self.assertEqual(cli.main(["serve"]), 130)

    def test_version_and_help(self):
        for flag in ("--version", "-h"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
                cli.main([flag])
            self.assertEqual(ctx.exception.code, 0)
            self.assertIn("wontdo", out.getvalue())

    def test_bad_arguments_exit_2(self):
        for argv in (["sort", "--format", "pdf"], ["frobnicate"], ["sort", "--budget", "lots"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                cli.main(argv)
            self.assertEqual(ctx.exception.code, 2, argv)

    def test_python_dash_m_works(self):
        import subprocess
        import sys

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        done = subprocess.run(
            [sys.executable, "-m", "wontdo", "sort", "--demo", "--data-dir", self.data],
            input="Pay rent by tomorrow\nLearn Rust someday\n", capture_output=True, text=True, cwd=root, timeout=60, check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("## Let it expire", done.stdout)


if __name__ == "__main__":
    unittest.main()
