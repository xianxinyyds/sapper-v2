#!/usr/bin/env python3
"""CTFY-0001 v3 single-challenge container lifecycle."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

import ctfy
import sapper
from ctfy_engine import configure_model


def _write_empty_result(work_dir: Path, stop_reason: str) -> None:
    ctfy.write_result(
        work_dir, answers={}, solved=[], fully_solved=False, stop_reason=stop_reason,
    )


def _fail(work_dir: Path, stop_reason: str, error: Exception,
          store: sapper.Store | None = None, *, exit_code: int = 2) -> int:
    try:
        if store is None:
            _write_empty_result(work_dir, stop_reason)
        else:
            finalize_result(work_dir, store, stop_reason)
    except (ctfy.CtfyError, sapper.FGError) as result_error:
        sys.stderr.write(f"ctfy: {result_error}\n")
    sys.stderr.write(f"ctfy: {error}\n")
    return exit_code


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


def _checkpoint_result(work_dir: Path, store: sapper.Store) -> None:
    answers, solved, fully_solved = collect_ledger(store)
    ctfy.write_result(
        work_dir, answers=answers, solved=solved,
        fully_solved=fully_solved, stop_reason="incomplete",
    )


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
    with store.locked():
        project = store.load_project()
        metadata = project.get("ctfy") or {}
        if metadata.pop("blocker", None) is not None:
            store.save_project(project)
    sapper.sync_ctfy_facts(store, task, challenge)
    return store


def _positive_env(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ctfy.CtfyError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ctfy.CtfyError(f"{name} must be a positive integer")
    return value


def _max_steps(task: dict) -> int:
    configured = task.get("max_turns")
    if type(configured) is not int or configured < 1:
        configured = _positive_env("CTFY_EVAL_MAX_TURNS")
    if configured is None:
        configured = 8
    safety_cap = _positive_env("SAPPER_MAX_STEPS")
    if safety_cap is not None:
        configured = min(configured, safety_cap)
    return configured


def _round_deadline(challenge: dict) -> float:
    minutes = _positive_env("SAPPER_ROUND_MINUTES") or 165
    candidates = [time.time() + minutes * 60]
    close_at = challenge.get("close_at")
    if isinstance(close_at, str) and close_at.strip():
        text = close_at.strip()
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            candidates.append(parsed.timestamp())
        except ValueError:
            pass
    return min(candidates)


def run(work_dir: Path) -> int:
    try:
        task = ctfy.load_task(work_dir)
    except ctfy.CtfyError as exc:
        return _fail(work_dir, "config_error", exc)
    if task.get("dry_run") is True or os.environ.get("CTFY_EVAL_DRY_RUN") == "1":
        _write_empty_result(work_dir, "dry_run")
        return 0
    try:
        ctfy.require_task(task)
    except ctfy.CtfyError as exc:
        reason = "unsupported_run_mode" if task.get("run_mode") not in {"instance", "qa"} else "config_error"
        return _fail(work_dir, reason, exc)
    try:
        client = ctfy.CtfyClient.from_env()
        challenge = client.get_challenge(task["challenge"]["id"])
    except ctfy.CtfyError as exc:
        reason = "blocked_by_infra" if exc.transport else "config_error"
        exit_code = 0 if exc.transport else 2
        store = sapper.Store(work_dir / ".sapper")
        try:
            if store.exists() and store.load_project().get("origin") \
                    == f"ctfy:{task['challenge']['id']}":
                return _fail(work_dir, reason, exc, store, exit_code=exit_code)
        except sapper.FGError:
            pass
        return _fail(work_dir, reason, exc, exit_code=exit_code)
    try:
        store = prepare_graph(work_dir, task, challenge)
    except (ctfy.CtfyError, sapper.FGError) as exc:
        candidate_store = sapper.Store(work_dir / ".sapper")
        try:
            if candidate_store.exists() and candidate_store.load_project().get("origin") \
                    == f"ctfy:{task['challenge']['id']}":
                return _fail(work_dir, "config_error", exc, candidate_store)
        except sapper.FGError:
            pass
        return _fail(work_dir, "config_error", exc)
    try:
        if store.load_project()["status"] == "completed" \
                and challenge.get("my_fully_solved") is not True:
            store.reopen_project()
        _checkpoint_result(work_dir, store)
        model = configure_model()
        max_steps = _max_steps(task)
        max_explore = _positive_env("SAPPER_MAX_EXPLORE") or max_steps
        dispatch_timeout = _positive_env("CTFY_DISPATCH_TIMEOUT") \
            or _positive_env("SAPPER_TIMEOUT") or 300
        deadline = _round_deadline(challenge)
        ctfy.ensure_attachments(task, client, work_dir)
    except (ctfy.CtfyError, sapper.FGError) as exc:
        transport = getattr(exc, "transport", False)
        return _fail(
            work_dir, "blocked_by_infra" if transport else "config_error", exc, store,
            exit_code=0 if transport else 2,
        )

    auto_args = [
        "--store", str(store.root), "auto",
        "--model", model,
        "--templates", str(Path(__file__).resolve().parent.parent / "templates" / "ctfy"),
        "--skip-permissions",
        "--max-steps", str(max_steps),
        "--max-explore", str(max_explore),
    ]
    auto_args += [
        "--timeout", str(dispatch_timeout),
        "--deadline", datetime.fromtimestamp(deadline).isoformat(),
    ]
    return_code = sapper.main(
        auto_args, checkpoint=lambda: _checkpoint_result(work_dir, store)
    )
    answers, _, _ = collect_ledger(store)
    project = store.load_project()
    outcome = project.get("last_auto")
    outcome_reason = outcome.get("reason") if isinstance(outcome, dict) else None
    if outcome_reason in {"model_error", "explore_failed"}:
        stop_reason = outcome_reason
    elif return_code != 0:
        stop_reason = "auto_failed"
    elif project.get("status") == "completed" \
            or (project.get("ctfy") or {}).get("fully_solved") is True \
            or outcome_reason == "completed":
        stop_reason = "done"
    elif outcome_reason == "blocker":
        stop_reason = "blocked_by_infra"
    elif task["run_mode"] == "qa" and answers:
        stop_reason = "answers_handed_off"
    elif isinstance(outcome_reason, str) and outcome_reason != "running":
        stop_reason = outcome_reason
    else:
        stop_reason = "incomplete"
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
