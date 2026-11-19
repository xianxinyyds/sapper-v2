---
name: ctfy-play
description: One-prompt CTFY operator. Trigger when the user says “打比赛” (play the competition) or asks to autonomously play a running CTFY competition — launches `python3 lib/ctfy_local.py play`, which discovers the competition and challenges, lets the AI commander choose targets, runs single-challenge Workers, and reports platform-verified results. Not for a manually chosen single challenge (`lib/ctfy_local.py run`).
license: MIT
---

# CTFY Play — 一句「打比赛」启动整场比赛

This skill is a **thin launcher** and operator contract for
`lib/ctfy_local.py play`. The scheduler already lives in the CLI
(`lib/ctfy_commander.py`): it discovers running competitions and their
challenges, asks the model commander to fire/park/stop Workers, enforces the
deadline and instance capacity, and records decisions. **Do not re-implement**
that scheduler here or drive challenges one by one with `run`.

## Operator contract

- Do **not** read challenge statements, attachments, instance data, or flags on
  behalf of the solver. The Worker reads what it needs; your job is to start and
  observe the run.
- Do **not** hand-write exploits, payloads, commands, or answers for the solver.
  Do not submit answers yourself; the Worker and the host handoff do that.
- Do **not** ask for a competition, challenge, or instance ID first. `play`
  discovers them. Pass `--competition` only when the user names a specific
  competition.
- Report the AI's decisions and results **faithfully**: the targets it chose,
  the minutes it granted, park/fire/stop reasons, result categories, and handoff
  evidence. Never claim a flag was accepted unless the platform verdict or
  authoritative instance state proves it.
- Never print or log credential values. Do not enable shell tracing around the
  credential environment.

## Quick start (one prompt)

Run from the repository root (the directory containing `lib/ctfy_local.py`) on a
host that can reach the platform and whose Docker daemon is available.

Run the whole setup and launch **as one shell command** (one Claude Code Bash
call). Each Bash call is a fresh shell, so environment exported in an earlier
call would be lost before `play` starts, and a missing `LLM_BASE_URL` would also
skip the gateway `--add-host` derivation. The single block below loads the
optional env file, maps the model session variables to `LLM_*`, derives the
gateway hosts entries, and launches `play`:

```bash
# ONE shell command: env -> LLM_* -> gateway --add-host -> play.
# Optional host-only credentials; loaded only when present.
ENV_FILE="${CTFY_ENV_FILE:-$HOME/.config/ctfy/env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

# Map the existing OpenAI/Anthropic session variables to the LLM_* interface.
export LLM_FLAVOR="${LLM_FLAVOR:-anthropic}"
export LLM_API_KEY="${LLM_API_KEY:-${OPENAI_API_KEY:-}}"
export LLM_BASE_URL="${LLM_BASE_URL:-${OPENAI_BASE_URL:-}}"
# deepseek-v4-pro is only the current test default; export LLM_MODEL to change it.
export LLM_MODEL="${LLM_MODEL:-${OPENAI_MODEL:-deepseek-v4-pro}}"

# Forward an existing custom HTTP engine when present; never rebuild the image.
ENGINE="${CTFY_ENGINE:-$HOME/.config/ctfy/openai_http_engine.py}"
ENGINE_ARGS=()
if [[ -f "$ENGINE" ]]; then
  ENGINE_ARGS=(--engine "$ENGINE")
fi

# Resolve the model gateway host now and copy its current IPv4 addresses into
# the container's /etc/hosts. No gateway address is hard-coded.
ADD_HOST_ARGS=()
if [[ -n "${LLM_BASE_URL:-}" ]]; then
  gateway_host="${LLM_BASE_URL#*://}"
  gateway_host="${gateway_host%%/*}"
  gateway_host="${gateway_host%%:*}"
  if [[ -n "$gateway_host" ]] && command -v getent >/dev/null 2>&1; then
    while read -r gateway_ip _; do
      [[ -n "$gateway_ip" ]] && ADD_HOST_ARGS+=(--add-host "${gateway_host}:${gateway_ip}")
    done < <(getent ahostsv4 "$gateway_host" | awk '{print $1}' | sort -u)
  fi
fi

# Thin launcher around the existing scheduler.
PLAY_ARGS=(play)
[[ -n "${CTFY_COMPETITION:-}" ]] && PLAY_ARGS+=(--competition "$CTFY_COMPETITION")
[[ -n "${CTFY_MINUTES:-}" ]] && PLAY_ARGS+=(--minutes "$CTFY_MINUTES")
[[ -n "${CTFY_STALL_MINUTES:-}" ]] && PLAY_ARGS+=(--stall-minutes "$CTFY_STALL_MINUTES")
[[ -n "${CTFY_MAX_WORKERS:-}" ]] && PLAY_ARGS+=(--max-workers "$CTFY_MAX_WORKERS")
if [[ ${#ENGINE_ARGS[@]} -gt 0 ]]; then
  PLAY_ARGS+=("${ENGINE_ARGS[@]}")
fi
if [[ ${#ADD_HOST_ARGS[@]} -gt 0 ]]; then
  PLAY_ARGS+=("${ADD_HOST_ARGS[@]}")
fi
python3 lib/ctfy_local.py "${PLAY_ARGS[@]}"
```

