# CTFY-0001 v3 solver image

This image runs **one already-started challenge instance** per evaluation. It
implements the `instance` run mode only. `qa`, `defence`, `awd`, and `engagement`
are rejected with a non-zero exit and `stop_reason=unsupported_run_mode` when a
result file can be written.

The container does not list challenges, start instances, or sweep multiple
challenges. The legacy `sapper arena` and `sapper bench` commands are not part of
this run path.

## Files and environment

The container starts as an unprivileged user with cwd `/ctfy/work`.

| Path | Purpose |
| --- | --- |
| `/ctfy/work/task.json` | CTFY v3 task briefing |
| `/ctfy/work/attachments/` | Copied or downloaded challenge attachments |
| `/ctfy/work/.sapper/` | Persistent fact graph for later rounds of the same challenge |
| `/ctfy/work/result.json` | Formal result rollup; stdout is not a scoring channel |

| Variable | Meaning |
| --- | --- |
| `CTFY_WORK_DIR` | Work directory; defaults to `/ctfy/work` |
| `CTFY_PLATFORM_URL` | Base URL of the current CTFY platform |
| `CTFY_TOKEN` | Bearer token scoped to the current run |
| `CTFY_EVAL_DRY_RUN` | `1` requests the no-credential conformance fast path |
| `LLM_FLAVOR` | `anthropic` (Claude Code) or `openai` (Codex) |
| `LLM_API_KEY` | Platform-provided model credential; no personal fallback is used |
| `LLM_MODEL` | Exact model id requested from the platform gateway |
| `LLM_BASE_URL` | Optional provider endpoint override |
| `SAPPER_MAX_STEPS` | Optional step budget when `task.max_turns` is null; default `8` |

The runner clears `SAPPER_KEY_POOL` and maps only the platform model variables to
the selected CLI. Credentials are kept in process memory and are not written to
`/ctfy/work` or baked into the image.

## Dry run

A task with `"dry_run": true`, or an environment with `CTFY_EVAL_DRY_RUN=1`,
writes the result before validating the instance, platform, or model
configuration and exits `0`:

```json
{
  "schema_version": 3,
  "answers": {},
  "solved": [],
  "fully_solved": false,
  "stop_reason": "dry_run"
}
```

No platform connection or model call is made. This is important because an
official dry run has no `CTFY_TOKEN` or `LLM_API_KEY`.

## Normal lifecycle

For each round the runner refreshes only the current challenge, resumes the
existing graph, configures the selected model CLI, ensures attachments whose
`path` is null, and runs the single-challenge `auto` loop with the CTFY prompts.
An executor submits an answer immediately:

```bash
sapper answer "FLAG{example}" --question "flag" --submit
```

The answer is recorded before submission. Submission POSTs are never retried
automatically. A correct response refreshes the challenge so gated questions can
enter the graph. Only idempotent GETs and attachment downloads receive bounded
retries.

## Result and exit semantics

Answer facts have these states:

- `recorded`: saved locally, but no platform verdict was received.
- `accepted`: the platform returned `correct: true`.
- `rejected`: the platform returned `correct: false`.
- `handed_off`: a recorded candidate was written to `result.json`; later rounds
  do not automatically submit it again.

`answers` contains only the latest unverified `recorded` candidate for each
question. Accepted, rejected, handed-off, and legacy Arena flags are excluded.
After a successful result write, the exported candidates become `handed_off`.
`solved` combines locally accepted question ids with the platform's solved list;
`fully_solved` comes only from the platform challenge response.

A completed auto run exits `0` with `stop_reason=done`, even when nothing was
solved. An auto failure still writes the rollup, uses `stop_reason=auto_failed`,
and exits non-zero. Configuration/protocol failures write `config_error` when
possible and exit non-zero.

## Build and local verification

Build from the repository root:

```bash
docker build -f docker/Dockerfile -t sapper-ctfy:latest .
```

Run the standard-library unit tests and local HTTP-platform integration tests:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash -n docker/entrypoint-ctfy.sh
python3 -m py_compile \
  lib/ctfy.py lib/ctfy_engine.py lib/codex_engine.py lib/ctfy_run.py
git diff --check
```

The integration tests submit only to a loopback mock platform. They cover an
accepted answer, a TCP endpoint, graph reuse across rounds, gated question sync,
handed-off answers, and scanning the work directory for test credentials. They
do **not** prove real model solve quality or official platform conformance.

Install the official CLI in an isolated environment and run its manifest and
local dry-run checks against the built image:

```bash
python3 -m venv /tmp/ctfy-agent-check
/tmp/ctfy-agent-check/bin/pip install 'ctfy[client,server,node]'
/tmp/ctfy-agent-check/bin/ctfy agent check sapper-ctfy:latest
```

The `server` and `node` extras provide the local probe imports and Docker SDK
needed by `ctfy 0.8.0`. A local mock pass must not be reported as an official
conformance pass; use the result from `ctfy agent check` itself.
