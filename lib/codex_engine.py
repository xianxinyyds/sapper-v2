#!/usr/bin/env python3
"""Adapt sapper's Claude-style engine call to non-interactive Codex exec."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from sapper import FGError, extract_json_object


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", action="store_true")
    parser.add_argument("--output-format", default="json")
    parser.add_argument("--model", required=True)
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    parser.add_argument("--append-system-prompt", default=None)
    args = parser.parse_args()
    if not args.model.strip():
        parser.error("--model cannot be empty")
    prompt = sys.stdin.read()
    if args.append_system_prompt:
        prompt = args.append_system_prompt.strip() + "\n\n" + prompt
    if not prompt.strip():
        sys.stderr.write("codex prompt cannot be empty\n")
        return 2

    with tempfile.TemporaryDirectory(prefix="sapper-codex-") as tmp:
        last_message = Path(tmp) / "last-message.txt"
        argv = ["codex", "exec", "--skip-git-repo-check"]
        if args.dangerously_skip_permissions:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        argv += [
            "-c", f"model={args.model}",
            "--output-last-message", str(last_message), "-",
        ]
        try:
            proc = subprocess.run(argv, input=prompt, capture_output=True, text=True)
        except FileNotFoundError:
            sys.stderr.write("codex CLI not found on PATH\n")
            return 1
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            sys.stderr.write(f"codex exec exited {proc.returncode}: {detail[:1000]}\n")
            return proc.returncode
        try:
            result = last_message.read_text(encoding="utf-8").strip()
        except OSError as exc:
            sys.stderr.write(f"codex did not write its last message: {exc}\n")
            return 1
        if not result:
            sys.stderr.write("codex returned an empty last message\n")
            return 1
        try:
            extract_json_object(result)
        except FGError as exc:
            sys.stderr.write(f"codex returned an invalid response: {exc}\n")
            return 1
    sys.stdout.write(json.dumps({"result": result}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
