"""The command line: arguments, the busy-port message, start-up warnings, and a real boot of the server."""
import json
import os
import socket
import subprocess
import sys
import time
import types
import urllib.request
from pathlib import Path

import pytest
from fastapi import FastAPI

from reelcaptioner import __main__ as cli
from reelcaptioner import media
from reelcaptioner.config import PROJECT_DIR

from .helpers import needs_ffmpeg


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """main() writes HRC_* variables into os.environ. Nothing of that may leak into other tests."""
    saved = dict(os.environ)
    for name in [n for n in os.environ if n.startswith("HRC_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HRC_DATA_DIR", str(tmp_path / "data"))
    yield
    os.environ.clear()
    os.environ.update(saved)


class FakeTimer:
    def __init__(self, delay, fn):
        self.delay, self.fn = delay, fn

    def start(self):
        FakeTimer.started.append(self)


@pytest.fixture
def timers(monkeypatch):
    FakeTimer.started = []
    monkeypatch.setattr(cli, "threading", types.SimpleNamespace(Timer=FakeTimer))
    return FakeTimer.started


@pytest.fixture
def served(monkeypatch):
    """uvicorn.run replaced, so main() "starts" without opening a port. Collects (app, kwargs)."""
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs)))
    return calls


@pytest.fixture
def full_toolchain(monkeypatch):
    monkeypatch.setattr(media, "tools_status", lambda: {"ffmpeg": True, "ffprobe": True, "libass": True, "version": "x"})


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --------------------------------------------------------------------------- small helpers
def test_port_in_use_sees_a_listening_socket_and_a_free_port():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        assert cli._port_in_use("127.0.0.1", busy.getsockname()[1]) is True
    assert cli._port_in_use("127.0.0.1", free_port()) is False


def test_port_in_use_stays_quiet_about_other_socket_errors():
    assert cli._port_in_use("192.0.2.1", 8000) is False          # TEST-NET-1: not an address of this machine


@pytest.mark.parametrize("host, expected", [
    ("127.0.0.1", "http://127.0.0.1:8000"), ("localhost", "http://localhost:8000"),
    ("0.0.0.0", "http://127.0.0.1:8000"), ("::", "http://127.0.0.1:8000"), ("", "http://127.0.0.1:8000"),
    ("::1", "http://[::1]:8000"), ("192.168.1.20", "http://192.168.1.20:8000"),
])
def test_the_url_people_are_told_to_open(host, expected):
    assert cli._url(host, 8000) == expected


@pytest.mark.parametrize("value, message", [("99999", "between 1 and 65535"), ("0", "between 1 and 65535"),
                                            ("abc", "not a number")])
def test_a_bad_port_argument_is_rejected_politely(capsys, value, message):
    with pytest.raises(SystemExit) as stop:
        cli.main(["--port", value])
    assert stop.value.code == 2
    assert message in capsys.readouterr().err


# --------------------------------------------------------------------------- starting up
def test_the_arguments_reach_the_server(served, timers, full_toolchain, capsys, tmp_path):
    port = free_port()
    data = tmp_path / "elsewhere"
    code = cli.main(["--host", "127.0.0.1", "--port", str(port), "--model", "tiny", "--data-dir", str(data),
                     "--no-browser"])
    assert code == 0
    ((app, kwargs),) = served
    assert isinstance(app, FastAPI)
    assert kwargs["host"] == "127.0.0.1" and kwargs["port"] == port
    assert (os.environ["HRC_MODEL"], os.environ["HRC_PORT"], os.environ["HRC_DATA_DIR"]) == ("tiny", str(port), str(data))
    assert data.is_dir()
    out = capsys.readouterr().out
    assert f"running at http://127.0.0.1:{port}" in out and "tiny" in out and str(data) in out
    assert not timers


def test_demo_mode_needs_no_model_and_plays_the_sample_script(served, full_toolchain, capsys):
    assert cli.main(["--demo", "--no-browser", "--port", str(free_port())]) == 0
    assert os.environ["HRC_ASR_BACKEND"] == "mock" and os.environ["HRC_MOCK_DELAY"] == "1.5"
    assert "demo mode" in capsys.readouterr().out


def test_demo_mode_respects_a_delay_chosen_by_the_user(served, full_toolchain, monkeypatch):
    monkeypatch.setenv("HRC_MOCK_DELAY", "0")
    cli.main(["--demo", "--no-browser", "--port", str(free_port())])
    assert os.environ["HRC_MOCK_DELAY"] == "0"


