#!/usr/bin/env python3
"""Persistent state and local locks for a CTFY campaign."""
from __future__ import annotations

import datetime
import fcntl
import json
import math
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import ctfy


def resolve_deadline(
        ends_at: str | None, minutes: int | None, *, now: float) -> float:
    """Resolve the earliest usable campaign deadline as Unix seconds."""
    now = float(now)
    if not math.isfinite(now):
        raise ctfy.CtfyError("campaign start time must be finite")
    candidates = []
    if ends_at is not None:
        if not isinstance(ends_at, str) or not ends_at.strip():
            raise ctfy.CtfyError("competition ends_at must be an ISO timestamp")
        value = ends_at.strip()
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        try:
            parsed = datetime.datetime.fromisoformat(value)
        except ValueError as exc:
            raise ctfy.CtfyError("competition ends_at must be an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ctfy.CtfyError("competition ends_at must include a timezone")
        timestamp = parsed.timestamp()
        if not math.isfinite(timestamp):
            raise ctfy.CtfyError("competition ends_at must be finite")
        candidates.append(timestamp)
    if minutes is not None:
        if type(minutes) is not int or minutes <= 0:
            raise ctfy.CtfyError("campaign minutes must be a positive integer")
        try:
            candidates.append(now + minutes * 60)
        except OverflowError as exc:
            raise ctfy.CtfyError("campaign minutes are too large") from exc
    if not candidates:
        raise ctfy.CtfyError("campaign needs a reliable ends_at or minutes budget")
    deadline = float(min(candidates))
    if not math.isfinite(deadline) or deadline <= now:
        raise ctfy.CtfyError("campaign deadline must be in the future")
    return deadline


def make_campaign(
        competition: dict, *, deadline: float, stall_minutes: int,
        max_workers: int, now: float) -> dict:
    """Create a new manifest without copying platform credentials."""
    title = competition.get("title") or competition.get("name") or ""
    if not isinstance(title, str):
        raise ctfy.CtfyError("competition title must be a string")
    return {
        "schema_version": 1,
        "competition_id": ctfy.require_id(
            competition.get("id"), "competition_id",
        ),
        "competition_title": title,
        "created_at": float(now),
        "deadline": float(deadline),
        "stall_minutes": stall_minutes,
        "max_workers": max_workers,
        "challenges": {},
        "workers": {},
        "instances": {},
    }


@contextmanager
def _file_lock(path: Path, *, blocking: bool, label: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        operation = fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(fh.fileno(), operation)
        except BlockingIOError:
            raise ctfy.CtfyError(f"{label} is already locked") from None
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class CampaignStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def load(self) -> dict:
        path = self.root / "campaign.json"
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as exc:
            raise ctfy.CtfyError(f"cannot read campaign state: {exc}") from exc
        try:
            state = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ctfy.CtfyError("campaign state is not valid JSON") from exc
        if not isinstance(state, dict):
            raise ctfy.CtfyError("campaign state must be a JSON object")
        return state

    def save(self, state: dict) -> None:
        path = self.root / "campaign.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=path.parent,
                    prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as fh:
                temporary = Path(fh.name)
                json.dump(state, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def append_decision(self, record: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(record, ensure_ascii=False)
        with (self.root / "decisions.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(encoded + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    @contextmanager
    def lock(self, *, blocking: bool = True):
        with _file_lock(
                self.root / "campaign.lock", blocking=blocking, label="campaign"):
            yield

    @contextmanager
    def challenge_lock(self, challenge_id: str):
        challenge_id = ctfy.require_id(challenge_id, "challenge_id")
        with _file_lock(
                self.root / "locks" / f"{challenge_id}.lock",
                blocking=True, label=f"challenge {challenge_id}"):
            yield
