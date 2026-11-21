#!/usr/bin/env python3
"""Bounded OpenAI-compatible tool loop used as SAPPER_BIN in CTFY runs."""
import json
import os
import signal
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

MODEL = os.environ.get("LLM_MODEL") or "deepseek-v4-pro"
BASE = os.environ.get(
    "LLM_BASE_URL", "https://deepseek.llm-next.ctfy.io/v1").rstrip("/")
if not urllib.parse.urlsplit(BASE).path.strip("/"):
    BASE = BASE + "/v1"
KEY = (
    os.environ.get("ANTHROPIC_API_KEY")
    or os.environ.get("OPENAI_API_KEY")
    or os.environ.get("LLM_API_KEY", "")
)
MAX_ROUNDS = 16
# Commander decisions need one JSON object, not an exploration loop.
TOOLS_ENABLED = os.environ.get("CTFY_ENGINE_NO_TOOLS") != "1"
TOOLS = [{
    "type": "function",
    "function": {
        "name": "shell",
        "description": "Run a bounded shell command in the CTF workdir",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 90},
            },
            "required": ["command"],
        },
    },
}]


def call_model(messages, *, tools=True):
    payload = {"model": MODEL, "max_tokens": 4096, "messages": messages}
    if tools and TOOLS_ENABLED:
        payload["tools"] = TOOLS
    request = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        return json.loads(response.read().decode("utf-8"))


def run_tool(call):
    function = call.get("function") or {}
    try:
        args = json.loads(function.get("arguments") or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        command = args["command"]
        if not isinstance(command, str):
            raise ValueError("command must be a string")
        timeout = max(1, min(int(args.get("timeout", 30) or 30), 90))
        proc = subprocess.Popen(
            ["/bin/bash", "-lc", command], cwd="/ctfy/work",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=3)
            return f"ERROR: command timed out after {timeout}s"
        output = (stdout or "") + (stderr or "")
        return f"exit={proc.returncode}\n{output[:16000]}"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


def _content(message):
    text = message.get("content", "")
    if isinstance(text, list):
        text = "".join(
            part.get("text", "") for part in text if isinstance(part, dict))
    if not isinstance(text, str):
        text = ""
    if not text.strip():
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str):
            text = reasoning
    return text


def _call_signature(calls):
    return [
        (
            (call.get("function") or {}).get("name"),
            (call.get("function") or {}).get("arguments"),
        )
        for call in calls
    ]


def finish_without_tools(messages, reason):
    print(f"requesting final answer ({reason})", file=sys.stderr)
    final_messages = list(messages) + [{
        "role": "user",
        "content": (
            "Stop calling tools. Return ONLY the single JSON object that the "
            "original instructions require. No prose, no markdown fences, no "
            "status summary. If the original instructions ask for a decision "
            "or an intent, emit exactly that JSON now."
        ),
    }]
    data = call_model(final_messages, tools=False)
    text = _content(data["choices"][0]["message"]).strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    if not text:
        text = (
            "The model produced no final text. Last status: tool loop "
            f"stopped because {reason}."
        )
    print(json.dumps({"result": text}, ensure_ascii=False))
    return 0


def main():
    prompt = sys.stdin.read()
    args = sys.argv[1:]
    if "--append-system-prompt" in args:
        prompt = args[args.index("--append-system-prompt") + 1] + "\n\n" + prompt
    messages = [{"role": "user", "content": prompt}]
    previous = []
    repeats = 0
    for _ in range(1, MAX_ROUNDS + 1):
        data = call_model(messages)
        print("model response received", file=sys.stderr)
        message = data["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if not calls:
            text = _content(message)
            if not text.strip():
                return finish_without_tools(messages, "empty model response")
            print(json.dumps({"result": text}, ensure_ascii=False))
            return 0
        signature = _call_signature(calls)
        if signature and signature == previous:
            repeats += 1
        else:
            repeats = 1
        if repeats >= 3:
            return finish_without_tools(
                messages, "the model repeated the same tool call three times")
        previous = signature
        messages.append({
            "role": "assistant",
            "content": message.get("content", ""),
            "tool_calls": calls,
        })
        for call in calls:
            print("executing tool", call.get("function", {}).get("name"),
                  file=sys.stderr)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": run_tool(call),
            })
        print("tool results sent", file=sys.stderr)
    return finish_without_tools(messages, f"{MAX_ROUNDS} tool rounds reached")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000]
        print(f"model gateway HTTP {exc.code}: {detail}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"model gateway error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
