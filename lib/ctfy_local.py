#!/usr/bin/env python3
"""Host-side instance lifecycle for local CTFY-0001 runs."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import ctfy


_FORWARDED_ENV = (
    "CTFY_TOKEN", "LLM_FLAVOR", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL",
    "SAPPER_MAX_STEPS", "SAPPER_MAX_EXPLORE", "SAPPER_TIMEOUT", "SAPPER_RETRY_MODEL",
)


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ctfy.CtfyError(f"CTFY {label} must be an object")
    return value


def _endpoint(service: dict) -> dict:
    kind = service.get("service_type")
    host = service.get("host")
    port = service.get("port")
    if kind not in {"http", "https", "tcp", "udp", "ssh"} \
            or not isinstance(host, str) or not host.strip() \
            or type(port) is not int or not 1 <= port <= 65535:
        raise ctfy.CtfyError("CTFY instance returned an invalid service endpoint")
    path = service.get("path") or "/"
    if not isinstance(path, str) or not path.startswith("/"):
        raise ctfy.CtfyError("CTFY service path must start with /")
    url = f"{host}:{port}" if kind in {"tcp", "udp"} else f"{kind}://{host}:{port}"
    if kind not in {"tcp", "udp"} and path != "/":
        url += path
    result = {"label": service.get("label") or "", "type": kind, "url": url}
    if service.get("access") is not None:
        result["access"] = service["access"]
    credentials = service.get("credentials")
    if credentials is not None:
        credentials = _object(credentials, "service credentials")
        result["credentials"] = {
            key: credentials[key] for key in ("username", "password", "key_file")
            if isinstance(credentials.get(key), str) and credentials[key]
        }
    return result


def build_task(challenge: dict, instance: dict | None, competition_id: str,
               platform_url: str, max_turns: int) -> dict:
    challenge_id = challenge.get("id")
    if not isinstance(challenge_id, str) or not challenge_id.strip():
        raise ctfy.CtfyError("local CTFY run requires a challenge id")
    if not competition_id.strip():
        raise ctfy.CtfyError("local CTFY run requires a competition id")
    qa = challenge.get("is_qa_only") is True
    if not qa and not isinstance(instance, dict):
        raise ctfy.CtfyError("instance challenge requires a running instance")
    source = instance or challenge
    questions = []
    for question in source.get("questions", []):
        if not isinstance(question, dict) or not question.get("unlocked", True):
            continue
        if not isinstance(question.get("id"), str) or not question["id"].strip() \
                or not isinstance(question.get("prompt"), str):
            raise ctfy.CtfyError("CTFY question requires a string id and prompt")
        questions.append({"id": question["id"], "prompt": question["prompt"]})
    metadata = {
        key: challenge.get(key, "") for key in ("id", "name", "description", "category")
    }
    if isinstance(challenge.get("close_at"), str):
        metadata["close_at"] = challenge["close_at"]
    if instance:
        metadata["category"] = instance.get("category") or metadata["category"]
        expires_at = instance.get("expires_at")
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool) \
                and expires_at > 0:
            metadata["close_at"] = datetime.fromtimestamp(
                expires_at, timezone.utc,
            ).isoformat()
    topology = (instance or {}).get("network_topology", "simple")
    if topology == "engagement":
        raise ctfy.CtfyError(
            "local engagement networking is not configured; use a simple or QA challenge"
        )
    attachments = []
    for attachment in source.get("attachments", []):
        attachment = _object(attachment, "attachment")
        name = ctfy.CtfyClient._attachment_name(attachment.get("name"))
        size = attachment.get("size", 0)
        if type(size) is not int or size < 0:
            raise ctfy.CtfyError(f"invalid attachment size for {name!r}")
        path = f"/ctfy/work/attachments/{name}" if qa else None
        attachments.append({"name": name, "size": size, "path": path})
    return {
        "schema_version": 3,
        "run_mode": "qa" if qa else "instance",
        "challenge": metadata,
        "competition_id": competition_id,
        "questions": questions,
        "gated_questions": max(0, len(challenge.get("questions", [])) - len(questions)),
        "target_instance": None if qa else instance.get("id"),
        "target_endpoints": [
            _endpoint(_object(service, "service"))
            for service in (instance or {}).get("services", [])
        ],
        "attachments": attachments,
        "scope": source.get("entry_urls", []) if topology == "engagement" else [],
        "rules_of_engagement": [],
        "platform_url": platform_url,
        "max_turns": max_turns,
        "result": {"path": "/ctfy/work/result.json", "stdout_fallback": False},
    }


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as fh:
            temporary = Path(fh.name)
            json.dump(value, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _check_work_dir(work_dir: Path, task: dict) -> None:
    previous_path = work_dir / "task.json"
    if not previous_path.exists():
        if (work_dir / ".sapper").exists():
            raise ctfy.CtfyError("existing graph has no task.json; use a new work directory")
        return
    try:
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ctfy.CtfyError(f"cannot read previous local task: {exc}") from exc
    if not isinstance(previous, dict) \
            or (previous.get("challenge") or {}).get("id") != task["challenge"]["id"] \
            or previous.get("competition_id") != task["competition_id"] \
            or previous.get("target_instance") != task["target_instance"]:
        raise ctfy.CtfyError(
            "work directory belongs to another challenge, competition, or instance; "
            "use a new directory or keep and reuse the original instance"
        )


def prepare_work_dir(client: ctfy.CtfyClient, work_dir: Path, task: dict) -> None:
    _check_work_dir(work_dir, task)
    work_dir.mkdir(parents=True, exist_ok=True)
    for attachment in task["attachments"]:
        name = attachment["name"]
        destination = work_dir / "attachments" / Path(*name.split("/"))
        if task["run_mode"] == "qa" and not destination.is_file():
            client.download_challenge_attachment(task["challenge"]["id"], name, destination)
    ctfy.require_task(task)
    _write_json(work_dir / "task.json", task)


def _find_instance(client: ctfy.CtfyClient, challenge_id: str,
                   competition_id: str) -> dict | None:
    matches = [
        instance for instance in client.list_instances(challenge_id)
        if instance.get("competition_id") == competition_id
        and instance.get("status", "ready") in {"ready", "running"}
    ]
    if len(matches) > 1:
        raise ctfy.CtfyError(
            f"multiple running instances for {challenge_id}; stop the unused one first"
        )
    return client.get_instance(matches[0]["id"]) if matches else None


def _docker_command(args, work_dir: Path) -> list[str]:
    name = "sapper-local-" + "".join(
        char.lower() if char.isalnum() else "-" for char in args.challenge_id
    )[:60] + f"-{os.getpid()}"
    command = [
        "docker", "run", "--rm", "--name", name,
        "--mount", f"type=bind,src={work_dir.resolve()},dst=/ctfy/work",
        "--env", "CTFY_WORK_DIR=/ctfy/work",
        "--env", "CTFY_PLATFORM_URL",
        "--env", "CTFY_EVAL_MAX_TURNS",
        "--env", "SAPPER_ROUND_MINUTES",
        "--env", "CTFY_DISPATCH_TIMEOUT",
    ]
    for variable in _FORWARDED_ENV:
        command += ["--env", variable]
    command.append(args.image)
    return command


def submit_handed_off_answers(client: ctfy.CtfyClient, task: dict,
                              result: dict, work_dir: Path) -> list[dict]:
    answers = result.get("answers")
    if not isinstance(answers, dict) or not answers:
        return []
    outcomes = []
    for question_id, answer in answers.items():
        if not isinstance(question_id, str) or not isinstance(answer, str):
            outcomes.append({"question_id": str(question_id), "error": "invalid answer shape"})
            continue
        try:
            if task["run_mode"] == "qa":
                response = client.submit_qa_answer(
                    task["challenge"]["id"], task["competition_id"], question_id, answer,
                )
            else:
                response = client.submit_answer(task["target_instance"], question_id, answer)
            outcomes.append({
                "question_id": question_id, "correct": response["correct"], "response": response,
            })
        except ctfy.CtfyError as exc:
            outcomes.append({"question_id": question_id, "error": str(exc)})
    _write_json(work_dir / "handoff.json", {"submissions": outcomes})
    return outcomes


def run_local(args) -> int:
    client = ctfy.CtfyClient(args.platform_url, os.environ.get("CTFY_TOKEN", ""))
    challenge = client.get_challenge(args.challenge_id, competition_id=args.competition)
    instance = None
    pending_instance_id = None
    try:
        if challenge.get("is_qa_only") is not True:
            instance = _find_instance(client, args.challenge_id, args.competition)
            if instance is None:
                def track_instance(instance_id):
                    nonlocal pending_instance_id
                    pending_instance_id = instance_id

                instance = client.start_instance(
                    args.challenge_id, args.competition, ttl=args.ttl,
                    timeout=args.startup_timeout, on_created=track_instance,
                )
                pending_instance_id = None
        task = build_task(
            challenge, instance, args.competition, args.platform_url, args.max_turns,
        )
        work_dir = args.work_dir or Path.cwd() / "runs" / args.competition \
            / args.challenge_id / (instance["id"] if instance else "qa")
        prepare_work_dir(client, work_dir, task)
        if args.prepare_only:
            print(json.dumps({
                "work_dir": str(work_dir), "instance_id": task["target_instance"],
                "prepare_only": True,
            }, ensure_ascii=False))
            return 0
        result_path = work_dir / "result.json"
        try:
            if result_path.exists() or result_path.is_symlink():
                os.replace(result_path, work_dir / "result.previous.json")
        except OSError as exc:
            raise ctfy.CtfyError(f"cannot preserve the previous result: {exc}") from exc
        env = os.environ.copy()
        env.update({
            "CTFY_PLATFORM_URL": args.platform_url,
            "CTFY_EVAL_MAX_TURNS": str(args.max_turns),
            "SAPPER_ROUND_MINUTES": str(args.minutes),
            "CTFY_DISPATCH_TIMEOUT": str(args.dispatch_timeout),
        })
        command = _docker_command(args, work_dir)
        try:
            completed = subprocess.run(
                command, env=env, timeout=args.minutes * 60 + 30,
            )
        except BaseException:
            container_name = command[command.index("--name") + 1]
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True, timeout=10, check=False,
                )
            except (OSError, subprocess.SubprocessError) as cleanup_error:
                sys.stderr.write(f"ctfy-local: container cleanup failed: {cleanup_error}\n")
            raise
        try:
            result = json.loads((work_dir / "result.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ctfy.CtfyError(f"container did not produce a valid result.json: {exc}") from exc
        if not isinstance(result, dict) or result.get("schema_version") != 3:
            raise ctfy.CtfyError("container result.json has an unsupported schema")
        handoff = submit_handed_off_answers(client, task, result, work_dir)
        print(json.dumps({
            "work_dir": str(work_dir), "instance_id": task["target_instance"],
            "container_exit_code": completed.returncode, "result": result,
            "handoff": handoff,
        }, ensure_ascii=False))
        return completed.returncode
    finally:
        active_error = sys.exc_info()[0] is not None
        cleanup_id = instance.get("id") if instance else pending_instance_id
        if cleanup_id and not args.keep_instance and not (instance or {}).get("shared"):
            try:
                client.stop_instance(cleanup_id)
            except ctfy.CtfyError as exc:
                if not active_error:
                    raise
                sys.stderr.write(f"ctfy-local: instance cleanup failed: {exc}\n")


def _list_challenges(args) -> int:
    client = ctfy.CtfyClient(args.platform_url, os.environ.get("CTFY_TOKEN", ""))
    competition = client._segment(args.competition, "competition_id")
    path = f"/api/v1/competitions/{competition}/challenges?limit={args.limit}"
    result = _object(client._request("GET", path, retry=True), "challenge list")
    print(json.dumps(result.get("items", []), ensure_ascii=False, indent=2))
    return 0


def _list_instances(args) -> int:
    client = ctfy.CtfyClient(args.platform_url, os.environ.get("CTFY_TOKEN", ""))
    instances = [
        instance for instance in client.list_instances()
        if not args.competition or instance.get("competition_id") == args.competition
    ]
    print(json.dumps(instances, ensure_ascii=False, indent=2))
    return 0


def _stop(args) -> int:
    client = ctfy.CtfyClient(args.platform_url, os.environ.get("CTFY_TOKEN", ""))
    client.stop_instance(args.instance_id)
    print(json.dumps({"stopped": args.instance_id}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platform-url", default=os.environ.get("CTFY_PLATFORM_URL", "https://ctfy.tsinghua.edu.cn"),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    challenges = sub.add_parser("challenges", help="list a competition's challenges")
    challenges.add_argument("--competition", required=True)
    challenges.add_argument("--limit", type=_positive, default=50)
    challenges.set_defaults(func=_list_challenges)
    instances = sub.add_parser("instances", help="list running instances")
    instances.add_argument("--competition", default="")
    instances.set_defaults(func=_list_instances)
    stop = sub.add_parser("stop", help="stop one owned instance; missing instances are ignored")
    stop.add_argument("instance_id")
    stop.set_defaults(func=_stop)
    run = sub.add_parser("run", help="start/reuse, solve, hand off answers, and clean up")
    run.add_argument("challenge_id")
    run.add_argument("--competition", required=True)
    run.add_argument("--work-dir", type=Path)
    run.add_argument("--image", default="sapper-ctfy:local-test")
    run.add_argument("--minutes", type=_positive, default=165)
    run.add_argument("--max-turns", type=_positive, default=12)
    run.add_argument("--dispatch-timeout", type=_positive, default=300)
    run.add_argument("--startup-timeout", type=_positive, default=300)
    run.add_argument("--ttl", type=_positive)
    run.add_argument("--keep-instance", action="store_true")
    run.add_argument("--prepare-only", action="store_true")
    run.set_defaults(func=run_local)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ctfy.CtfyError as exc:
        sys.stderr.write(f"ctfy-local: {exc}\n")
        return 2
    except subprocess.TimeoutExpired:
        sys.stderr.write("ctfy-local: container exceeded its configured time budget\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
