#!/usr/bin/env python3
"""Authoritative, redacted board projection for CTFY campaigns."""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import ctfy
import ctfy_campaign
import ctfy_local
import sapper


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

_MODEL_ERROR_RETRIES = 3
_INFRA_ERROR_RETRIES = 3
_BACKOFF_BASE = 10.0
_MAX_BACKOFF = 60.0
_DECISION_TIMEOUT = 120
_DECISION_RESULT_FIELDS = (
    "challenge_id", "status", "work_dir", "instance_id", "attempt_minutes",
    "closed", "preserved", "target", "cancel_after_slice", "workers",
)


def select_competition(competitions: list[dict], competition_id: str = "") -> dict:
    """Choose the one running, registered competition with visible challenges."""
    if not isinstance(competitions, list):
        raise ctfy.CtfyError("competition list must be a list")
    candidates = []
    for competition in competitions:
        if not isinstance(competition, dict):
            continue
        if competition.get("phase") != "running":
            continue
        if competition.get("is_registered") is not True:
            continue
        challenge_count = competition.get("challenge_count")
        if type(challenge_count) is not int or isinstance(challenge_count, bool) \
                or challenge_count <= 0:
            continue
        candidates.append(competition)
    if competition_id:
        competition_id = ctfy.require_id(competition_id, "competition_id")
        matches = [
            competition for competition in candidates
            if competition.get("id") == competition_id
        ]
        if len(matches) != 1:
            raise ctfy.CtfyError(
                f"competition {competition_id!r} is not a unique running, "
                "registered competition with visible challenges"
            )
        return matches[0]
    if len(candidates) != 1:
        names = ", ".join(
            f"{competition.get('id', '')} "
            f"({competition.get('title') or competition.get('name', '')})"
            for competition in candidates
        )
        raise ctfy.CtfyError(
            f"expected exactly one running registered competition with visible "
            f"challenges, found {len(candidates)}"
            + (f": {names}" if names else "")
            + ("; pass --competition to choose one" if candidates else "")
        )
    return candidates[0]


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
    progress_by_fact = {}
    facts_dir = work_dir / ".sapper" / "facts"
    try:
        paths = sorted(facts_dir.glob("*.json"))[:10000]
    except OSError:
        return None
    for path in paths:
        fact = _read_object(path, limit=_FACT_LIMIT)
        if fact.get("creator") in (None, "ctfy", "system"):
            continue
        data = fact.get("data")
        if isinstance(data, dict) and data.get("status") == "duplicate":
            continue
        created_at = _timestamp(fact.get("created_at"))
        if created_at is None:
            continue
        semantic_data = {
            key: value for key, value in data.items()
            if key not in {"status", "result"}
        } if isinstance(data, dict) else data
        identity = json.dumps(
            [fact.get("kind"), fact.get("description"), semantic_data],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        previous = progress_by_fact.get(identity)
        if previous is None or created_at < previous:
            progress_by_fact[identity] = created_at
    return max(progress_by_fact.values(), default=None)


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
                       questions: list[dict], *, gated_questions: int) -> tuple[list[str], list[str], bool]:
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
    if all_visible_answered and gated_questions == 0 \
            and len(source_questions) >= len(challenge.get("questions", [])):
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
        work_dir_value = record.get("work_dir")
        work_dir = Path(work_dir_value) if isinstance(work_dir_value, str) and work_dir_value else None
        task = _read_object(work_dir / "task.json") if work_dir is not None else {}
        task_gated_questions = task.get("gated_questions", 0)
        if type(task_gated_questions) is not int or task_gated_questions < 0:
            task_gated_questions = 0
        task_questions = task.get("questions")
        if isinstance(task_questions, list):
            task_gated_questions = max(
                0, len(task_questions) + task_gated_questions - len(questions),
            )
        gated_questions = max(
            sum(not question["unlocked"] for question in questions),
            len(challenge.get("questions", []))
            - sum(question["unlocked"] for question in questions),
            task_gated_questions,
        )
        team_ids, platform_ids, fully_solved = _platform_progress(
            challenge, instance, questions, gated_questions=gated_questions,
        )
        unsupported = _unsupported(challenge, instance, task)

        old_progress = _timestamp(record.get("last_progress_at"))
        fact_progress = _latest_fact_progress(work_dir) if work_dir is not None else None
        old_platform_ids = _strings(record.get("platform_solved_question_ids"))
        old_platform_id_set = set(old_platform_ids)
        new_platform_ids = [
            qid for qid in platform_ids if qid not in old_platform_id_set
        ]
        platform_changed = bool(new_platform_ids)
        fact_changed = fact_progress is not None \
            and (old_progress is None or fact_progress > old_progress)
        meaningful_progress = platform_changed or fact_changed
        latest_progress = old_progress
        for candidate in (fact_progress, now if platform_changed else None):
            if candidate is not None and (latest_progress is None or candidate > latest_progress):
                latest_progress = candidate
        record["last_progress_at"] = latest_progress
        record["platform_solved_question_ids"] = old_platform_ids + new_platform_ids

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
            "gated_questions": gated_questions,
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


@dataclass(frozen=True)
class Decision:
    action: str
    target: str
    minutes: int
    note: str


def _decision_target(rows: list[dict], target: str) -> dict:
    matches = [row for row in rows if row.get("id") == target]
    if len(matches) != 1:
        raise ctfy.CtfyError(f"decision target {target!r} is not a unique board id")
    return matches[0]


def _target_finished(row: dict) -> bool:
    return row.get("status") in {"solved", "closed", "unsupported"} \
        or row.get("closed") is True \
        or row.get("my_team_fully_solved") is True \
        or bool(row.get("unsupported"))


def _target_active(row: dict) -> bool:
    worker = row.get("worker", {})
    worker_status = worker.get("status") if isinstance(worker, dict) else None
    return row.get("status") == "active" \
        or row.get("worker_status", worker_status) in _ACTIVE_WORKERS


