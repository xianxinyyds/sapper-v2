import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "openai_http_engine.py"


def load_engine(**env):
    spec = importlib.util.spec_from_file_location("openai_http_engine_under_test", ENGINE)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict("os.environ", env, clear=False):
        spec.loader.exec_module(module)
    return module


def tool_message(command="hostname", call_id="c1"):
    return {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "function": {"name": "shell",
                                 "arguments": json.dumps({"command": command})},
                }],
            },
        }],
    }


def text_message(text):
    return {"choices": [{"message": {"content": text}}]}


class EngineTests(unittest.TestCase):
    def test_bare_gateway_url_gets_v1(self):
        engine = load_engine(LLM_BASE_URL="https://gateway.example")
        self.assertEqual(engine.BASE, "https://gateway.example/v1")

    def test_explicit_v1_is_preserved(self):
        engine = load_engine(LLM_BASE_URL="https://gateway.example/v1")
        self.assertEqual(engine.BASE, "https://gateway.example/v1")

    def test_model_comes_from_environment(self):
        engine = load_engine(LLM_MODEL="deepseek-flash")
        self.assertEqual(engine.MODEL, "deepseek-flash")

    def test_worker_uses_the_forwarded_llm_api_key(self):
        engine = load_engine(
            LLM_API_KEY="platform-key",
            ANTHROPIC_API_KEY="", OPENAI_API_KEY="",
        )
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(text_message("ok")).encode()

        def urlopen(request, timeout=None):
            captured["auth"] = request.get_header("Authorization")
            return Response()

        with mock.patch.object(engine.urllib.request, "urlopen", side_effect=urlopen):
            engine.call_model([{"role": "user", "content": "solve"}])
        self.assertEqual(captured["auth"], "Bearer platform-key")

    def test_commander_mode_sends_no_tools(self):
        engine = load_engine(CTFY_ENGINE_NO_TOOLS="1")
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(text_message("ok")).encode()

        def urlopen(request, timeout=None):
            captured["payload"] = json.loads(request.data.decode())
            return Response()

        with mock.patch.object(engine.urllib.request, "urlopen", side_effect=urlopen):
            engine.call_model([{"role": "user", "content": "decide"}])
        self.assertNotIn("tools", captured["payload"])
        self.assertEqual(captured["payload"]["max_tokens"], 4096)

    def test_worker_mode_sends_tools(self):
        engine = load_engine(CTFY_ENGINE_NO_TOOLS="0")
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(text_message("ok")).encode()

        def urlopen(request, timeout=None):
            captured["payload"] = json.loads(request.data.decode())
            return Response()

        with mock.patch.object(engine.urllib.request, "urlopen", side_effect=urlopen):
            engine.call_model([{"role": "user", "content": "solve"}])
        self.assertEqual(captured["payload"]["tools"][0]["function"]["name"], "shell")

    def test_two_repeats_are_tolerated_three_end_the_loop(self):
        engine = load_engine()
        replies = [
            tool_message(call_id="c1"), tool_message(call_id="c2"),
            tool_message(call_id="c3"), tool_message(call_id="c4"),
        ]
        out = io.StringIO()
        with mock.patch.object(engine, "call_model", side_effect=replies), \
                mock.patch.object(engine, "finish_without_tools",
                                  return_value=0) as finish, \
                mock.patch.object(engine, "run_tool", return_value="ok"), \
                mock.patch.object(sys, "stdin", io.StringIO("solve")), \
                redirect_stdout(out):
            self.assertEqual(engine.main(), 0)
        finish.assert_called_once()
        self.assertIn("repeated", finish.call_args.args[1])

    def test_round_limit_ends_with_a_final_answer(self):
        engine = load_engine()
        outs = io.StringIO()

        def never_finishes(messages, *, tools=True):
            if tools:
                return tool_message(command="echo x")
            return text_message("final status text")

        with mock.patch.object(engine, "call_model", side_effect=never_finishes), \
                mock.patch.object(engine, "run_tool", return_value="ok"), \
                mock.patch.object(sys, "stdin", io.StringIO("solve")), \
                redirect_stdout(outs):
            self.assertEqual(engine.main(), 0)
        self.assertEqual(json.loads(outs.getvalue()), {"result": "final status text"})

    def test_empty_final_response_still_returns_an_envelope(self):
        engine = load_engine()
        outs = io.StringIO()
        with mock.patch.object(engine, "call_model", return_value=text_message("")), \
                mock.patch.object(sys, "stdin", io.StringIO("solve")), \
                redirect_stdout(outs):
            self.assertEqual(engine.main(), 0)
        result = json.loads(outs.getvalue())["result"]
        self.assertIn("no final text", result)

    def test_tool_loop_has_a_hard_bound(self):
        engine = load_engine()
        self.assertEqual(engine.MAX_ROUNDS, 16)


if __name__ == "__main__":
    unittest.main()
