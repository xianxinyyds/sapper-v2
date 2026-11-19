#!/usr/bin/env python3
"""Authoritative, redacted board projection for CTFY campaigns."""
from __future__ import annotations

import datetime
import json
import math
import os
import re
from pathlib import Path


_ACTIVE_WORKERS = {"starting", "prepared", "running", "working", "stopping"}
_METADATA_LIMIT = 256 * 1024
_FACT_LIMIT = 64 * 1024
_SECRET_VALUE_PATTERN = re.compile(
    r"(?:"
    r"\b(?:flag|ctfy?)\{[^{}\r\n]*\}"
    r"|-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----"
    r"|(?<![A-Za-z0-9])(?:"
    r"sk-|sk_(?:live|test)_|rk_(?:live|test)_|gh[pousr]_|github_pat_|"
    r"glpat-|xox[baprs]-|AIza|AKIA|ASIA|hf_"
    r")[A-Za-z0-9_+=/-]{4,}"
    r"|(?<![A-Za-z0-9])Bearer\s+[A-Za-z0-9._~+/-]{4,}={0,2}"
    r")",
    re.IGNORECASE,
)
_BOARD_FIELDS = (
    "id", "name", "category", "difficulty", "solves_count", "scoring",
    "closed", "my_team_authored", "my_team_fully_solved",
    "my_team_solved_question_ids", "platform_solved_question_ids",
    "questions", "gated_questions", "instance_id", "instance_status",
    "instance_expires_at", "created_by_campaign", "work_dir",
    "result_summary", "worker", "worker_status", "attempt_minutes",
    "total_minutes", "first_seen_at", "last_progress_at", "stalled",
    "stalled_decisions", "unsupported", "status",
)


def _number(value, default=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value) if math.isfinite(value) else default


