import socket
import unittest
import urllib.error
from unittest import mock

from tests.mock_llm import DEFAULT_MODELS, MockLLM
from wontdo.doctor import run_doctor
from wontdo.llm import (
    LLMError,
    ModelInfo,
    OllamaClient,
    OpenAIClient,
    detect_backend,
    extract_rows,
    make_client,
)
from wontdo.prompts import VERDICT_SCHEMA
from wontdo.store import DEFAULTS

SYSTEM = "system text"
USER = "Tasks:\n1. Pay rent\n2. Learn Rust someday\n"


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def settings(**overrides):
    base = dict(DEFAULTS)
    base.update(overrides)
    return base


class MockedTest(unittest.TestCase):
    def setUp(self):
        self.mock = MockLLM().start()
        self.addCleanup(self.mock.stop)


class OllamaTests(MockedTest):
    def client(self, model="gemma3:4b", **kw):
        return OllamaClient(self.mock.url, model=model, timeout=kw.pop("timeout", 5), **kw)

    def test_list_models_and_version(self):
        models = self.client().list_models()
        self.assertEqual([m.name for m in models], [m["name"] for m in DEFAULT_MODELS])
        self.assertEqual((models[0].params, models[0].family, models[0].size_bytes), ("4.3B", "gemma3", 3_300_000_000))
        self.assertEqual(self.client().version(), "0.12.0")

    def test_request_shape(self):
        text = self.client(temperature=0.2, seed=11).complete_json(SYSTEM, USER, VERDICT_SCHEMA, 321)
        path, payload = self.mock.calls[-1]
        self.assertEqual(path, "/api/chat")
        self.assertEqual(payload["model"], "gemma3:4b")
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["format"], VERDICT_SCHEMA)
        self.assertEqual(payload["options"]["num_predict"], 321)
        self.assertEqual((payload["options"]["temperature"], payload["options"]["seed"]), (0.2, 11))
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])
        self.assertEqual(payload["messages"][1]["content"], USER)
        self.assertNotIn("think", payload)
        rows, _ = extract_rows(text)
        self.assertEqual([r["id"] for r in rows], [1, 2])

    def test_model_not_installed(self):
        with self.assertRaises(LLMError) as ctx:
            self.client(model="nope:1b").complete_json(SYSTEM, USER, VERDICT_SCHEMA, 100)
        self.assertEqual(ctx.exception.kind, "model_missing")
        self.assertIn("ollama pull nope:1b", ctx.exception.hint)

    def test_falls_back_to_plain_json_when_schema_is_rejected(self):
        self.mock.reject_schema_format = True
        client = self.client()
        client.complete_json(SYSTEM, USER, VERDICT_SCHEMA, 100)
        client.complete_json(SYSTEM, USER, VERDICT_SCHEMA, 100)
        formats = [p["format"] for _, p in self.mock.calls]
        self.assertEqual(formats[0], VERDICT_SCHEMA)
        self.assertEqual(formats[1:], ["json", "json"])  # and it remembers

    def test_reasoning_models_get_think_off_and_drop_it_if_rejected(self):
        self.mock.models.append({"name": "qwen3:4b", "details": {"parameter_size": "4B"}})
        client = self.client(model="qwen3:4b")
        client.complete_json(SYSTEM, USER, VERDICT_SCHEMA, 100)
        self.assertIs(self.mock.calls[-1][1]["think"], False)
        self.mock.reject_think = True
        retry_client = self.client(model="qwen3:4b")
        retry_client.complete_json(SYSTEM, USER, VERDICT_SCHEMA, 100)
        self.assertNotIn("think", self.mock.calls[-1][1])

    def test_empty_answer(self):
        self.mock.mode = "empty"
        with self.assertRaises(LLMError) as ctx:
            self.client().complete_json(SYSTEM, USER, VERDICT_SCHEMA, 100)
        self.assertEqual(ctx.exception.kind, "format")

    def test_server_error(self):
        self.mock.mode = "error500"
        with self.assertRaises(LLMError) as ctx:
            self.client().complete_json(SYSTEM, USER, VERDICT_SCHEMA, 100)
        self.assertEqual((ctx.exception.kind, ctx.exception.status), ("http", 500))
        self.assertIn("crashed", ctx.exception.message)

    def test_timeout(self):
        self.mock.delay = 1.0
        with self.assertRaises(LLMError) as ctx:
            self.client(timeout=0.3).complete_json(SYSTEM, USER, VERDICT_SCHEMA, 100)
        self.assertEqual(ctx.exception.kind, "timeout")

    def test_unreachable(self):
        client = OllamaClient("http://127.0.0.1:1", model="x", timeout=2)
        with mock.patch(
            "wontdo.llm._LOCAL_OPENER.open",
            side_effect=urllib.error.URLError(ConnectionRefusedError("connection refused")),
        ):
            with self.assertRaises(LLMError) as ctx:
                client.list_models(timeout=1)
        self.assertEqual(ctx.exception.kind, "unreachable")


