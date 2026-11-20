#!/usr/bin/env bash
# Thin wrapper so systemd units can keep pointing at a .sh entrypoint.
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ctfy_watchdog.py" "$@"
