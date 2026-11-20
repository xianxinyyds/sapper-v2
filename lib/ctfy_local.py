#!/usr/bin/env python3
"""Host-side instance lifecycle for local CTFY-0001 runs."""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import ctfy
import ctfy_campaign


_FORWARDED_ENV = (
    "CTFY_TOKEN", "LLM_FLAVOR", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL",
    "SAPPER_MAX_STEPS", "SAPPER_MAX_EXPLORE", "SAPPER_TIMEOUT", "SAPPER_RETRY_MODEL",
    "SAPPER_BIN",
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
        "--network", "host",
        "--mount", f"type=bind,src={work_dir.resolve()},dst=/ctfy/work",
        "--mount", (
            f"type=bind,src={Path(__file__).resolve().parent},"
            "dst=/opt/sapper/lib,readonly"
        ),
        "--env", "CTFY_WORK_DIR=/ctfy/work",
        "--env", "CTFY_PLATFORM_URL",
        "--env", "CTFY_EVAL_MAX_TURNS",
        "--env", "SAPPER_ROUND_MINUTES",
        "--env", "CTFY_DISPATCH_TIMEOUT",
    ]
    for variable in _FORWARDED_ENV:
        command += ["--env", variable]
    for mapping in args.add_host:
        command += ["--add-host", mapping]
    command.append(args.image)
    return command


