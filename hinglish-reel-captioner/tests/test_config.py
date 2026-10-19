"""Settings come from HRC_* environment variables. Bad values must produce a sentence, not a traceback."""
import pytest

from reelcaptioner import config
from reelcaptioner.config import ConfigError, get_settings

NAMES = ["HRC_DATA_DIR", "HRC_ASR_BACKEND", "HRC_MODEL", "HRC_DEVICE", "HRC_COMPUTE_TYPE", "HRC_CPU_THREADS",
         "HRC_MAX_UPLOAD_MB", "HRC_OLLAMA_URL", "HRC_OLLAMA_MODEL", "HRC_HOST", "HRC_PORT"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HRC_DATA_DIR", str(tmp_path / "data"))


def test_the_defaults_suit_a_laptop(tmp_path):
    s = get_settings()
    assert (s.asr_backend, s.model, s.device, s.compute_type) == ("faster-whisper", "small", "auto", "auto")
    assert (s.cpu_threads, s.max_upload_mb, s.host, s.port) == (0, 2048, "127.0.0.1", 8000)
    assert s.ollama_url == "http://127.0.0.1:11434" and s.ollama_model == ""
    assert s.data_dir == (tmp_path / "data").resolve() and s.data_dir.is_dir()      # created on demand


def test_every_setting_can_be_overridden(monkeypatch):
    for name, value in {"HRC_ASR_BACKEND": "mock", "HRC_MODEL": "large-v3-turbo", "HRC_DEVICE": "cuda",
                        "HRC_COMPUTE_TYPE": "float16", "HRC_CPU_THREADS": "6", "HRC_MAX_UPLOAD_MB": "300",
                        "HRC_OLLAMA_URL": "http://box:11434", "HRC_OLLAMA_MODEL": "gemma3:4b",
                        "HRC_HOST": "0.0.0.0", "HRC_PORT": "9001"}.items():
        monkeypatch.setenv(name, value)
    s = get_settings()
    assert (s.asr_backend, s.model, s.device, s.compute_type) == ("mock", "large-v3-turbo", "cuda", "float16")
    assert (s.cpu_threads, s.max_upload_mb, s.host, s.port) == (6, 300, "0.0.0.0", 9001)
    assert (s.ollama_url, s.ollama_model) == ("http://box:11434", "gemma3:4b")


def test_values_are_tidied_up(monkeypatch):
    monkeypatch.setenv("HRC_ASR_BACKEND", "  Mock ")
    monkeypatch.setenv("HRC_DEVICE", "CUDA")
    monkeypatch.setenv("HRC_COMPUTE_TYPE", "INT8")
    monkeypatch.setenv("HRC_OLLAMA_URL", "http://localhost:11434/")
    monkeypatch.setenv("HRC_OLLAMA_MODEL", "  llama3.2:3b ")
    s = get_settings()
    assert (s.asr_backend, s.device, s.compute_type) == ("mock", "cuda", "int8")
    assert s.ollama_url == "http://localhost:11434" and s.ollama_model == "llama3.2:3b"


def test_blank_values_mean_use_the_default(monkeypatch):
    for name in ("HRC_MODEL", "HRC_PORT", "HRC_HOST", "HRC_CPU_THREADS", "HRC_MAX_UPLOAD_MB"):
        monkeypatch.setenv(name, "  ")
    s = get_settings()
    assert (s.model, s.port, s.host, s.cpu_threads, s.max_upload_mb) == ("small", 8000, "127.0.0.1", 0, 2048)


def test_the_data_folder_defaults_to_one_beside_the_code(monkeypatch, tmp_path):
    monkeypatch.delenv("HRC_DATA_DIR")
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
    assert get_settings().data_dir == (tmp_path / "data").resolve()


def test_a_tilde_in_the_data_folder_means_the_home_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HRC_DATA_DIR", "~/captions")
    assert get_settings().data_dir == (tmp_path / "captions").resolve()


@pytest.mark.parametrize("name, value", [
    ("HRC_PORT", "abc"), ("HRC_PORT", "80.5"), ("HRC_PORT", "0"), ("HRC_PORT", "65536"), ("HRC_PORT", "-1"),
    ("HRC_MAX_UPLOAD_MB", "lots"), ("HRC_MAX_UPLOAD_MB", "0"),
    ("HRC_CPU_THREADS", "-1"), ("HRC_CPU_THREADS", "many"),
])
def test_unusable_numbers_are_explained(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        get_settings()


def test_the_edges_of_the_valid_range_are_accepted(monkeypatch):
    monkeypatch.setenv("HRC_PORT", "65535")
    monkeypatch.setenv("HRC_MAX_UPLOAD_MB", "1")
    monkeypatch.setenv("HRC_CPU_THREADS", "0")
    s = get_settings()
    assert (s.port, s.max_upload_mb, s.cpu_threads) == (65535, 1, 0)


def test_a_data_folder_that_cannot_be_created_is_explained(monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("a file, so nothing can be created inside it")
    monkeypatch.setenv("HRC_DATA_DIR", str(blocker / "data"))
    with pytest.raises(ConfigError, match="--data-dir"):
        get_settings()
