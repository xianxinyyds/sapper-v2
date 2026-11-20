#!/usr/bin/env python3
"""Automatic WireGuard lifecycle for CTFY engagement instances."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import ctfy

_CONFIG_DIR = Path("/etc/wireguard")


def interface_name(instance_id: str) -> str:
    # Linux interface names are limited to 15 characters (IFNAMSIZ - 1).
    safe = re.sub(r"[^a-z0-9]", "", instance_id.lower())[:10] or "instance"
    return f"ctfy-{safe}"


def _run(command: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False,
    )


def is_up(name: str) -> bool:
    return _run(["wg", "show", name]).returncode == 0


def ensure_vpn(client: ctfy.CtfyClient, instance: dict) -> str | None:
    """Bring up the instance's WireGuard tunnel; idempotent.

    Returns the interface name, or None for non-engagement instances.
    """
    if instance.get("network_topology") != "engagement":
        return None
    endpoint = instance.get("vpn_endpoint")
    if not isinstance(endpoint, dict):
        raise ctfy.CtfyError("engagement instance has no vpn_endpoint")
    instance_id = ctfy.require_id(instance.get("id"), "instance_id")
    name = interface_name(instance_id)
    if is_up(name):
        return name
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(_CONFIG_DIR, 0o700)
    config = _CONFIG_DIR / f"{name}.conf"
    temporary = config.with_name(f".{config.name}.tmp")
    try:
        client.download_vpn_config(instance_id, temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, config)
    except ctfy.CtfyError:
        temporary.unlink(missing_ok=True)
        raise
    result = _run(["wg-quick", "up", name])
    if result.returncode != 0:
        config.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout).strip()[:300]
        raise ctfy.CtfyError(f"cannot bring up WireGuard {name}: {detail}")
    return name


def teardown_vpn(name: str) -> None:
    if not name or not is_up(name):
        return
    _run(["wg-quick", "down", name])
    (_CONFIG_DIR / f"{name}.conf").unlink(missing_ok=True)
