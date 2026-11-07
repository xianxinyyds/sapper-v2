#!/usr/bin/env python3
"""CTFY-0001 v3 single-challenge container lifecycle."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import ctfy
import sapper
from ctfy_engine import configure_model


def _write_empty_result(work_dir: Path, stop_reason: str) -> None:
    ctfy.write_result(
        work_dir, answers={}, solved=[], fully_solved=False, stop_reason=stop_reason,
    )


def _fail(work_dir: Path, stop_reason: str, error: Exception,
          store: sapper.Store | None = None) -> int:
    try:
        if store is None:
            _write_empty_result(work_dir, stop_reason)
        else:
            finalize_result(work_dir, store, stop_reason)
    except (ctfy.CtfyError, sapper.FGError) as result_error:
        sys.stderr.write(f"ctfy: {result_error}\n")
    sys.stderr.write(f"ctfy: {error}\n")
    return 2


def _scan_ledger(store: sapper.Store) -> tuple[dict[str, dict], set[str], bool]:
    latest: dict[str, dict] = {}
    solved: set[str] = set()
    for fact in store.list_facts():
        if fact.get("kind") != "flag":
            continue
        data = fact.get("data") or {}
        if data.get("platform") != "ctfy":
            continue
        question_id = data.get("question_id")
        if not isinstance(question_id, str) or not question_id:
            continue
        status = data.get("status", "recorded")
        if status == "accepted":
            solved.add(question_id)
        elif status == "recorded" and isinstance(data.get("value"), str):
            previous = latest.get(question_id)
            candidate_key = (fact.get("created_at", ""), fact.get("id", ""))
            previous_key = (
                previous.get("created_at", ""), previous.get("id", "")
            ) if previous else None
            if previous_key is None or candidate_key > previous_key:
                latest[question_id] = fact
    metadata = store.load_project().get("ctfy") or {}
    platform_solved = metadata.get("solved_question_ids", [])
    if isinstance(platform_solved, list):
        solved.update(q for q in platform_solved if isinstance(q, str))
    return latest, solved, metadata.get("fully_solved") is True


def collect_ledger(store: sapper.Store) -> tuple[dict[str, str], list[str], bool]:
    latest, solved, fully_solved = _scan_ledger(store)
    answers = {question: fact["data"]["value"] for question, fact in latest.items()}
    return answers, sorted(solved), fully_solved


def finalize_result(work_dir: Path, store: sapper.Store, stop_reason: str) -> None:
    latest, solved, fully_solved = _scan_ledger(store)
    answers = {question: fact["data"]["value"] for question, fact in latest.items()}
    ctfy.write_result(
        work_dir, answers=answers, solved=sorted(solved),
        fully_solved=fully_solved, stop_reason=stop_reason,
    )
    for fact in latest.values():
        store.update_flag_fact(fact["id"], "handed_off", None)


def prepare_graph(work_dir: Path, task: dict,
                  challenge: dict | None = None) -> sapper.Store:
    challenge_id = task["challenge"]["id"]
    origin = f"ctfy:{challenge_id}"
    store = sapper.Store(work_dir / ".sapper")
    if store.exists():
        if store.load_project()["origin"] != origin:
            raise sapper.FGError("fact graph belongs to a different CTFY challenge")
    else:
        store.init(origin, f"Solve CTFY challenge {challenge_id}")
    sapper.sync_ctfy_facts(store, task, challenge)
    return store


def _max_steps(task: dict) -> int:
    configured = task.get("max_turns")
    if type(configured) is int and configured > 0:
        return configured
    try:
        steps = int(os.environ.get("SAPPER_MAX_STEPS", "8"))
    except ValueError as exc:
        raise ctfy.CtfyError("SAPPER_MAX_STEPS must be a positive integer") from exc
    if steps < 1:
        raise ctfy.CtfyError("SAPPER_MAX_STEPS must be a positive integer")
    return steps


def run(work_dir: Path) -> int:
    try:
        task = ctfy.load_task(work_dir)
    except ctfy.CtfyError as exc:
        return _fail(work_dir, "config_error", exc)
    if task.get("dry_run") is True or os.environ.get("CTFY_EVAL_DRY_RUN") == "1":
        _write_empty_result(work_dir, "dry_run")
        return 0
    try:
        ctfy.require_instance_task(task)
    except ctfy.CtfyError as exc:
        reason = "unsupported_run_mode" if task.get("run_mode") != "instance" else "config_error"
        return _fail(work_dir, reason, exc)
    try:
        client = ctfy.CtfyClient.from_env()
        challenge = client.get_challenge(task["challenge"]["id"])
    except ctfy.CtfyError as exc:
        return _fail(work_dir, "config_error", exc)
    try:
        store = prepare_graph(work_dir, task, challenge)
    except (ctfy.CtfyError, sapper.FGError) as exc:
        return _fail(work_dir, "config_error", exc)
    try:
        if store.load_project()["status"] == "completed" \
                and challenge.get("my_fully_solved") is not True:
            store.reopen_project()
        model = configure_model()
        max_steps = _max_steps(task)
        ctfy.ensure_attachments(task, client, work_dir)
    except (ctfy.CtfyError, sapper.FGError) as exc:
        return _fail(work_dir, "config_error", exc, store)

    return_code = sapper.main([
        "--store", str(store.root), "auto",
        "--model", model,
        "--templates", str(Path(__file__).resolve().parent.parent / "templates" / "ctfy"),
        "--skip-permissions",
        "--max-steps", str(max_steps),
        "--max-explore", str(min(6, max_steps)),
    ])
    stop_reason = "done" if return_code == 0 else "auto_failed"
    try:
        finalize_result(work_dir, store, stop_reason)
    except (ctfy.CtfyError, sapper.FGError) as exc:
        sys.stderr.write(f"ctfy: {exc}\n")
        return 2
    return return_code


def main() -> int:
    return run(Path(os.environ.get("CTFY_WORK_DIR", "/ctfy/work")))


if __name__ == "__main__":
    sys.exit(main())
