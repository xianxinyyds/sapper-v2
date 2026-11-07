#!/usr/bin/env python3
"""CTFY-0001 v3 protocol helpers for the hosted single-challenge runner."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class CtfyError(Exception):
    """A task, platform, or result-file error safe to show to the operator."""

    def __init__(self, message: str, *, status: int | None = None,
                 code: str | None = None):
        self.status = status
        self.code = code
        super().__init__(message)


class CtfyClient:
    """Scoped synchronous client for the current CTFY evaluation run."""

    def __init__(self, base_url: str, token: str, timeout: int = 30):
        if not base_url.strip():
            raise CtfyError("CTFY_PLATFORM_URL is empty")
        if not token.strip():
            raise CtfyError("CTFY_TOKEN is empty")
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "CtfyClient":
        return cls(os.environ.get("CTFY_PLATFORM_URL", ""),
                   os.environ.get("CTFY_TOKEN", ""))

    @staticmethod
    def _segment(value: str, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise CtfyError(f"{label} cannot be empty")
        return urllib.parse.quote(value, safe="")

    def _request(self, method: str, path: str, *, body: dict | None = None,
                 retry: bool = False) -> object:
        data = None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        attempts = 3 if retry else 1
        for attempt in range(attempts):
            req = urllib.request.Request(
                self.base_url + path, data=data, headers=headers, method=method,
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")[:2000]
                if retry and exc.code in {429, 502, 503, 504} and attempt + 1 < attempts:
                    delay = exc.headers.get("Retry-After", "")
                    try:
                        seconds = max(0.0, float(delay))
                    except ValueError:
                        seconds = float(2 ** attempt)
                    time.sleep(seconds)
                    continue
                code = None
                message = raw
                try:
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        code = payload.get("code")
                        message = payload.get("message", raw)
                except json.JSONDecodeError:
                    pass
                raise CtfyError(
                    f"CTFY {method} {path} -> HTTP {exc.code}: {message}",
                    status=exc.code, code=code if isinstance(code, str) else None,
                ) from None
            except urllib.error.URLError as exc:
                if retry and attempt + 1 < attempts:
                    time.sleep(float(2 ** attempt))
                    continue
                raise CtfyError(f"CTFY {method} {path} failed: {exc.reason}") from exc
            if not raw.strip():
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CtfyError(f"CTFY {method} {path} returned non-JSON") from exc
        raise AssertionError("unreachable")

    def get_challenge(self, challenge_id: str) -> dict:
        segment = self._segment(challenge_id, "challenge_id")
        result = self._request("GET", f"/api/v1/challenges/{segment}", retry=True)
        if not isinstance(result, dict):
            raise CtfyError("CTFY challenge response must be an object")
        if not isinstance(result.get("questions"), list):
            raise CtfyError("CTFY challenge response questions must be a list")
        return result

    @staticmethod
    def _attachment_name(name: str) -> str:
        if not isinstance(name, str) or not name or name in {".", ".."} \
                or Path(name).is_absolute() or Path(name).name != name:
            raise CtfyError(f"unsafe CTFY attachment name {name!r}")
        return name

    def download_attachment(self, instance_id: str, name: str, destination: Path) -> None:
        instance = self._segment(instance_id, "instance_id")
        filename = self._segment(self._attachment_name(name), "attachment name")
        path = f"/api/v1/instances/{instance}/attachments/{filename}"
        req = urllib.request.Request(
            self.base_url + path,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/octet-stream"},
            method="GET",
        )
        for attempt in range(3):
            try:
                response = urllib.request.urlopen(req, timeout=self.timeout)
                break
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")[:2000]
                if exc.code in {429, 502, 503, 504} and attempt < 2:
                    delay = exc.headers.get("Retry-After", "")
                    try:
                        seconds = max(0.0, float(delay))
                    except ValueError:
                        seconds = float(2 ** attempt)
                    time.sleep(seconds)
                    continue
                code = None
                message = raw
                try:
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        code = payload.get("code")
                        message = payload.get("message", raw)
                except json.JSONDecodeError:
                    pass
                raise CtfyError(
                    f"CTFY GET {path} -> HTTP {exc.code}: {message}",
                    status=exc.code, code=code if isinstance(code, str) else None,
                ) from None
            except urllib.error.URLError as exc:
                raise CtfyError(f"CTFY GET {path} failed: {exc.reason}") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with response, tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent,
                prefix=".attachment-", suffix=".tmp", delete=False,
            ) as fh:
                temporary = Path(fh.name)
                shutil.copyfileobj(response, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, destination)
        except OSError as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise CtfyError(f"cannot write CTFY attachment {destination}: {exc}") from exc

    def submit_answer(self, instance_id: str, question_id: str, answer: str) -> dict:
        self._segment(instance_id, "instance_id")
        if not isinstance(question_id, str) or not question_id.strip():
            raise CtfyError("question_id cannot be empty")
        if not isinstance(answer, str) or not answer.strip():
            raise CtfyError("answer cannot be empty")
        result = self._request("POST", "/api/v1/submissions", body={
            "instance_id": instance_id, "question_id": question_id, "answer": answer,
        })
        if not isinstance(result, dict) or type(result.get("correct")) is not bool:
            raise CtfyError("CTFY submission response requires a boolean correct field")
        return result


def ensure_attachments(task: dict, client: CtfyClient, work_dir: Path) -> None:
    attachments = task.get("attachments", [])
    if not isinstance(attachments, list):
        raise CtfyError("CTFY task attachments must be a list")
    pending = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise CtfyError("CTFY attachment entry must be an object")
        if attachment.get("path") is not None:
            continue
        name = client._attachment_name(attachment.get("name"))
        pending.append(name)
    for name in pending:
        client.download_attachment(
            task["target_instance"], name, work_dir / "attachments" / name,
        )


def load_task(work_dir: Path) -> dict:
    path = work_dir / "task.json"
    try:
        task = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CtfyError(f"cannot read CTFY task {path}: {exc}") from exc
    if not isinstance(task, dict):
        raise CtfyError("CTFY task must be a JSON object")
    if type(task.get("schema_version")) is not int or task["schema_version"] != 3:
        raise CtfyError("unsupported CTFY task schema_version; expected 3")
    return task


def require_instance_task(task: dict) -> None:
    if task.get("run_mode") != "instance":
        raise CtfyError(f"unsupported CTFY run_mode {task.get('run_mode')!r}; expected 'instance'")
    challenge = task.get("challenge")
    if not isinstance(challenge, dict) or not isinstance(challenge.get("id"), str) \
            or not challenge["id"].strip():
        raise CtfyError("CTFY instance task requires challenge.id")
    if not isinstance(task.get("target_instance"), str) or not task["target_instance"].strip():
        raise CtfyError("CTFY instance task requires target_instance")
    if not isinstance(task.get("questions"), list):
        raise CtfyError("CTFY instance task requires a questions list")
    for field in ("target_endpoints", "attachments", "scope", "rules_of_engagement"):
        if not isinstance(task.get(field, []), list):
            raise CtfyError(f"CTFY instance task {field} must be a list")


def write_result(work_dir: Path, *, answers: dict[str, str], solved: list[str],
                 fully_solved: bool, stop_reason: str) -> None:
    result = {
        "schema_version": 3,
        "answers": answers,
        "solved": solved,
        "fully_solved": fully_solved,
        "stop_reason": stop_reason,
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=work_dir,
            prefix=".result-", suffix=".tmp", delete=False,
        ) as fh:
            temporary = Path(fh.name)
            json.dump(result, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, work_dir / "result.json")
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise CtfyError(f"cannot write CTFY result in {work_dir}: {exc}") from exc