def submit_handed_off_answers(client: ctfy.CtfyClient, task: dict,
                              result: dict, work_dir: Path) -> list[dict]:
    answers = result.get("answers")
    if not isinstance(answers, dict) or not answers:
        return []
    handoff_path = work_dir / "handoff.json"
    try:
        previous = json.loads(handoff_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        previous = {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ctfy.CtfyError(f"cannot read previous answer handoff: {exc}") from exc
    if not isinstance(previous, dict) or not isinstance(previous.get("submissions", []), list):
        raise ctfy.CtfyError("previous answer handoff has an unsupported schema")
    outcomes = previous.get("submissions", [])
    if not all(isinstance(outcome, dict) for outcome in outcomes):
        raise ctfy.CtfyError("previous answer handoff submissions must be objects")
    seen = {
        (outcome.get("question_id"), outcome.get("answer")) for outcome in outcomes
        if isinstance(outcome.get("question_id"), str)
        and isinstance(outcome.get("answer"), str)
    }
    for question_id, answer in answers.items():
        if not isinstance(question_id, str) or not isinstance(answer, str):
            outcomes.append({"question_id": str(question_id), "error": "invalid answer shape"})
            continue
        candidate = (question_id, answer)
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            if task["run_mode"] == "qa":
                response = client.submit_qa_answer(
                    task["challenge"]["id"], task["competition_id"], question_id, answer,
                )
            else:
                response = client.submit_answer(task["target_instance"], question_id, answer)
            outcomes.append({
                "question_id": question_id, "answer": answer,
                "correct": response["correct"], "response": response,
            })
        except ctfy.CtfyError as exc:
            outcomes.append({
                "question_id": question_id, "answer": answer, "correct": None,
                "status": "unresolved" if exc.transport else "submission_failed",
                "transport_uncertain": exc.transport, "error": str(exc),
            })
    _write_json(handoff_path, {"submissions": outcomes})
    return outcomes


def get_challenge_for_run(client: ctfy.CtfyClient, challenge_id: str,
                          competition_id: str) -> dict:
    challenge_id = ctfy.require_id(challenge_id, "challenge_id")
    competition_id = ctfy.require_id(competition_id, "competition_id")
    try:
        return client.get_challenge(challenge_id, competition_id=competition_id)
    except ctfy.CtfyError as exc:
        if exc.code != "go_unsupported_feature":
            raise
    matches = [
        item for item in client.list_challenges(competition_id)
        if item.get("id") == challenge_id
    ]
    if len(matches) != 1:
        raise ctfy.CtfyError(
            f"challenge {challenge_id} was not found exactly once in {competition_id}"
        )
    return matches[0]


def run_challenge(args: argparse.Namespace, *, client: ctfy.CtfyClient | None = None,
                  challenge: dict | None = None,
                  on_event: Callable[[str, dict], None] | None = None) -> dict:
    challenge_id = ctfy.require_id(args.challenge_id, "challenge_id")
    competition_id = ctfy.require_id(args.competition, "competition_id")
    if client is None:
        client = ctfy.CtfyClient(args.platform_url, os.environ.get("CTFY_TOKEN", ""))
    if challenge is None:
        challenge = get_challenge_for_run(client, challenge_id, competition_id)
    instance = None
    pending_instance_id = None
    created_instance = False
    owns_instance = False
    try:
        if challenge.get("is_qa_only") is not True:
            known_id = getattr(args, "instance_id", None)
            forced_id = os.environ.get("CTFY_FORCE_INSTANCE_ID", "").strip()
            forced_challenge = os.environ.get("CTFY_FORCE_CHALLENGE_ID", "").strip()
            if known_id:
                instance = client.get_instance(
                    ctfy.require_id(known_id, "instance_id"),
                    retry=True, require_questions=True,
                )
            elif forced_id and forced_challenge == challenge_id:
                instance = client.get_instance(
                    forced_id, retry=True, require_questions=True)
            else:
                instance = _find_instance(client, challenge_id, competition_id)
            if instance is None:
                def track_instance(instance_id):
                    nonlocal pending_instance_id
                    pending_instance_id = ctfy.require_id(instance_id, "instance_id")

                owns_instance = True
                instance = client.start_instance(
                    challenge_id, competition_id, ttl=args.ttl,
                    timeout=args.startup_timeout, on_created=track_instance,
                )
                pending_instance_id = None
                created_instance = True
            ctfy.require_id(instance.get("id"), "instance_id")
        vpn_interface = None
        if instance is not None:
            import ctfy_vpn
            vpn_interface = ctfy_vpn.ensure_vpn(client, instance)
        task = build_task(
            challenge, instance, competition_id, args.platform_url, args.max_turns,
        )
        work_dir = args.work_dir or Path.cwd() / "runs" / competition_id \
            / challenge_id / (instance["id"] if instance else "qa")
        prepare_work_dir(client, work_dir, task)
        engine = args.engine
        if engine is not None:
            temporary_engine = None
            try:
                with engine.open("rb") as source, tempfile.NamedTemporaryFile(
                        mode="wb", dir=work_dir, prefix=".ctfy-engine.",
                        suffix=".tmp", delete=False,
                ) as fh:
                    temporary_engine = Path(fh.name)
                    shutil.copyfileobj(source, fh)
                    os.fchmod(fh.fileno(), os.fstat(source.fileno()).st_mode | 0o111)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(temporary_engine, work_dir / ".ctfy-engine")
            except OSError as exc:
                raise ctfy.CtfyError(f"cannot prepare custom engine: {exc}") from exc
            finally:
                if temporary_engine is not None:
                    temporary_engine.unlink(missing_ok=True)
        outcome = {
            "work_dir": str(work_dir), "instance_id": task["target_instance"],
            "created_instance": created_instance, "prepare_only": args.prepare_only,
            "container_exit_code": None, "result": None, "handoff": [],
            "vpn_interface": vpn_interface,
        }
        if on_event is not None:
            on_event("prepared", {
                "work_dir": str(work_dir), "instance_id": task["target_instance"],
                "created_instance": created_instance,
                "vpn_interface": vpn_interface,
            })
        if args.prepare_only:
            return outcome
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
        if engine is not None:
            env["SAPPER_BIN"] = "/ctfy/work/.ctfy-engine"
        command = _docker_command(args, work_dir)
        container_name = command[command.index("--name") + 1]
        if on_event is not None:
            on_event("container", {"name": container_name})
        try:
            completed = subprocess.run(
                command, env=env, timeout=args.minutes * 60 + 30,
            )
        except BaseException:
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
        outcome.update({
            "container_exit_code": completed.returncode, "result": result, "handoff": handoff,
        })
        return outcome
    finally:
        active_error = sys.exc_info()[0] is not None
        cleanup_id = instance.get("id") if instance else pending_instance_id
        if cleanup_id and owns_instance and not args.keep_instance \
                and not (instance or {}).get("shared"):
            try:
                client.stop_instance(cleanup_id)
            except ctfy.CtfyError as exc:
                if not active_error:
                    raise
                sys.stderr.write(f"ctfy-local: instance cleanup failed: {exc}\n")


def run_local(args) -> int:
    client = ctfy.CtfyClient(args.platform_url, os.environ.get("CTFY_TOKEN", ""))
    outcome = run_challenge(args, client=client)
    print(json.dumps(outcome, ensure_ascii=False))
    return outcome["container_exit_code"] if outcome["container_exit_code"] is not None else 0


def _runs_root() -> Path:
    override = os.environ.get("CTFY_RUNS_ROOT")
    return Path(override) if override else Path.cwd() / "runs"


def _resumable_campaign(manifest: dict, *, now: float) -> tuple[str, float] | None:
    """Return (competition_id, deadline) for a live, resumable manifest.

    A manifest is resumable only when it is a schema-1 campaign with a finite
    deadline strictly in the future; an expired campaign is replaced rather
    than silently resumed into an immediate deadline cleanup.
    """
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        return None
    competition_id = manifest.get("competition_id")
    if not isinstance(competition_id, str) or not competition_id.strip():
        return None
    deadline = manifest.get("deadline")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) \
            or not math.isfinite(deadline) or deadline <= now:
        return None
    return competition_id, float(deadline)


def _scan_campaigns(runs: Path, *, now: float) -> dict[str, Path]:
    """Map competition_id to campaign root for live, resumable manifests."""
    result = {}
    try:
        paths = sorted(runs.glob("*/campaign.json"))
    except OSError:
        return result
    for path in paths:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        live = _resumable_campaign(manifest, now=now)
        if live is None:
            continue
        competition_id, _ = live
        result[competition_id] = path.parent
    return result


def _competition_by_id(client, competition_id: str) -> dict:
    try:
        for competition in client.list_competitions():
            if isinstance(competition, dict) \
                    and competition.get("id") == competition_id:
                return competition
    except ctfy.CtfyError:
        pass
    return {"id": competition_id}


def _resolve_campaign(client, args, runs: Path, *, now: float):
    import ctfy_commander

    manifests = _scan_campaigns(runs, now=now)
    competition_id = args.competition
    if competition_id:
        competition_id = ctfy.require_id(competition_id, "competition_id")
    else:
        if len(manifests) == 1:
            competition_id = next(iter(manifests))
        elif len(manifests) > 1:
            raise ctfy.CtfyError(
                "multiple resumable campaigns exist; pass --competition to choose one"
            )
    if competition_id and competition_id in manifests:
        store = ctfy_campaign.CampaignStore(manifests[competition_id])
        return store, store.load(), _competition_by_id(client, competition_id)

    competitions = client.list_competitions(phase="running")
    competition = ctfy_commander.select_competition(competitions, competition_id)
    competition_id = competition["id"]
    store = ctfy_campaign.CampaignStore(runs / competition_id)
    with store.lock():
        manifest = store.load()
        if manifest and _resumable_campaign(manifest, now=now) is not None:
            return store, manifest, _competition_by_id(client, competition_id)
        deadline = ctfy_campaign.resolve_deadline(
            competition.get("ends_at"), args.minutes, now=now,
        )
        manifest = ctfy_campaign.make_campaign(
            competition, deadline=deadline, stall_minutes=args.stall_minutes,
            max_workers=args.max_workers, now=now,
        )
        store.save(manifest)
    return store, manifest, competition


class _PlaySignals:
    def __init__(self, commander):
        self.commander = commander
        self._old_handlers = {}

    def install(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle)

    def _handle(self, signum, frame):
        self.commander.interrupt()

    def restore(self):
        for signum, handler in self._old_handlers.items():
            signal.signal(signum, handler)


def play(args, *, client=None) -> int:
    if client is None:
        client = ctfy.CtfyClient(args.platform_url, os.environ.get("CTFY_TOKEN", ""))
    import ctfy_commander

    store, manifest, competition = _resolve_campaign(
        client, args, _runs_root(), now=time.time(),
    )
    adopt = getattr(args, "adopt", None) or []
    if adopt:
        with store.lock():
            state = store.load()
            for spec in adopt:
                if "=" not in spec:
                    raise ctfy.CtfyError("--adopt requires CHALLENGE=INSTANCE")
                challenge_id, instance_id = spec.split("=", 1)
                challenge_id = ctfy.require_id(challenge_id, "challenge_id")
                instance_id = ctfy.require_id(instance_id, "instance_id")
                state.setdefault("instances", {})[instance_id] = {
                    "challenge_id": challenge_id,
                    "created_by_campaign": False,
                    "status": "ready",
                    "adopted_at": time.time(),
                }
            store.save(state)
    engine = getattr(args, "engine", None)
    if engine is not None:
        os.environ["SAPPER_BIN"] = str(Path(engine).resolve())
        # The commander must answer with one JSON object, not explore with tools.
        os.environ["CTFY_ENGINE_NO_TOOLS"] = "1"
    commander = ctfy_commander.Commander(
        store, client, competition, args, poll_seconds=args.poll_seconds,
    )
    signals = _PlaySignals(commander)
    signals.install()
    try:
        with store.lock(blocking=False):
            return commander.run()
    finally:
        signals.restore()


def cleanup(args, *, client=None) -> int:
    if client is None:
        client = ctfy.CtfyClient(args.platform_url, os.environ.get("CTFY_TOKEN", ""))
    competition_id = ctfy.require_id(args.competition, "competition_id")
    store = ctfy_campaign.CampaignStore(_runs_root() / competition_id)
    stopped = []
    failed = []
    with store.lock():
        state = store.load()
        if not state:
            raise ctfy.CtfyError(
                f"no campaign manifest for competition {competition_id}"
            )
        instances = state.get("instances", {})
        if not isinstance(instances, dict):
            instances = {}
        changed = False
        for instance_id, record in list(instances.items()):
            if not isinstance(instance_id, str) or not isinstance(record, dict):
                continue
            if record.get("created_by_campaign") is not True:
                continue
            if record.get("closed") is True or record.get("status") in {
                    "stopped", "expired", "failed", "error"}:
                continue
            try:
                client.stop_instance(instance_id)
            except ctfy.CtfyError:
                failed.append(instance_id)
                continue
            record["closed"] = True
            record["status"] = "stopped"
            stopped.append(instance_id)
            changed = True
            try:
                import ctfy_vpn
                ctfy_vpn.teardown_vpn(ctfy_vpn.interface_name(instance_id))
            except (OSError, ctfy.CtfyError, subprocess.SubprocessError) as exc:
                sys.stderr.write(
                    f"ctfy-local: vpn teardown failed for {instance_id}: {exc}\n")
        if changed:
            store.save(state)
    print(json.dumps(
        {"competition_id": competition_id, "stopped": stopped, "failed": failed},
        ensure_ascii=False,
    ))
    return 0


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


def _add_worker_options(parser) -> None:
    parser.add_argument("--image", default="sapper-ctfy:local-test")
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--add-host", action="append", default=[], metavar="HOST:IP")
    parser.add_argument("--max-turns", type=_positive, default=12)
    parser.add_argument("--dispatch-timeout", type=_positive, default=300)
    parser.add_argument("--startup-timeout", type=_positive, default=300)
    parser.add_argument("--ttl", type=_positive)
    parser.add_argument("--instance-id", default=None)


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
    _add_worker_options(run)
    run.add_argument("--minutes", type=_positive, default=165)
    run.add_argument("--keep-instance", action="store_true")
    run.add_argument("--prepare-only", action="store_true")
    run.set_defaults(func=run_local)
    play_parser = sub.add_parser("play", help="autonomously run a CTFY campaign")
    play_parser.add_argument("--competition", default="")
    play_parser.add_argument("--minutes", type=_positive)
    play_parser.add_argument("--stall-minutes", type=_positive, default=30)
    play_parser.add_argument("--max-workers", type=_positive, default=2)
    play_parser.add_argument("--poll-seconds", type=_positive, default=30)
    play_parser.add_argument(
        "--adopt", action="append", default=[], metavar="CHALLENGE=INSTANCE",
        help="record an existing platform instance as reusable, never auto-closed",
    )
    _add_worker_options(play_parser)
    play_parser.set_defaults(func=play)
    cleanup_parser = sub.add_parser(
        "cleanup", help="stop campaign-owned leftover instances")
    cleanup_parser.add_argument("--competition", required=True)
    cleanup_parser.set_defaults(func=cleanup)
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
