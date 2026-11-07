#!/usr/bin/env bash
# CTFY-0001 v3: the runner reads /ctfy/work and writes result.json there.
set -euo pipefail

export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export DISABLE_TELEMETRY=1
export DISABLE_ERROR_REPORTING=1
export DISABLE_AUTOUPDATER=1
export IS_SANDBOX=1

exec python3 /opt/sapper/lib/ctfy_run.py