If the engine file is absent, `play` uses the image's configured engine. If
`getent` finds no address, Docker uses its normal DNS path. In both cases the
image is reused as-is; this skill never builds or rebuilds it.

## Run it in the background

`play` blocks until the campaign deadline or until it is stopped. That is longer
than a foreground Bash call's timeout/cap, so launch the one shell command above
with the Bash tool's `run_in_background: true`, or detach it in tmux:

```bash
# Claude Code Bash tool: start the ONE shell command above with run_in_background: true.
# tmux alternative, from the repository root:
tmux new-session -d -s ctfy-play 'bash -lc "<the one shell command above>"'
```

Poll campaign state and recent AI decisions without waiting inline:

```bash
# Campaign manifest: status, deadline, rows, and owned instances.
python3 -m json.tool runs/<competition>/campaign.json

# One JSON decision record per line; the tail shows the latest AI choices.
tail -n 20 runs/<competition>/decisions.jsonl

# tmux only: read the latest terminal output.
tmux capture-pane -pt ctfy-play -S -40
```

Interrupt by signalling the `play` process itself, not its Docker children:

```bash
pgrep -af 'lib/ctfy_local.py.*play'
kill -INT <pid>    # first SIGINT: graceful Worker stop, instances preserved
kill -INT <pid>    # second SIGINT: bounded forced cleanup
# or
kill -TERM <pid>   # SIGTERM follows the same interrupt path
```

The first signal asks Workers to stop gracefully and preserves their instances;
a second signal follows the bounded forced path. If any campaign-created
instance remains afterwards, stop it with the cleanup command below.

## Options (all optional)

| Option | Meaning |
| --- | --- |
| `--competition ID` | Choose a competition when several are visible. Omit to auto-discover. |
| `--minutes N` | Campaign budget. Defaults to the competition `ends_at` when available. |
| `--stall-minutes N` | Soft advice to the AI commander after N minutes without real progress (default 30). |
| `--max-workers N` | Maximum concurrent single-challenge Workers (default 2). |
| `--poll-seconds N` | Decision-loop polling interval (default 30). |
| `--image NAME` | Existing solver image to run. The default local tag is used when omitted. |
| `--engine PATH` | Existing custom engine wrapper. The launcher forwards `CTFY_ENGINE` when present. |
| `--add-host HOST:IP` | Extra container hosts entry. The launcher derives it from `LLM_BASE_URL` and `getent ahostsv` at runtime. |
| `--max-turns N` | Per-Worker turn budget (default 12). |
| `--dispatch-timeout N` | Per-dispatch cap in seconds (default 300). |
| `--startup-timeout N` | Instance readiness cap in seconds (default 300). |
| `--ttl N` | Optional instance lifetime in seconds. |

Examples:

```bash
# Auto-discover and play at the competition's own deadline.
python3 lib/ctfy_local.py play

# Bound a manual session to 20 minutes with one Worker.
python3 lib/ctfy_local.py play --minutes 20 --max-workers 1

# Disambiguate when more than one campaign is resumable.
python3 lib/ctfy_local.py play --competition <competition-id>
```

Placeholders such as `<competition-id>` are supplied by the user or discovered
at runtime; never commit a real competition or instance identifier here.

## Cleanup

`play` owns its instances and stops them when it stops. After an abnormal exit,
or to clear leftovers from a previous run, stop only campaign-created instances:

```bash
python3 lib/ctfy_local.py cleanup --competition <competition-id>
```

`cleanup` prints the stopped and failed instance IDs. It never starts or solves
anything, and it does not touch instances the campaign did not create.

## Result categories

Each attempt writes `result.json` under its work directory. Answer states are:

- `recorded` — saved locally, no platform verdict yet.
- `accepted` — platform returned `correct=true`.
- `rejected` — platform returned `correct=false`.
- `submission_failed` — HTTP/protocol failure; excluded from silent handoff.
- `handed_off` — a recorded candidate was written to `result.json`; later rounds
  will not submit it again.

Campaign outcomes use stop reasons such as `done`, `incomplete`, `no_progress`,
`max_steps`, `explore_cap`, `deadline`, `model_error`, `infra_error`,
`blocked_by_infra`, and `interrupt`. `answers_handed_off` is a handoff, not a
verdict; only an accepted platform response or authoritative instance state
counts as solved.

## Resume behavior

Campaign state is a manifest at `runs/<competition>/campaign.json` plus the
per-challenge graphs and attempt directories beneath it. Re-running `play` from
the same working directory resumes the single existing campaign and keeps its
original deadline; it does not start a fresh one. When several campaigns are
resumable, `play` refuses to guess and requires `--competition`.

The first Ctrl-C or `SIGTERM` asks Workers to stop gracefully and preserves
their instances; a second interrupt forces bounded cleanup. The campaign lock
allows only one commander per campaign, so do not run two `play` commands at
once.

## What not to do

- Do not build or rebuild the solver image for this skill.
- Do not create challenge instances or download attachments by hand; the
  scheduler and Workers own that lifecycle.
- Do not edit `campaign.json`, decision logs, or result files to make progress
  look better.