class OpenAITests(MockedTest):
    def client(self, **kw):
        return OpenAIClient(self.mock.url, model="gemma3:4b", timeout=5, **kw)

    def test_list_models(self):
        self.assertEqual([m.name for m in self.client().list_models()][0], "gemma3:4b")

    def test_request_shape_uses_a_json_schema(self):
        self.client().complete_json(SYSTEM, USER, VERDICT_SCHEMA, 222)
        path, payload = self.mock.calls[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], VERDICT_SCHEMA)
        self.assertIs(payload["response_format"]["json_schema"]["strict"], True)
        self.assertEqual((payload["max_tokens"], payload["stream"]), (222, False))

    def test_degrades_json_schema_then_json_object_then_nothing_and_remembers(self):
        self.mock.reject_json_schema = True
        client = self.client()
        client.complete_json(SYSTEM, USER, VERDICT_SCHEMA, 100)
        self.assertEqual([p.get("response_format", {}).get("type") for _, p in self.mock.calls], ["json_schema", "json_object"])
        self.mock.reject_json_object = True
        other = self.client()
        other.complete_json(SYSTEM, USER, VERDICT_SCHEMA, 100)
        kinds = [p.get("response_format", {}).get("type") for _, p in self.mock.calls[2:]]
        self.assertEqual(kinds, ["json_schema", "json_object", None])
        before = len(self.mock.calls)
        other.complete_json(SYSTEM, USER, VERDICT_SCHEMA, 100)
        self.assertEqual(len(self.mock.calls) - before, 1)  # remembered: straight to no response_format

    def test_api_key(self):
        self.mock.api_key = "sekret"
        self.client(api_key="sekret").complete_json(SYSTEM, USER, VERDICT_SCHEMA, 100)
        with self.assertRaises(LLMError) as ctx:
            self.client().list_models()
        self.assertEqual(ctx.exception.status, 401)

    def test_v1_suffix_in_base_url_is_fine(self):
        client = OpenAIClient(self.mock.url + "/v1", model="gemma3:4b", timeout=5)
        self.assertEqual(len(client.list_models()), 3)

    def test_empty_choices(self):
        with mock.patch("wontdo.llm.http_json", return_value={"choices": []}):
            with self.assertRaises(LLMError) as ctx:
                self.client().complete_json(SYSTEM, USER, VERDICT_SCHEMA, 100)
        self.assertEqual(ctx.exception.kind, "format")

    def test_ollamas_own_openai_endpoint_works_too(self):
        self.mock.mode = "ok"
        text = self.client().complete_json(SYSTEM, USER, VERDICT_SCHEMA, 100)
        self.assertEqual(len(extract_rows(text)[0]), 2)


