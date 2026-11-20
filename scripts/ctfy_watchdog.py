#!/usr/bin/env python3
"""Restart a dead or hung CTFY campaign from a systemd timer."""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path


def log(message: str) -> None:
    print(f"[watchdog] {time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}",
          flush=True)


def in_window(clock: str, start: str, end: str) -> bool:
    return int(start) <= int(clock) <= int(end)


def lock_is_free(lock: Path) -> bool:
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def heartbeat_state(runs: Path, *, now: float, stale: int) -> str:
    beats = list(runs.glob("*/heartbeat")) if runs.is_dir() else []
    if beats:
        newest = max(path.stat().st_mtime for path in beats)
        return "fresh" if now - newest <= stale else "stale"
    manifests = list(runs.glob("*/campaign.json")) if runs.is_dir() else []
    if not manifests:
        return "missing"
    oldest = min(path.stat().st_mtime for path in manifests)
    return "stale" if now - oldest > stale else "missing"


def find_play_pids(project: Path, proc_root: Path = Path("/proc")) -> list[int]:
    """Find real `lib/ctfy_local.py play` processes running in the project."""
    pids = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace")
        except OSError:
            continue
        if "lib/ctfy_local.py" not in cmdline or " play" not in cmdline:
            continue
        try:
            cwd = (entry / "cwd").resolve()
        except OSError:
            continue
        if cwd != project.resolve():
            continue
        pids.append(int(entry.name))
    return pids


def read_cmdline(pid: int, proc_root: Path = Path("/proc")) -> str:
    try:
        return (proc_root / str(pid) / "cmdline").read_bytes().replace(
            b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def lock_holders(lock: Path, runner=subprocess.run,
                 proc_root: Path = Path("/proc")) -> list[int]:
    pids = []
    try:
        result = runner(["lslocks"], capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines():
            if str(lock) in line:
                parts = line.split()
                if len(parts) > 1 and parts[1].isdigit():
                    pids.append(int(parts[1]))
    except (OSError, subprocess.SubprocessError):
        pass
    if not pids:
        try:
            result = runner(["fuser", str(lock)], capture_output=True, text=True,
                            timeout=10)
            for token in result.stdout.split():
                if token.isdigit():
                    pids.append(int(token))
        except (OSError, subprocess.SubprocessError):
            pass
    return [pid for pid in pids
            if "qualifier" in read_cmdline(pid, proc_root)]


def main(argv=None) -> int:
    project = Path(os.environ.get(
        "CTFY_PROJECT", "/root/workspace/sapper-c870444"))
    runs = Path(os.environ.get("CTFY_RUNS_ROOT", project / "runs"))
    lock = Path(os.environ.get(
        "CTFY_LOCK_FILE", Path.home() / ".config" / "ctfy" / "qualifier.lock"))
    service = os.environ.get("CTFY_SERVICE", "ctfy-qualifier.service")
    stale = int(os.environ.get("CTFY_HEARTBEAT_STALE", "900"))
    start = os.environ.get("CTFY_WINDOW_START", "1000")
    end = os.environ.get("CTFY_WINDOW_END", "2005")
    clock = time.strftime("%H%M")
    if not in_window(clock, start, end):
        log(f"outside match window ({clock})")
        return 0

    def systemctl(*args: str) -> None:
        subprocess.run(["systemctl", *args], check=False)

    if lock_is_free(lock):
        log(f"no live campaign; starting {service}")
        systemctl("reset-failed", service)
        systemctl("start", "--no-block", service)
        return 0

    state = heartbeat_state(runs, now=time.time(), stale=stale)
    if state == "fresh":
        log("campaign healthy")
        return 0
    if state == "missing":
        # A campaign that just started has not written its first heartbeat
        # yet; never kill it based on absence alone.
        log("campaign starting (no heartbeat yet); leaving state alone")
        return 0

    pids = find_play_pids(project)
    if not pids:
        log(f"{state} heartbeat but no verified play process; leaving state alone")
        return 0
    log(f"{state} heartbeat; terminating hung play pid(s) {pids}")
    for pid in pids:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    time.sleep(3)
    for pid in pids:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    for pid in lock_holders(lock):
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    time.sleep(1)
    if lock_is_free(lock):
        systemctl("reset-failed", service)
        # A killed launcher can leave the oneshot unit nominally active; a
        # restart guarantees a fresh ExecStart.
        systemctl("restart", "--no-block", service)
    return 0


if __name__ == "__main__":
    sys.exit(main())