def parse_decision(
        payload: dict, rows: list[dict], *, now: float, deadline: float,
        default_minutes: int = 10, max_minutes: int = 30) -> Decision:
    """Validate one Sapper command against the authoritative CTFY board."""
    action, target, minutes, note = sapper._parse_command_payload(payload)
    if action in {"stop", "reject"}:
        return Decision(action, target, minutes, note)

    row = _decision_target(rows, target)
    if _target_finished(row):
        raise ctfy.CtfyError(f"cannot {action} finished or unsupported target {target!r}")
    if action == "fire":
        if not _target_active(row) and row.get("status") != "idle":
            raise ctfy.CtfyError(f"cannot fire non-active, non-idle target {target!r}")
        return Decision(action, target, 0, note)
    if _target_active(row):
        raise ctfy.CtfyError(f"cannot {action} already-active target {target!r}")

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if "minutes" not in data:
        minutes = default_minutes
    now = _number(now, float("nan"))
    deadline = _number(deadline, float("nan"))
    if not math.isfinite(now) or not math.isfinite(deadline):
        raise ctfy.CtfyError("decision time and deadline must be finite")

    usable_seconds = deadline - now
    expires_at = _timestamp(row.get("instance_expires_at"))
    if expires_at is not None:
        usable_seconds = min(usable_seconds, expires_at - now)
    if usable_seconds < 5 * 60:
        raise ctfy.CtfyError(f"target {target!r} has fewer than five usable minutes")

    if row.get("stalled") is True and not note.strip():
        raise ctfy.CtfyError(f"stalled target {target!r} needs a concrete decision note")
    minutes = max(5, min(int(minutes), int(max_minutes)))
    minutes = min(minutes, int(usable_seconds // 60))
    if minutes < 5:
        raise ctfy.CtfyError("decision slice must allow at least five minutes")
    return Decision(action, target, minutes, note)


def request_decision(
        rows: list[dict], *, deadline: float, model: str, timeout: int,
        template: Path, now: float) -> Decision:
    """Ask the commander model for one decision; never invent a fallback target."""
    prompt = Path(template).read_text(encoding="utf-8")
    remaining_minutes = max(0.0, (deadline - now) / 60)
    prompt = prompt.replace(
        "{time_budget}",
        f"{remaining_minutes:.1f} minutes remain; deadline is Unix {deadline:.3f}.",
    ).replace("{board}", render_ctfy_board(rows))
    envelope = sapper.run_claude(
        prompt, model=model, timeout=timeout, skip_permissions=False,
        append_system=None, cwd=Path.cwd(),
    )
    payload = sapper.extract_json_object(envelope.get("result", ""))
    return parse_decision(payload, rows, now=now, deadline=deadline)


class Commander:
    """Autonomous campaign loop: refresh, decide, execute, persist, repeat.

    AI decides strategy; deterministic code enforces targets, slices, capacity,
    and ownership. Model/API failures never start a new target: they back off
    with a bounded delay and then enter model_error/infra_error cleanup.
    """

    def __init__(self, store, client, competition, run_args, *,
                 model: str = "", template: Path | None = None,
                 poll_seconds: int = 30, decision_timeout: int = _DECISION_TIMEOUT,
                 pool=None):
        self.store = store
        self.client = client
        self.competition = competition
        self.competition_id = ctfy.require_id(competition.get("id"), "competition_id")
        self.run_args = run_args
        self.model = model or os.environ.get("LLM_MODEL") \
            or os.environ.get("SAPPER_MODEL", "")
        if not self.model:
            raise ctfy.CtfyError(
                "campaign needs LLM_MODEL or SAPPER_MODEL to decide strategy",
            )
        self.template = Path(template) if template is not None else \
            Path(__file__).resolve().parent.parent / "templates" / "ctfy" / "command.md"
        self.poll_seconds = poll_seconds
        self.decision_timeout = decision_timeout
        self.pool = pool if pool is not None else WorkerPool(
            store, client, competition, run_args,
        )
        self._interrupts = 0

    def interrupt(self) -> None:
        """Record an interrupt request; the main loop performs the cleanup."""
        self._interrupts += 1

    def _cleanup_interrupts(self) -> int:
        """Drain pending interrupts in the main flow, never in a handler.

        One cleanup per recorded interrupt (capped at two) preserves the pool's
        contract: the first interrupt waits gracefully and preserves instances,
        while a second interrupt follows the bounded forced-exit path.
        """
        handled = 0
        while self._interrupts > 0 and handled < 2:
            self._interrupts -= 1
            self.pool.cleanup(
                "interrupt", interrupt_pending=lambda: self._interrupts > 0,
            )
            handled += 1
        return 130

    def _deadline(self) -> float:
        deadline = _timestamp(self.store.load().get("deadline"))
        if deadline is None:
            raise ctfy.CtfyError("campaign manifest has no valid deadline")
        return deadline

    def _refresh(self):
        return self.client.list_challenges(self.competition_id), \
            self.client.list_instances()

    def _decision_timeout(self, deadline: float, now: float) -> int:
        remaining = max(1, int(deadline - now))
        return max(1, min(self.decision_timeout, remaining))

    def _backoff(self, remaining: float, attempts: int) -> None:
        delay = min(_MAX_BACKOFF, _BACKOFF_BASE * (2 ** (attempts - 1)))
        if remaining <= 0:
            return
        time.sleep(min(delay, max(0.0, remaining)))

    def _poll(self, deadline: float) -> None:
        if self.poll_seconds <= 0:
            return
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(float(self.poll_seconds), remaining))

    @staticmethod
    def _no_viable_work(rows: list[dict]) -> bool:
        return not any(
            row.get("status") in {"idle", "parked", "active"} for row in rows
        )

    @staticmethod
    def _challenge_by_id(challenges: list[dict], target: str) -> dict:
        matches = [item for item in challenges if item.get("id") == target]
        if len(matches) != 1:
            raise ctfy.CtfyError(
                f"decision target {target!r} is not a unique platform challenge",
            )
        return matches[0]

    def _execute(self, decision: Decision, challenges: list[dict]):
        if decision.action == "stop":
            return {"stopped": True}, None
        if decision.action == "reject":
            return {"rejected": True}, None
        if decision.action == "fire":
            try:
                return self.pool.fire(decision.target), None
            except Exception as exc:  # noqa: BLE001 - record and continue the loop
                return None, _error_record(exc)
        try:
            challenge = self._challenge_by_id(challenges, decision.target)
        except ctfy.CtfyError as exc:
            return None, _error_record(exc)
        try:
            return self.pool.start(
                decision, challenge, background=decision.action == "hire",
            ), None
        except Exception as exc:  # noqa: BLE001 - record and continue the loop
            return None, _error_record(exc)

    def _record_decision(self, decision: Decision, now: float, outcome, error) -> None:
        result = None
        if isinstance(outcome, dict):
            result = {
                key: outcome[key] for key in _DECISION_RESULT_FIELDS if key in outcome
            }
        record = {
            "at": now,
            "action": decision.action,
            "target": decision.target,
            "minutes": decision.minutes,
            "note": _redact(decision.note),
        }
        if result:
            record["result"] = _redact(result)
        if error:
            record["error"] = error
        self.store.append_decision(record)

    def run(self) -> int:
        deadline = self._deadline()
        consecutive_model = 0
        consecutive_infra = 0
        while True:
            if self._interrupts:
                return self._cleanup_interrupts()
            now = time.time()
            if now >= deadline:
                self.pool.cleanup("deadline")
                return 0
            try:
                challenges, instances = self._refresh()
            except Exception:  # noqa: BLE001 - platform unavailability backoff
                consecutive_infra += 1
                consecutive_model = 0
                if self._interrupts:
                    return self._cleanup_interrupts()
                if consecutive_infra >= _INFRA_ERROR_RETRIES:
                    self.pool.cleanup("infra_error")
                    return 2
                self._backoff(deadline - now, consecutive_infra)
                continue
            consecutive_infra = 0
            if self._interrupts:
                return self._cleanup_interrupts()
            self.pool.reap()
            state = self.store.load()
            rows = build_board(challenges, instances, state, now=now)
            self.store.save(state)
            if self._no_viable_work(rows):
                self.pool.cleanup("stop")
                return 0
            try:
                decision = request_decision(
                    rows, deadline=deadline, model=self.model,
                    timeout=self._decision_timeout(deadline, now),
                    template=self.template, now=now,
                )
            except Exception:  # noqa: BLE001 - model unavailability backoff
                consecutive_model += 1
                if self._interrupts:
                    return self._cleanup_interrupts()
                if consecutive_model >= _MODEL_ERROR_RETRIES:
                    self.pool.cleanup("model_error")
                    return 2
                self._backoff(deadline - now, consecutive_model)
                continue
            consecutive_model = 0
            if self._interrupts:
                return self._cleanup_interrupts()
            outcome, error = self._execute(decision, challenges)
            self._record_decision(decision, now, outcome, error)
            if decision.action == "stop":
                self.pool.cleanup("stop")
                return 0
            self._poll(deadline)


_WORKER_RUNNING_STATUSES = {"ready", "running"}
_WORKER_STARTING_STATUSES = {"pending", "starting", "creating"}
_OUTCOME_LIMIT = 16 * 1024 * 1024
_CLEANUP_REASONS = {"stop", "deadline", "interrupt", "model_error", "infra_error"}


def _atomic_json(path: Path, value: dict) -> None:
    """Atomically persist one per-attempt record; attempt files may contain answers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _error_record(exc: BaseException) -> dict:
    message = _redact(str(exc))[:500]
    record = {"message": message or exc.__class__.__name__}
    if isinstance(exc, ctfy.CtfyError):
        if exc.code is not None:
            record["code"] = _redact(str(exc.code))[:100]
        if exc.status is not None:
            record["status"] = exc.status
        record["transport"] = exc.transport is True
    return record


def _pid_matches(pid, *, store_root: Path, challenge_id: str, attempt: Path) -> bool:
    """Verify a Linux worker command line; a PID by itself is never authority."""
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()[:65536]
    except (OSError, PermissionError):
        return False
    argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    if "worker" not in argv or not any(
            Path(part).name == "ctfy_commander.py" for part in argv):
        return False

    def option_value(name):
        try:
            return argv[argv.index(name) + 1]
        except (ValueError, IndexError):
            return None

    return option_value("--store") == str(store_root) \
        and option_value("--challenge-id") == challenge_id \
        and option_value("--attempt") == str(attempt)


def _active_worker(worker: dict) -> bool:
    return isinstance(worker, dict) and worker.get("status") in _ACTIVE_WORKERS


def _attempt_path(worker: dict, record: dict) -> Path | None:
    value = worker.get("attempt_dir") or record.get("work_dir")
    return Path(value) if isinstance(value, str) and value else None


class WorkerPool:
    """Process-based single-challenge workers with deterministic safety limits."""

    def __init__(self, store, client, competition, run_args):
        self.store = store
        self.client = client
        self.competition = competition
        self.competition_id = ctfy.require_id(competition.get("id"), "competition_id")
        self.run_args = dict(vars(run_args)) if hasattr(run_args, "__dict__") \
            else dict(run_args)
        self._processes = {}
        self._adopted = set()
        self._accepting = True
        self._cleanup_reason = None
        self._interrupts = 0
        self._capacity_blocked = False
        self._last_instances = []

    def _competition_instances(self, instances):
        return [
            item for item in instances if isinstance(item, dict)
            and item.get("competition_id") in (None, "", self.competition_id)
        ]

    def _matching_instances(self, instances, challenge_id):
        matches = [
            item for item in self._competition_instances(instances)
            if item.get("challenge_id") == challenge_id
            and item.get("status", "ready") in _WORKER_RUNNING_STATUSES
        ]
        if len(matches) > 1:
            raise ctfy.CtfyError(
                f"multiple ready/running instances for {challenge_id}; fail closed",
            )
        return matches[0] if matches else None

    def _active_instances(self, instances):
        statuses = _WORKER_RUNNING_STATUSES | _WORKER_STARTING_STATUSES
        return [
            item for item in self._competition_instances(instances)
            if item.get("status", "ready") in statuses
        ]

    def _refresh_instances(self):
        self._last_instances = self.client.list_instances()
        return self._last_instances

    def _instance_capacity(self):
        value = self.competition.get("max_instances_per_team_effective")
        return value if type(value) is int and value > 0 else 1

    def _reported_capacity(self):
        value = self.competition.get("max_instances_per_team_effective")
        return type(value) is int and value > 0

    def _sync_instances(self, state, instances):
        records = state.setdefault("instances", {})
        for item in self._competition_instances(instances):
            instance_id = item.get("id")
            if not isinstance(instance_id, str) or not instance_id:
                continue
            record = records.setdefault(instance_id, {
                "created_by_campaign": False,
            })
            record["challenge_id"] = item.get("challenge_id")
            if isinstance(item.get("status"), str):
                record["status"] = item["status"][:40]
            expires_at = item.get("expires_at")
            if isinstance(expires_at, str) and len(expires_at) <= 100:
                record["expires_at"] = expires_at
            elif isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool) \
                    and math.isfinite(expires_at):
                record["expires_at"] = expires_at

    def _refresh_platform(self, challenge_id):
        current = ctfy_local.get_challenge_for_run(
            self.client, challenge_id, self.competition_id,
        )
        instances = self.client.list_instances()
        self._matching_instances(instances, challenge_id)
        self._last_instances = instances
        return current, instances

    @staticmethod
    def _record_platform_progress(record, current):
        solved = _strings(current.get("my_team_solved_question_ids"))
        solved.extend(_strings(current.get("my_solved_question_ids")))
        for question in current.get("questions", []):
            if isinstance(question, dict) and question.get("answered_correctly") is True \
                    and isinstance(question.get("id"), str):
                solved.append(question["id"])
        previous = _strings(record.get("platform_solved_question_ids"))
        record["platform_solved_question_ids"] = list(dict.fromkeys(previous + solved))
        record["platform_fully_solved"] = record.get("platform_fully_solved") is True \
            or current.get("my_team_fully_solved") is True \
            or current.get("my_fully_solved") is True

    def _new_attempt(self, challenge_id):
        parent = self.store.root / challenge_id
        parent.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="attempt-", dir=parent))

    def _choose_attempt(self, record, challenge, matching):
        previous = record.get("work_dir")
        if isinstance(previous, str) and previous:
            attempt = Path(previous)
            try:
                attempt.resolve().relative_to(self.store.root.resolve())
            except (OSError, ValueError):
                attempt = None
            if attempt is not None:
                task = _read_object(attempt / "task.json", limit=_OUTCOME_LIMIT)
                if challenge.get("is_qa_only") is True \
                        and task.get("run_mode") == "qa":
                    return attempt
                if matching is not None \
                        and task.get("target_instance") == matching.get("id"):
                    return attempt
        return self._new_attempt(challenge["id"])

    def _worker_config(self, challenge_id, decision, attempt):
        engine = self.run_args.get("engine")
        add_host = self.run_args.get("add_host", [])
        return {
            "platform_url": self.run_args.get(
                "platform_url", os.environ.get("CTFY_PLATFORM_URL", ""),
            ),
            "competition": self.competition_id,
            "challenge_id": challenge_id,
            "work_dir": str(attempt),
            "image": self.run_args.get("image", "sapper-ctfy:local-test"),
            "engine": str(engine) if engine is not None else None,
            "add_host": list(add_host) if isinstance(add_host, (list, tuple)) else [],
            "minutes": decision.minutes,
            "max_turns": self.run_args.get("max_turns", 12),
            "dispatch_timeout": self.run_args.get("dispatch_timeout", 300),
            "startup_timeout": self.run_args.get("startup_timeout", 300),
            "ttl": self.run_args.get("ttl"),
            "prepare_only": False,
        }

    def start(self, decision: Decision, challenge: dict, *, background: bool) -> dict:
        if not self._accepting:
            raise ctfy.CtfyError("worker pool is not accepting new work")
        challenge_id = ctfy.require_id(challenge.get("id"), "challenge_id")
        if decision.action not in {"work", "hire"} or decision.target != challenge_id:
            raise ctfy.CtfyError("worker decision does not match its challenge")
        self.reap()
        self.recover()
        if self._capacity_blocked:
            raise ctfy.CtfyError("platform capacity rejection is paused pending reconciliation")
        instances = self._last_instances
        matching = self._matching_instances(instances, challenge_id)
        state = self.store.load()
        workers = state.setdefault("workers", {})
        now = time.time()
        deadline = _timestamp(state.get("deadline"))
        usable_seconds = deadline - now if deadline is not None else float("nan")
        if challenge.get("is_qa_only") is not True and matching is not None:
            expires_at = _timestamp(matching.get("expires_at"))
            if expires_at is not None:
                usable_seconds = min(usable_seconds, expires_at - now)
        if type(decision.minutes) is not int or decision.minutes <= 0 \
                or not math.isfinite(usable_seconds) or usable_seconds < 5 * 60:
            raise ctfy.CtfyError("worker decision has no viable bounded time slice")
        minutes = max(5, min(decision.minutes, 30, int(usable_seconds // 60)))
        decision = Decision(decision.action, decision.target, minutes, decision.note)
        if _active_worker(workers.get(challenge_id, {})):
            raise ctfy.CtfyError(f"challenge {challenge_id!r} already has an active worker")
        active_workers = {
            target: worker for target, worker in workers.items()
            if _active_worker(worker)
        }
        max_workers = state.get("max_workers", 1)
        max_workers = max_workers if type(max_workers) is int and max_workers > 0 else 1
        if len(active_workers) >= max_workers:
            raise ctfy.CtfyError("campaign worker capacity is exhausted")

        if challenge.get("is_qa_only") is not True:
            active_instances = self._active_instances(instances)
            capacity = self._instance_capacity()
            if len(active_instances) > capacity \
                    or (matching is None and len(active_instances) >= capacity):
                raise ctfy.CtfyError("platform instance capacity is exhausted")
            if not self._reported_capacity() and any(
                    target != challenge_id and worker.get("is_qa") is not True
                    for target, worker in active_workers.items()
            ):
                raise ctfy.CtfyError(
                    "instance capacity is unknown; parallel instance work is disabled",
                )

        records = state.setdefault("challenges", {})
        record = records.setdefault(challenge_id, {})
        attempt = self._choose_attempt(record, challenge, matching)
        config = self._worker_config(challenge_id, decision, attempt)
        _atomic_json(attempt / "worker.json", config)
        argv = [
            sys.executable, str(Path(__file__).resolve()), "worker",
            "--store", str(self.store.root.resolve()),
            "--challenge-id", challenge_id,
            "--attempt", str(attempt.resolve()),
        ]
        try:
            with (attempt / "worker.log").open("ab") as log:
                process = subprocess.Popen(
                    argv, stdout=log, stderr=subprocess.STDOUT,
                    env=os.environ.copy(), shell=False,
                )
        except OSError as exc:
            raise ctfy.CtfyError(f"cannot launch campaign worker: {_redact(str(exc))}") from exc

        started_at = time.time()
        worker = {
            "status": "starting", "pid": process.pid,
            "attempt_dir": str(attempt), "started_at": started_at,
            "minutes": decision.minutes, "background": background is True,
            "cancel_after_slice": False,
            "is_qa": challenge.get("is_qa_only") is True,
        }
        workers[challenge_id] = worker
        record["work_dir"] = str(attempt)
        record["parked"] = False
        if matching is not None:
            self._sync_instances(state, [matching])
        self.store.save(state)
        self._processes[challenge_id] = process
        if background:
            return dict(worker)

        process.wait()
        completed = [item for item in self.reap() if item["challenge_id"] == challenge_id]
        if completed:
            return completed[0]
        return dict(self.store.load().get("workers", {}).get(challenge_id, worker))

    def _ingest_events(self, state):
        changed = False
        workers = state.setdefault("workers", {})
        records = state.setdefault("challenges", {})
        for target, worker in workers.items():
            if not _active_worker(worker):
                continue
            record = records.setdefault(target, {})
            attempt = _attempt_path(worker, record)
            if attempt is None:
                continue
            prepared = _read_object(attempt / "prepared.json", limit=_OUTCOME_LIMIT)
            if prepared:
                work_dir = prepared.get("work_dir")
                if isinstance(work_dir, str) and work_dir:
                    record["work_dir"] = work_dir
                instance_id = prepared.get("instance_id")
                if isinstance(instance_id, str) and instance_id:
                    instance = state.setdefault("instances", {}).setdefault(instance_id, {})
                    instance["challenge_id"] = target
                    instance["created_by_campaign"] = False \
                        if worker.get("ownership_unknown") is True \
                        else prepared.get("created_instance") is True
                    if isinstance(prepared.get("work_dir"), str):
                        instance["work_dir"] = prepared["work_dir"]
                if worker.get("status") == "starting":
                    worker["status"] = "prepared"
                changed = True
            container = _read_object(attempt / "container.json")
            name = container.get("name")
            if isinstance(name, str) and name and worker.get("container") != name:
                worker["container"] = name[:200]
                worker["status"] = "running"
                changed = True
        if changed:
            self.store.save(state)
        return state

    def _ensure_attempt_result(self, attempt, outcome):
        result = outcome.get("result")
        if isinstance(result, dict) and not (attempt / "result.json").exists():
            _atomic_json(attempt / "result.json", result)
        handoff = outcome.get("handoff")
        if isinstance(handoff, list) and not (attempt / "handoff.json").exists():
            _atomic_json(attempt / "handoff.json", {"submissions": handoff})

    def _final_handoff(self, attempt):
        task = _read_object(attempt / "task.json", limit=_OUTCOME_LIMIT)
        result = _read_object(attempt / "result.json", limit=_OUTCOME_LIMIT)
        if not task or not result:
            return []
        return ctfy_local.submit_handed_off_answers(
            self.client, task, result, attempt,
        )

    def _close_instance(self, instance_id) -> bool:
        try:
            self.client.stop_instance(instance_id)
        except (ctfy.CtfyError, OSError) as exc:
            state = self.store.load()
            record = state.setdefault("instances", {}).setdefault(instance_id, {
                "created_by_campaign": True,
            })
            record["error"] = _error_record(exc)
            self.store.save(state)
            return False
        state = self.store.load()
        record = state.setdefault("instances", {}).setdefault(instance_id, {
            "created_by_campaign": True,
        })
        record["closed"] = True
        record["status"] = "stopped"
        self.store.save(state)
        return True

    def _finalize(self, target, *, outcome=None, recovery=False, error=None):
        state = self.store.load()
        worker = state.setdefault("workers", {}).get(target)
        if not isinstance(worker, dict):
            return None
        record = state.setdefault("challenges", {}).setdefault(target, {})
        attempt = _attempt_path(worker, record)
        if attempt is None:
            worker["status"] = "failed"
            worker["error"] = {"message": "worker has no attempt directory"}
            self.store.save(state)
            return {"challenge_id": target, "status": "failed", "closed": []}
        record["work_dir"] = str(attempt)
        if outcome is None:
            outcome = _read_object(attempt / "outcome.json", limit=_OUTCOME_LIMIT)
        prepared = _read_object(attempt / "prepared.json", limit=_OUTCOME_LIMIT)
        self._ensure_attempt_result(attempt, outcome)
        errors = []
        try:
            self._final_handoff(attempt)
        except (ctfy.CtfyError, OSError) as exc:
            errors.append(_error_record(exc))
        current = None
        instances = []
        try:
            current, instances = self._refresh_platform(target)
        except (ctfy.CtfyError, OSError) as exc:
            errors.append(_error_record(exc))

        status = outcome.get("status")
        if status not in {"completed", "failed", "interrupted"}:
            process = self._processes.get(target)
            returncode = process.poll() if process is not None else None
            status = "completed" if returncode == 0 and outcome else "failed"
        if error is not None:
            status = "interrupted"
            outcome = {**outcome, "error": error}
        ended_at = time.time()
        started_at = _timestamp(worker.get("started_at"))
        attempt_minutes = max(0.0, (ended_at - started_at) / 60) \
            if started_at is not None else _number(worker.get("attempt_minutes", 0))
        worker["status"] = status
        worker["ended_at"] = ended_at
        worker["attempt_minutes"] = attempt_minutes
        if not worker.get("time_accounted"):
            record["total_minutes"] = _number(record.get("total_minutes", 0)) + attempt_minutes
            worker["time_accounted"] = True
        if isinstance(outcome.get("error"), dict):
            outcome_error = outcome["error"]
            worker["error"] = _redact(outcome_error)
            capacity_text = " ".join(str(outcome_error.get(key, "")) for key in (
                "code", "message",
            )).lower()
            if any(marker in capacity_text for marker in (
                    "capacity", "instance_limit", "max_instances", "quota",
                    "resource_limit", "too many instances",
            )):
                self._capacity_blocked = True
        elif errors:
            worker["error"] = errors[-1]
        summary = _result_summary(attempt)
        record["result_summary"] = summary
        record["handoff_summary"] = {
            key: value for key, value in summary.items() if key != "stop_reason"
        }
        if current is not None:
            self._record_platform_progress(record, current)
        self._sync_instances(state, instances)

        instance_id = outcome.get("instance_id") or prepared.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            task = _read_object(attempt / "task.json", limit=_OUTCOME_LIMIT)
            instance_id = task.get("target_instance")
        if isinstance(instance_id, str) and instance_id:
            instance_records = state.setdefault("instances", {})
            instance = instance_records.get(instance_id)
            if instance is None:
                instance = instance_records.setdefault(instance_id, {})
                instance["created_by_campaign"] = False if recovery \
                    else prepared.get("created_instance", outcome.get("created_instance")) is True
            elif not recovery:
                instance["created_by_campaign"] = False \
                    if worker.get("ownership_unknown") is True else prepared.get(
                        "created_instance", outcome.get(
                            "created_instance", instance.get("created_by_campaign"),
                        ),
                    ) is True
            instance["challenge_id"] = target
            instance["work_dir"] = str(attempt)

        owned = isinstance(instance_id, str) and instance_id \
            and state.get("instances", {}).get(instance_id, {}).get(
                "created_by_campaign") is True
        preserving = self._cleanup_reason in {"interrupt", "model_error", "infra_error"}
        close = bool(owned and not errors and not preserving and (
            worker.get("cancel_after_slice") is True
            or self._cleanup_reason in {"stop", "deadline"}
        ))
        if close:
            state["instances"][instance_id]["close_requested"] = True
        self.store.save(state)
        closed = []
        if close and self._close_instance(instance_id):
            closed.append(instance_id)
        self._processes.pop(target, None)
        self._adopted.discard(target)
        return {
            "challenge_id": target, "status": status, "work_dir": str(attempt),
            "instance_id": instance_id if isinstance(instance_id, str) else None,
            "attempt_minutes": attempt_minutes, "closed": closed,
        }

    def reap(self) -> list[dict]:
        state = self.store.load()
        self._ingest_events(state)
        state = self.store.load()
        results = []
        for target, worker in list(state.get("workers", {}).items()):
            if not _active_worker(worker):
                continue
            record = state.get("challenges", {}).get(target, {})
            attempt = _attempt_path(worker, record)
            process = self._processes.get(target)
            outcome_path = attempt / "outcome.json" if attempt is not None else None
            outcome_exists = outcome_path is not None and outcome_path.is_file()
            if process is not None:
                if process.poll() is None and not outcome_exists:
                    continue
            elif target in self._adopted and not outcome_exists:
                continue
            elif not outcome_exists:
                continue
            outcome = _read_object(outcome_path, limit=_OUTCOME_LIMIT) \
                if outcome_path is not None else {}
            error = None
            if not outcome:
                error = {"message": "worker exited without an outcome record"}
            result = self._finalize(target, outcome=outcome, error=error)
            if result is not None:
                results.append(result)
        return results

    def fire(self, target: str) -> dict:
        target = ctfy.require_id(target, "challenge_id")
        self._capacity_blocked = False
        self.reap()
        state = self.store.load()
        worker = state.setdefault("workers", {}).get(target)
        record = state.setdefault("challenges", {}).setdefault(target, {})
        record["parked"] = True
        if _active_worker(worker):
            worker["cancel_after_slice"] = True
            self.store.save(state)
            return {
                "target": target, "status": worker["status"],
                "cancel_after_slice": True, "closed": [], "preserved": [],
            }
        previous = worker if isinstance(worker, dict) else {}
        state["workers"][target] = {
            **previous, "status": "parked", "cancel_after_slice": True,
        }
        self.store.save(state)
        settled = self._settle_idle(target, close=True)
        return {"target": target, "status": "parked", **settled}

    def recover(self) -> dict:
        instances = self._refresh_instances()
        state = self.store.load()
        recovered = {"adopted": [], "reconciled": [], "errors": []}
        candidates = {
            target for target, worker in state.get("workers", {}).items()
            if _active_worker(worker)
        }
        candidates.update(
            target for target, record in state.get("challenges", {}).items()
            if isinstance(record, dict) and isinstance(record.get("work_dir"), str)
        )
        for target in candidates:
            try:
                matching = self._matching_instances(instances, target)
            except ctfy.CtfyError as exc:
                recovered["errors"].append(_error_record(exc))
                continue
            if matching is not None and isinstance(matching.get("id"), str):
                instance_records = state.setdefault("instances", {})
                if matching["id"] not in instance_records \
                        and target not in self._processes \
                        and _active_worker(state.get("workers", {}).get(target, {})):
                    state["workers"][target]["ownership_unknown"] = True
                instance = instance_records.setdefault(matching["id"], {})
                instance.setdefault("created_by_campaign", False)
                instance["challenge_id"] = target
        self.store.save(state)

        for target in sorted(candidates):
            state = self.store.load()
            worker = state.get("workers", {}).get(target, {})
            if not _active_worker(worker):
                continue
            record = state.get("challenges", {}).get(target, {})
            attempt = _attempt_path(worker, record)
            if attempt is not None and (attempt / "outcome.json").is_file():
                result = self._finalize(target, recovery=True)
                if result is not None:
                    recovered["reconciled"].append(target)
                continue
            process = self._processes.get(target)
            if process is not None and process.poll() is None:
                continue
            if attempt is not None and _pid_matches(
                    worker.get("pid"), store_root=self.store.root.resolve(),
                    challenge_id=target, attempt=attempt.resolve(),
            ):
                self._adopted.add(target)
                recovered["adopted"].append(target)
                continue
            error = {"message": "recorded worker PID is stale or has an unknown command line"}
            if attempt is not None:
                outcome = _read_object(attempt / "outcome.json", limit=_OUTCOME_LIMIT)
                outcome.update({
                    "status": "interrupted", "work_dir": str(attempt), "error": error,
                })
                _atomic_json(attempt / "outcome.json", outcome)
            result = self._finalize(target, recovery=True, error=error)
            if result is not None:
                recovered["reconciled"].append(target)
        self.reap()
        return recovered

    def _settle_idle(self, target, *, close):
        state = self.store.load()
        record = state.setdefault("challenges", {}).setdefault(target, {})
        attempt_value = record.get("work_dir")
        attempt = Path(attempt_value) if isinstance(attempt_value, str) and attempt_value else None
        errors = []
        if attempt is not None:
            try:
                self._final_handoff(attempt)
            except (ctfy.CtfyError, OSError) as exc:
                errors.append(_error_record(exc))
        current = None
        instances = []
        try:
            current, instances = self._refresh_platform(target)
        except (ctfy.CtfyError, OSError) as exc:
            errors.append(_error_record(exc))
        state = self.store.load()
        record = state.setdefault("challenges", {}).setdefault(target, {})
        if attempt is not None:
            summary = _result_summary(attempt)
            record["result_summary"] = summary
            record["handoff_summary"] = {
                key: value for key, value in summary.items() if key != "stop_reason"
            }
        if current is not None:
            self._record_platform_progress(record, current)
        self._sync_instances(state, instances)
        candidates = [
            instance_id for instance_id, instance in state.get("instances", {}).items()
            if instance.get("challenge_id") == target
            and instance.get("created_by_campaign") is True
            and instance.get("closed") is not True
        ]
        if close and not errors:
            for instance_id in candidates:
                state["instances"][instance_id]["close_requested"] = True
        self.store.save(state)
        closed = []
        if close and not errors:
            for instance_id in candidates:
                if self._close_instance(instance_id):
                    closed.append(instance_id)
        return {"closed": closed, "preserved": []}

    @staticmethod
    def _remaining_slice(worker):
        started = _timestamp(worker.get("started_at"))
        minutes = _number(worker.get("minutes", 0))
        if started is None or minutes <= 0:
            return 1.0
        return max(0.01, started + minutes * 60 + 30 - time.time())

    def _force_process(self, target, process, worker):
        container = worker.get("container")
        if isinstance(container, str) and container:
            _docker_cleanup(container)
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            return
        if target in self._adopted:
            try:
                os.kill(worker["pid"], signal.SIGTERM)
            except (KeyError, OSError, ProcessLookupError):
                pass

    def _preserved_instances(self):
        state = self.store.load()
        preserved = []
        for instance_id, record in sorted(state.get("instances", {}).items()):
            if record.get("closed") is True or record.get("status") in {
                    "stopped", "expired", "failed", "error"}:
                continue
            preserved.append({
                "id": instance_id, "expires_at": record.get("expires_at"),
            })
        return preserved

    def _wait_worker(self, target, process, worker, interrupt_pending):
        """Wait for one worker to finish its slice, escalating on a 2nd interrupt.

        Without an interrupt predicate this is the original single bounded wait.
        With one, the wait polls in short slices and force-terminates as soon as
        a second interrupt is recorded, so a signal sent only to the commander
        PID still reaches the bounded forced-exit path promptly.
        """
        if interrupt_pending is None:
            try:
                process.wait(timeout=self._remaining_slice(worker))
            except subprocess.TimeoutExpired:
                pass
            return False
        deadline = time.time() + self._remaining_slice(worker)
        while process.poll() is None and time.time() < deadline:
            if interrupt_pending():
                self._force_process(target, process, worker)
                return True
            time.sleep(min(1.0, max(0.0, deadline - time.time())))
        return False

    def cleanup(self, reason: str, *, interrupt_pending=None) -> dict:
        if reason not in _CLEANUP_REASONS:
            raise ValueError(f"unsupported cleanup reason {reason!r}")
        self._accepting = False
        self._cleanup_reason = reason
        if reason == "interrupt":
            self._interrupts += 1
        force = reason == "deadline" or (reason == "interrupt" and self._interrupts > 1)
        closed = [
            instance_id for item in self.reap() for instance_id in item["closed"]
        ]
        state = self.store.load()
        active = []
        for target, worker in state.get("workers", {}).items():
            if _active_worker(worker):
                worker["cancel_after_slice"] = True
                active.append((target, worker))
        if active:
            self.store.save(state)
        for target, worker in active:
            process = self._processes.get(target)
            if force:
                self._force_process(target, process, worker)
            elif process is not None and process.poll() is None:
                if self._wait_worker(target, process, worker, interrupt_pending):
                    force = True
        closed.extend(
            instance_id for item in self.reap() for instance_id in item["closed"]
        )
        state = self.store.load()
        targets = set(state.get("challenges", {}))
        targets.update(
            instance.get("challenge_id") for instance in state.get("instances", {}).values()
            if isinstance(instance.get("challenge_id"), str)
        )
        for target in sorted(targets):
            if _active_worker(state.get("workers", {}).get(target, {})):
                continue
            settled = self._settle_idle(target, close=reason in {"stop", "deadline"})
            closed.extend(settled["closed"])
        try:
            instances = self._refresh_instances()
            state = self.store.load()
            self._sync_instances(state, instances)
            self.store.save(state)
        except ctfy.CtfyError:
            pass
        return {
            "reason": reason,
            "closed": list(dict.fromkeys(closed)),
            "preserved": self._preserved_instances(),
            "workers": {
                target: worker.get("status")
                for target, worker in self.store.load().get("workers", {}).items()
            },
        }


def _docker_cleanup(name):
    try:
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"ctfy-worker: container cleanup failed: {_redact(str(exc))}\n")


class _WorkerInterrupted(BaseException):
    pass


def _worker_namespace(config, challenge_id, attempt):
    engine = config.get("engine")
    return argparse.Namespace(
        challenge_id=challenge_id,
        competition=config.get("competition"),
        work_dir=attempt,
        platform_url=config.get("platform_url", ""),
        image=config.get("image", "sapper-ctfy:local-test"),
        engine=Path(engine) if isinstance(engine, str) and engine else None,
        add_host=list(config.get("add_host", [])),
        minutes=int(config.get("minutes", 10)),
        max_turns=int(config.get("max_turns", 12)),
        dispatch_timeout=int(config.get("dispatch_timeout", 300)),
        startup_timeout=int(config.get("startup_timeout", 300)),
        ttl=config.get("ttl"),
        prepare_only=False,
        keep_instance=True,
    )


def worker_main(argv: list[str] | None = None) -> int:
    """Run one locked campaign worker and checkpoint lifecycle events atomically."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "worker":
        argv = argv[1:]
    parser = argparse.ArgumentParser(description="one CTFY campaign worker")
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--challenge-id", required=True)
    parser.add_argument("--attempt", required=True, type=Path)
    options = parser.parse_args(argv)
    challenge_id = ctfy.require_id(options.challenge_id, "challenge_id")
    store = ctfy_campaign.CampaignStore(options.store)
    attempt = options.attempt.resolve()
    try:
        attempt.relative_to((store.root / challenge_id).resolve())
    except ValueError:
        raise ctfy.CtfyError("worker attempt must belong to its campaign challenge") from None
    config = _read_object(attempt / "worker.json", limit=_METADATA_LIMIT)
    if not config or config.get("challenge_id") not in (None, challenge_id):
        raise ctfy.CtfyError("worker configuration is missing or belongs to another challenge")
    if config.get("work_dir") and Path(config["work_dir"]).resolve() != attempt:
        raise ctfy.CtfyError("worker configuration cannot redirect to another attempt")
    args = _worker_namespace(config, challenge_id, attempt)
    prepared = {}
    container = {}
    interrupted = {"requested": False}
    old_handlers = {}

    def request_interrupt(signum, frame):
        if interrupted["requested"]:
            raise KeyboardInterrupt()
        interrupted["requested"] = True
        raise _WorkerInterrupted()

    def on_event(event, data):
        nonlocal prepared, container
        if event == "prepared":
            prepared = dict(data)
        elif event == "container":
            container = dict(data)
        _atomic_json(attempt / f"{event}.json", data)

    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_interrupt)
    try:
        client = ctfy.CtfyClient(
            args.platform_url, os.environ.get("CTFY_TOKEN", ""),
        )
        with store.challenge_lock(challenge_id):
            outcome = ctfy_local.run_challenge(
                args, client=client, on_event=on_event,
            )
        outcome = {**outcome, "status": "completed"}
        _atomic_json(attempt / "outcome.json", outcome)
        returncode = outcome.get("container_exit_code")
        return returncode if type(returncode) is int else 0
    except (_WorkerInterrupted, KeyboardInterrupt):
        name = container.get("name")
        if isinstance(name, str) and name:
            _docker_cleanup(name)
        outcome = {
            "status": "interrupted", "stop_reason": "interrupted",
            "work_dir": str(attempt),
            "instance_id": prepared.get("instance_id"),
            "created_instance": prepared.get("created_instance", False),
            "prepare_only": False, "container_exit_code": None,
            "result": None, "handoff": [],
        }
        _atomic_json(attempt / "outcome.json", outcome)
        return 130
    except (ctfy.CtfyError, OSError, subprocess.SubprocessError) as exc:
        name = container.get("name")
        if isinstance(name, str) and name:
            _docker_cleanup(name)
        outcome = {
            "status": "failed", "work_dir": str(attempt),
            "instance_id": prepared.get("instance_id"),
            "created_instance": prepared.get("created_instance", False),
            "prepare_only": False, "container_exit_code": None,
            "result": None, "handoff": [], "error": _error_record(exc),
        }
        _atomic_json(attempt / "outcome.json", outcome)
        sys.stderr.write(f"ctfy-worker: {_redact(str(exc))}\n")
        return 2
    except Exception as exc:
        name = container.get("name")
        if isinstance(name, str) and name:
            _docker_cleanup(name)
        outcome = {
            "status": "failed", "work_dir": str(attempt),
            "instance_id": prepared.get("instance_id"),
            "created_instance": prepared.get("created_instance", False),
            "prepare_only": False, "container_exit_code": None,
            "result": None, "handoff": [], "error": _error_record(exc),
        }
        _atomic_json(attempt / "outcome.json", outcome)
        sys.stderr.write(f"ctfy-worker: {_redact(str(exc))}\n")
        return 2
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def _module_main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "worker":
        try:
            return worker_main(argv[1:])
        except ctfy.CtfyError as exc:
            sys.stderr.write(f"ctfy-worker: {_redact(str(exc))}\n")
            return 2
    sys.stderr.write("usage: ctfy_commander.py worker --store PATH --challenge-id ID --attempt PATH\n")
    return 2


if __name__ == "__main__":
    sys.exit(_module_main())
