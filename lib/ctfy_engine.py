#!/usr/bin/env python3
"""Map CTFY-provided model settings onto the existing CLI harness."""
from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path

from ctfy import CtfyError


_ANTHROPIC_MODEL_SLOTS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)


def configure_model(environ: MutableMapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    flavor = env.get("LLM_FLAVOR", "").strip()
    key = env.get("LLM_API_KEY", "").strip()
    model = env.get("LLM_MODEL", "").strip()
    base_url = env.get("LLM_BASE_URL", "").strip()
    if flavor not in {"anthropic", "openai"}:
        raise CtfyError(f"unsupported LLM_FLAVOR {flavor!r}; expected anthropic or openai")
    if not key:
        raise CtfyError("LLM_API_KEY is missing; refusing to use a personal fallback")
    if not model:
        raise CtfyError("LLM_MODEL is missing")

    env.pop("SAPPER_KEY_POOL", None)
    env["SAPPER_MODEL"] = model
    if flavor == "anthropic":
        env["ANTHROPIC_API_KEY"] = key
        env["ANTHROPIC_AUTH_TOKEN"] = key
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url
        else:
            env.pop("ANTHROPIC_BASE_URL", None)
        for slot in _ANTHROPIC_MODEL_SLOTS:
            env[slot] = model
        env["SAPPER_BIN"] = "claude"
    else:
        env["OPENAI_API_KEY"] = key
        env["OPENAI_MODEL"] = model
        if base_url:
            env["OPENAI_BASE_URL"] = base_url
        else:
            env.pop("OPENAI_BASE_URL", None)
        env["SAPPER_BIN"] = str(Path(__file__).resolve().with_name("codex_engine.py"))
    return model