class Detection(MockedTest):
    def test_ollama_at_an_explicit_url(self):
        backend = detect_backend(settings(base_url=self.mock.url))
        self.assertTrue(backend.reachable)
        self.assertEqual((backend.provider, backend.model, backend.version), ("ollama", "gemma3:4b", "0.12.0"))
        self.assertFalse(backend.model_missing)
        self.assertEqual(len(backend.models), 3)

    def test_openai_compatible_server_is_found_when_ollama_api_is_absent(self):
        self.mock.ollama_enabled = False
        backend = detect_backend(settings(base_url=self.mock.url))
        self.assertEqual((backend.reachable, backend.provider), (True, "openai"))

    def test_provider_can_be_forced(self):
        self.assertEqual(detect_backend(settings(base_url=self.mock.url, provider="openai")).provider, "openai")
        self.mock.openai_enabled = False
        self.assertFalse(detect_backend(settings(base_url=self.mock.url, provider="openai")).reachable)

    def test_auto_probes_the_default_local_ports(self):
        self.mock.ollama_enabled = False
        dead = "http://127.0.0.1:%d" % free_port()
        with mock.patch("wontdo.llm.OLLAMA_URL", dead), mock.patch("wontdo.llm.AUTO_OPENAI_URLS", (dead, self.mock.url)):
            backend = detect_backend(settings())
        self.assertEqual((backend.reachable, backend.provider, backend.base_url), (True, "openai", self.mock.url))

    def test_nothing_running(self):
        dead = "http://127.0.0.1:%d" % free_port()
        with mock.patch("wontdo.llm.OLLAMA_URL", dead), mock.patch("wontdo.llm.AUTO_OPENAI_URLS", (dead,)):
            backend = detect_backend(settings())
        self.assertFalse(backend.reachable)
        self.assertIn("ollama pull", backend.hint)
        self.assertIn("Ollama", backend.error)

    def test_explicit_dead_url_names_the_url(self):
        dead = "http://127.0.0.1:%d" % free_port()
        backend = detect_backend(settings(base_url=dead))
        self.assertFalse(backend.reachable)
        self.assertIn(dead, backend.error)

    def test_remote_servers_need_permission(self):
        with mock.patch("wontdo.llm._make_raw_client") as make:
            backend = detect_backend(settings(base_url="http://192.168.1.20:11434"))
            make.assert_not_called()
        self.assertFalse(backend.reachable)
        self.assertIn("leave", backend.error)
        self.assertIn("Settings", backend.hint)

    def test_remote_servers_are_tried_once_allowed(self):
        with mock.patch("wontdo.llm._make_raw_client") as make:
            make.return_value.list_models.return_value = [ModelInfo("gemma3:4b")]
            make.return_value.version.return_value = ""
            backend = detect_backend(settings(base_url="http://192.168.1.20:11434", allow_remote=True))
        self.assertTrue(backend.reachable)

    def test_requested_model_that_is_not_installed(self):
        backend = detect_backend(settings(base_url=self.mock.url, model="ghost:7b"))
        self.assertEqual((backend.reachable, backend.model, backend.model_missing), (True, "ghost:7b", True))

    def test_server_with_no_models(self):
        self.mock.models = []
        backend = detect_backend(settings(base_url=self.mock.url))
        self.assertTrue(backend.reachable)
        self.assertEqual(backend.model, "")
        self.assertTrue(backend.model_missing)
        self.assertIn("no models", backend.error)

    def test_make_client_requires_a_reachable_backend(self):
        dead = detect_backend(settings(base_url="http://127.0.0.1:%d" % free_port()))
        with self.assertRaises(LLMError):
            make_client(settings(), dead)
        good = detect_backend(settings(base_url=self.mock.url))
        client = make_client(settings(), good)
        self.assertEqual((client.provider, client.model), ("ollama", "gemma3:4b"))

    def test_backend_to_dict_is_json_friendly(self):
        import json

        json.dumps(detect_backend(settings(base_url=self.mock.url)).to_dict())


class Doctor(MockedTest):
    def run_it(self, **overrides):
        return run_doctor(settings(base_url=self.mock.url, **overrides))

    def levels(self, outcome):
        return [s["level"] for s in outcome["steps"]]

    def test_healthy_setup(self):
        outcome = self.run_it()
        self.assertTrue(outcome["ok"])
        self.assertEqual(self.levels(outcome), ["ok"] * 5)
        self.assertIn("Pay electricity bill", outcome["steps"][-1]["detail"])

    def test_works_over_the_openai_api_too(self):
        self.mock.ollama_enabled = False
        self.assertTrue(self.run_it()["ok"])

    def test_nothing_running(self):
        outcome = run_doctor(settings(base_url="http://127.0.0.1:%d" % free_port()))
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["steps"][0]["level"], "fail")

    def test_no_models(self):
        self.mock.models = []
        self.assertEqual(self.levels(self.run_it()), ["ok", "fail"])

    def test_missing_model(self):
        outcome = self.run_it(model="ghost:7b")
        self.assertFalse(outcome["ok"])
        self.assertIn("ollama pull ghost:7b", outcome["steps"][-1]["hint"])

    def test_garbage_answers_show_the_raw_reply(self):
        self.mock.mode = "garbage"
        outcome = self.run_it()
        self.assertFalse(outcome["ok"])
        self.assertIn("sorry", outcome["raw"])

    def test_a_model_that_wants_to_skip_the_bill_gets_a_warning_not_a_failure(self):
        self.mock.mode = "all_expire"
        outcome = self.run_it()
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["steps"][-1]["level"], "warn")

    def test_doctor_never_falls_back_to_the_demo_brain(self):
        self.mock.mode = "bad_bucket"
        self.assertFalse(self.run_it()["ok"])


if __name__ == "__main__":
    unittest.main()