def test_the_browser_opens_by_itself_unless_told_not_to(served, timers, full_toolchain, monkeypatch):
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    port = free_port()
    assert cli.main(["--port", str(port)]) == 0
    (timer,) = timers
    assert timer.delay == pytest.approx(1.2)
    timer.fn()
    assert opened == [f"http://127.0.0.1:{port}"]
    timers.clear()
    cli.main(["--port", str(port), "--no-browser"])
    assert not timers


def test_a_busy_port_gets_a_helpful_message_and_nothing_starts(served, full_toolchain, capsys):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert cli.main(["--port", str(port), "--no-browser"]) == 1
    err = capsys.readouterr().err
    assert "already in use" in err and f"http://127.0.0.1:{port}" in err and f"--port {port + 1}" in err
    assert not served


def test_a_server_that_cannot_bind_is_reported(monkeypatch, full_toolchain, capsys):
    def refuse(app, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("uvicorn.run", refuse)
    assert cli.main(["--no-browser", "--port", str(free_port())]) == 1
    assert "Could not start the server" in capsys.readouterr().err


def test_bad_settings_are_explained_not_raised(served, full_toolchain, capsys, monkeypatch):
    monkeypatch.setenv("HRC_MAX_UPLOAD_MB", "lots")
    assert cli.main(["--no-browser"]) == 2
    assert "HRC_MAX_UPLOAD_MB" in capsys.readouterr().err
    assert not served


# --------------------------------------------------------------------------- warnings
def test_a_missing_ffmpeg_is_called_out_but_does_not_stop_the_app(served, monkeypatch, capsys):
    monkeypatch.setattr(media, "tools_status", lambda: {"ffmpeg": False, "ffprobe": False, "libass": False, "version": ""})
    assert cli.main(["--no-browser", "--port", str(free_port())]) == 0
    assert "FFmpeg was not found" in capsys.readouterr().err and served


def test_ffmpeg_without_libass_says_what_will_not_work(served, monkeypatch, capsys):
    monkeypatch.setattr(media, "tools_status", lambda: {"ffmpeg": True, "ffprobe": True, "libass": False, "version": "x"})
    assert cli.main(["--no-browser", "--port", str(free_port())]) == 0
    err = capsys.readouterr().err
    assert "libass" in err and "burning captions will not" in err


def test_a_healthy_local_start_prints_no_warnings(served, full_toolchain, capsys):
    assert cli.main(["--no-browser", "--port", str(free_port())]) == 0
    assert capsys.readouterr().err == ""


def test_listening_beyond_this_computer_comes_with_a_warning(served, full_toolchain, capsys):
    port = free_port()
    assert cli.main(["--host", "0.0.0.0", "--port", str(port), "--no-browser"]) == 0
    captured = capsys.readouterr()
    assert "no password" in captured.err and "0.0.0.0" in captured.err
    assert f"running at http://127.0.0.1:{port}" in captured.out          # a wildcard address is not a URL to open
    assert served[0][1]["host"] == "0.0.0.0"


# --------------------------------------------------------------------------- as a real program
def run_module(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(PROJECT_DIR), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-m", "reelcaptioner", *args], cwd=PROJECT_DIR, env=env,
                          capture_output=True, text=True, timeout=timeout)


def test_python_dash_m_shows_help():
    done = run_module("--help")
    assert done.returncode == 0
    assert all(flag in done.stdout for flag in ("--demo", "--no-browser", "--port", "--model", "--data-dir", "--host"))


def test_python_dash_m_rejects_a_bad_port():
    done = run_module("--port", "99999")
    assert done.returncode == 2 and "between 1 and 65535" in done.stderr


@needs_ffmpeg
def test_the_demo_really_boots_and_serves_the_app(tmp_path):
    port = free_port()
    env = dict(os.environ, PYTHONPATH=str(PROJECT_DIR), PYTHONIOENCODING="utf-8")
    log = tmp_path / "server.log"
    with log.open("w") as sink:
        proc = subprocess.Popen([sys.executable, "-m", "reelcaptioner", "--demo", "--no-browser", "--port", str(port),
                                 "--data-dir", str(tmp_path / "data")], cwd=PROJECT_DIR, env=env, stdout=sink, stderr=sink)
    try:
        status, deadline = None, time.time() + 30
        while time.time() < deadline and status is None:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as resp:
                    status = json.load(resp)
            except OSError:
                time.sleep(0.2)
        assert status is not None, f"the server did not come up:\n{log.read_text()}"
        assert status["demo"] is True and status["tools"]["ffmpeg"] is True
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as page:
            assert b"Hinglish Reel Captioner" in page.read()
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    assert Path(tmp_path / "data").is_dir()