def _timestamp(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    result = parsed.timestamp()
    return result if math.isfinite(result) else None


def _read_object(path: Path, *, limit: int = _METADATA_LIMIT) -> dict:
    """Read one bounded JSON object; malformed optional metadata is ignored."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            return {}
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str)))


def _questions(source: dict) -> list[dict]:
    rows = []
    for question in source.get("questions", []):
        if not isinstance(question, dict) or not isinstance(question.get("id"), str):
            continue
        rows.append({
            "id": question["id"],
            "unlocked": question.get("unlocked", True) is True,
            "answered_correctly": question.get("answered_correctly") is True,
        })
    return rows


def _instance_for(challenge: dict, instances: list[dict]) -> dict | None:
    if challenge.get("is_qa_only") is True:
        return None
    challenge_id = challenge.get("id")
    for instance in instances:
        if isinstance(instance, dict) and instance.get("challenge_id") == challenge_id:
            return instance
    return None


def _unsupported(challenge: dict, instance: dict | None, task: dict) -> str | None:
    if challenge.get("defence") is not None:
        return "defence challenges are unsupported"
    source = instance or challenge
    topology = source.get("network_topology", challenge.get("network_topology", "simple"))
    mode = challenge.get("challenge_mode", challenge.get("mode"))
    if topology in (None, "", "simple") and mode not in (None, "", "simple", "instance", "qa"):
        topology = mode
    normalized = topology.lower() if isinstance(topology, str) else topology
    if normalized == "engagement":
        return "engagement networking is unsupported"
    if normalized == "awd" or challenge.get("awd") is True:
        return "AWD challenges are unsupported"
    if normalized != "simple":
        return f"unknown challenge mode {str(topology)[:80]!r} is unsupported"
    task_mode = task.get("run_mode")
    if task_mode is not None and task_mode not in {"instance", "qa"}:
        return f"unknown task run mode {str(task_mode)[:80]!r} is unsupported"
    return None


def _latest_fact_progress(work_dir: Path) -> float | None:
    latest = None
    facts_dir = work_dir / ".sapper" / "facts"
    try:
        paths = sorted(facts_dir.glob("*.json"))[:10000]
    except OSError:
        return None
    for path in paths:
        fact = _read_object(path, limit=_FACT_LIMIT)
        if fact.get("creator") in (None, "ctfy", "system"):
            continue
        created_at = _timestamp(fact.get("created_at"))
        if created_at is not None and (latest is None or created_at > latest):
            latest = created_at
    return latest


def _result_summary(work_dir: Path) -> dict:
    result = _read_object(work_dir / "result.json")
    handoff = _read_object(work_dir / "handoff.json")
    submissions = handoff.get("submissions", [])
    if not isinstance(submissions, list):
        submissions = []
    counts = {"accepted": 0, "rejected": 0, "submission_failed": 0, "unresolved": 0}
    for submission in submissions:
        if not isinstance(submission, dict):
            continue
        if submission.get("correct") is True:
            counts["accepted"] += 1
        elif submission.get("correct") is False:
            counts["rejected"] += 1
        elif submission.get("transport_uncertain") is True \
                or submission.get("status") == "unresolved":
            counts["unresolved"] += 1
        elif submission.get("status") == "submission_failed" or "error" in submission:
            counts["submission_failed"] += 1
    stop_reason = result.get("stop_reason")
    answers = result.get("answers", {})
    answer_values = {
        value for value in answers.values() if isinstance(value, str)
    } if isinstance(answers, dict) else set()
    answer_values.update(
        submission.get("answer") for submission in submissions
        if isinstance(submission, dict) and isinstance(submission.get("answer"), str)
    )
    if not isinstance(stop_reason, str) or len(stop_reason) > 100 \
            or stop_reason in answer_values or _SECRET_VALUE_PATTERN.search(stop_reason):
        stop_reason = None
    return {"stop_reason": stop_reason, **counts}


def _worker_summary(worker: dict, record: dict, *, now: float) -> tuple[dict, float, float]:
    status = worker.get("status") if isinstance(worker.get("status"), str) else "idle"
    started_at = _timestamp(worker.get("started_at"))
    active = status in _ACTIVE_WORKERS
    if active and started_at is not None:
        attempt_minutes = max(0.0, (now - started_at) / 60)
    else:
        attempt_minutes = _number(
            worker.get("attempt_minutes", record.get("attempt_minutes", 0)),
        )
    total_minutes = _number(record.get("total_minutes", record.get("minutes", 0)))
    if active:
        total_minutes += attempt_minutes
    summary = {"status": status}
    if started_at is not None:
        summary["started_at"] = started_at
    if isinstance(worker.get("cancel_after_slice"), bool):
        summary["cancel_after_slice"] = worker["cancel_after_slice"]
    return summary, attempt_minutes, total_minutes


def _platform_progress(challenge: dict, instance: dict | None,
                       questions: list[dict]) -> tuple[list[str], list[str], bool]:
    team_ids = _strings(challenge.get("my_team_solved_question_ids"))
    platform_ids = list(team_ids)
    if challenge.get("is_qa_only") is True:
        platform_ids.extend(_strings(challenge.get("my_solved_question_ids")))
    platform_ids.extend(question["id"] for question in questions if question["answered_correctly"])
    platform_ids = list(dict.fromkeys(platform_ids))
    fully_solved = challenge.get("my_team_fully_solved") is True
    if challenge.get("is_qa_only") is True:
        fully_solved = fully_solved or challenge.get("my_fully_solved") is True
    if instance is not None:
        fully_solved = fully_solved or instance.get("grace_kind") == "solved"
    source = instance or challenge
    source_questions = [q for q in source.get("questions", []) if isinstance(q, dict)]
    all_visible_answered = bool(questions) and all(
        question["unlocked"]
        and (question["answered_correctly"] or question["id"] in platform_ids)
        for question in questions
    )
    if all_visible_answered and len(source_questions) >= len(challenge.get("questions", [])):
        fully_solved = True
    return team_ids, platform_ids, fully_solved


def build_board(challenges: list[dict], instances: list[dict], state: dict, *,
                now: float) -> list[dict]:
    """Project platform and attempt metadata in the caller-provided order.

    Challenge records in ``state`` are updated in place. Only platform question
    progress affects solved status; local result and handoff claims do not.
    """
    now = _number(now, float("nan"))
    if not math.isfinite(now):
        raise ValueError("board time must be finite")
    records = state.setdefault("challenges", {})
    workers = state.get("workers", {})
    instance_records = state.get("instances", {})
    stall_minutes = _number(state.get("stall_minutes", 30), 30)
    rows = []
    for challenge in challenges:
        challenge_id = challenge.get("id")
        if not isinstance(challenge_id, str):
            raise ValueError("challenge id must be a string")
        record = records.setdefault(challenge_id, {})
        first_seen_at = _timestamp(record.get("first_seen_at"))
        if first_seen_at is None:
            first_seen_at = now
        record["first_seen_at"] = first_seen_at

        instance = _instance_for(challenge, instances)
        source = instance or challenge
        questions = _questions(source)
        team_ids, platform_ids, fully_solved = _platform_progress(
            challenge, instance, questions,
        )
        work_dir_value = record.get("work_dir")
        work_dir = Path(work_dir_value) if isinstance(work_dir_value, str) and work_dir_value else None
        task = _read_object(work_dir / "task.json") if work_dir is not None else {}
        unsupported = _unsupported(challenge, instance, task)

        old_progress = _timestamp(record.get("last_progress_at"))
        fact_progress = _latest_fact_progress(work_dir) if work_dir is not None else None
        old_platform_ids = set(_strings(record.get("platform_solved_question_ids")))
        platform_changed = any(qid not in old_platform_ids for qid in platform_ids)
        fact_changed = fact_progress is not None \
            and (old_progress is None or fact_progress > old_progress)
        meaningful_progress = platform_changed or fact_changed
        latest_progress = old_progress
        for candidate in (fact_progress, now if platform_changed else None):
            if candidate is not None and (latest_progress is None or candidate > latest_progress):
                latest_progress = candidate
        record["last_progress_at"] = latest_progress
        record["platform_solved_question_ids"] = platform_ids

        if fully_solved:
            status = "solved"
        elif challenge.get("closed") is True:
            status = "closed"
        elif unsupported is not None:
            status = "unsupported"
        else:
            worker_status = workers.get(challenge_id, {}).get("status", "idle")
            if worker_status in _ACTIVE_WORKERS:
                status = "active"
            elif worker_status == "parked" or record.get("parked") is True:
                status = "parked"
            else:
                status = "idle"

        progress_basis = latest_progress if latest_progress is not None else first_seen_at
        stalled = status not in {"solved", "closed", "unsupported"} \
            and now - progress_basis >= stall_minutes * 60
        previous_stalled_decisions = int(_number(record.get("stalled_decisions", 0)))
        if meaningful_progress or not stalled:
            stalled_decisions = 0
        else:
            stalled_decisions = previous_stalled_decisions + 1
        record["stalled_decisions"] = stalled_decisions

        summary = _result_summary(work_dir) if work_dir is not None else {
            "stop_reason": None, "accepted": 0, "rejected": 0,
            "submission_failed": 0, "unresolved": 0,
        }
        record["result_summary"] = dict(summary)
        worker, attempt_minutes, total_minutes = _worker_summary(
            workers.get(challenge_id, {}), record, now=now,
        )
        instance_id = instance.get("id") if instance is not None else None
        instance_record = instance_records.get(instance_id, {}) if instance_id is not None else {}
        created_by_campaign = instance_record.get(
            "created_by_campaign", record.get("created_by_campaign", False),
        ) is True
        row = {
            "id": challenge_id,
            "name": challenge.get("name"),
            "category": challenge.get("category"),
            "difficulty": challenge.get("difficulty"),
            "solves_count": challenge.get("solves_count"),
            "scoring": challenge.get("scoring"),
            "closed": challenge.get("closed") is True,
            "my_team_authored": challenge.get("my_team_authored") is True,
            "my_team_fully_solved": fully_solved,
            "my_team_solved_question_ids": team_ids,
            "platform_solved_question_ids": platform_ids,
            "questions": questions,
            "gated_questions": max(
                sum(not question["unlocked"] for question in questions),
                len(challenge.get("questions", []))
                - sum(question["unlocked"] for question in questions),
            ),
            "instance_id": instance_id,
            "instance_status": instance.get("status") if instance is not None else None,
            "instance_expires_at": instance.get("expires_at") if instance is not None else None,
            "created_by_campaign": created_by_campaign,
            "work_dir": str(work_dir) if work_dir is not None else None,
            "result_summary": summary,
            "worker": worker,
            "worker_status": worker["status"],
            "attempt_minutes": attempt_minutes,
            "total_minutes": total_minutes,
            "first_seen_at": first_seen_at,
            "last_progress_at": latest_progress,
            "stalled": stalled,
            "stalled_decisions": stalled_decisions,
            "unsupported": unsupported,
            "status": status,
        }
        rows.append(row)
    return rows


def _redact(value):
    if isinstance(value, str):
        for variable in ("CTFY_TOKEN", "LLM_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            secret = os.environ.get(variable)
            if secret:
                value = value.replace(secret, "[redacted]")
        return _SECRET_VALUE_PATTERN.sub("[redacted]", value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


def render_ctfy_board(rows: list[dict]) -> str:
    """Render compact JSON through a strict allowlist, never arbitrary row data."""
    safe_rows = [
        {key: row[key] for key in _BOARD_FIELDS if key in row}
        for row in rows
    ]
    return json.dumps(
        _redact(safe_rows), ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    )
