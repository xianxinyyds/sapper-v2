# CTFY-0001 v3 solver image

This image runs **one challenge per evaluation** in `run_mode=instance` or
`run_mode=qa`. Instance mode uses only the already-started target instance; it
never creates one. QA mode uses the description, questions, and local
attachments, with `target_instance=null` as the normal state. `defence`, `awd`,
`engagement`, and unknown modes are rejected with `unsupported_run_mode` when a
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
| `CTFY_EVAL_MAX_TURNS` | Turn budget when `task.max_turns` is absent or invalid |
| `LLM_FLAVOR` | `anthropic` (Claude Code) or `openai` (Codex) |
| `LLM_API_KEY` | Platform-provided model credential; no personal fallback is used |
| `LLM_MODEL` | Exact model id requested from the platform gateway |
| `LLM_BASE_URL` | Optional provider endpoint override |
| `SAPPER_MAX_STEPS` | Optional lower safety cap on the selected turn budget |
| `SAPPER_MAX_EXPLORE` | Optional explore-dispatch cap; defaults to the selected turn budget |
| `SAPPER_ROUND_MINUTES` | Optional wall-clock round budget; defaults to `165` minutes |
| `CTFY_DISPATCH_TIMEOUT` | Optional per-reason/explore dispatch cap; defaults to `300` seconds |
| `SAPPER_TIMEOUT` | Fallback dispatch cap when `CTFY_DISPATCH_TIMEOUT` is unset |
| `SAPPER_RETRY_MODEL` | Bounded retries for non-timeout transient reason calls |

A positive `task.max_turns` wins over `CTFY_EVAL_MAX_TURNS`; if neither is
available, the default is `8`. `SAPPER_MAX_STEPS` can only reduce that value.
Turn counts are not seconds. Invalid or non-positive budget environment values
produce `config_error`.

The effective absolute deadline is the earliest of the round budget and a parseable
`challenge.close_at`. Auto reserves the last 300 seconds as the **finalization
reserve**: no new reason/explore dispatch starts then, while an in-flight engine is
still bounded by the true deadline. The runner writes a ledger-aware result after
`prepare_graph()` and atomically checkpoints after every reason/explore attempt.
Intermediate checkpoints use `stop_reason=incomplete` and never mark answers
`handed_off`; finalization remains the only handoff transition. A hard SIGKILL is
still outside the cleanup guarantee, but it no longer erases earlier checkpoints.

The runner clears `SAPPER_KEY_POOL` and maps only the platform model variables to
the selected CLI. Credentials are kept in process memory and are not written to
`/ctfy/work` or baked into the image. See [ENV.md](ENV.md) for the configuration
checklist.

## Dry run

A task with `"dry_run": true`, or an environment with `CTFY_EVAL_DRY_RUN=1`,
writes the result before validating the target, platform, or model configuration
and exits `0`:

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

For each round the runner loads only the current challenge briefing, resumes the
existing graph, configures the selected model CLI, prepares attachments, and
runs the single-challenge `auto` loop with the CTFY prompts. Instance attachments
with a null path are downloaded from the current instance. QA attachments must
already exist at their local paths; QA never constructs an instance download.
If the initial challenge GET fails, a matching existing graph is still used for
the result rollup so recorded answers are not overwritten by an empty result.
Exhausted transport retries produce `blocked_by_infra` and exit `0`; HTTP/protocol
errors remain `config_error` with a non-zero exit.

An executor records a candidate immediately:

```bash
sapper answer "FLAG{example}" --question "flag" --submit
```

In instance mode, the answer is recorded before one submission POST. Submission
POSTs are never retried automatically, and executors must not write their own
repeated-POST scripts. A correct response refreshes the current instance, whose
per-question `answered_correctly` state and newly unlocked questions enter the
graph. The static challenge's `my_solved_question_ids` and `my_fully_solved` are
not treated as per-instance progress. An accepted fact is persisted before that
refresh; a later refresh failure cannot roll back `accepted` or remove it from
`solved`.

In QA mode, the same command records the answer before one verdict request to
`POST /api/v1/qa/submissions`, using the task's `competition_id`, `challenge_id`,
`question_id`, and answer. It never calls an instance API. Accepted and rejected
verdicts update the ledger, and accepted answers refresh the challenge. Only a
transport failure leaves the candidate `recorded` for `result.json.answers` handoff
and records `platform_unreachable`; HTTP/protocol failures use `submission_failed`
and are not silently handed off.

Challenge GETs and attachment downloads have bounded retries, including direct
`TimeoutError` response reads. The instance health GET is deliberately
single-attempt. A manual bounded check is available as:

```bash
sapper ctfy-health
```

It reads only the current task's instance, sends no POST, and prints availability
rather than instance credentials. A confirmed missing instance records an
`instance_unavailable` blocker; there is no sleep/poll loop and no instance
creation. If every known visible question is already accepted and no initial or
known gated question remains, instance teardown after an accepted answer can
instead finish as `done`. A refresh failure records `challenge_refresh_failed`.
Static challenge state showing all visible questions solved while
`fully_solved=false`, with no next question visible, records `gating_pending`.
Other transport kinds are `platform_unreachable` and `instance_health_unreachable`;
executor-confirmed DNS failures use `target_dns_unavailable`, and a structured
`status=instance_down` executor fact becomes `instance_unavailable`. Blockers stop
further reason/explore work and preserve recorded or accepted answers.

The CTFY planner supports concise routing for Crypto, Reverse, Misc, Web, Pwn and
AI security, with platform category preferred over task/attachment inference. Misc
includes offline PCAP analysis with capinfos/tshark/ngrep, image steganography with
pngcheck/zsteg/stegseek, audio/video processing with ffprobe/ffmpeg/sox, PDF/QR
extraction, 7zip archives, bounded fcrackzip/john attempts, and local GnuPG inputs.
The image deliberately excludes Sleuth Kit and hashcat. Web/Pwn/Reverse also include
non-privileged nmap connect scans, working Impacket examples, strace/ltrace, patchelf,
gcc/make, gdbserver, ropper, UPX, radare2 and YARA. Crypto adds fpylll, ecdsa,
xortool and explicitly selected offline RsaCtfTool attacks; AI attachments can use
numpy, safetensors and ONNX metadata inspection without model runtimes. Full nuclei
templates, Sage, Ghidra, browsers, Android/multi-arch suites and heavyweight ML
runtimes are not bundled.

## Result and exit semantics

Answer facts have these states:

- `recorded`: saved locally, but no platform verdict was received.
- `accepted`: the platform returned `correct: true`.
- `rejected`: the platform returned `correct: false`.
- `submission_failed`: an HTTP/protocol failure made the submission ineligible for
  silent result handoff; it is excluded from `answers`.
- `handed_off`: a recorded candidate was written to `result.json`; later rounds
  do not automatically submit it again.

`answers` contains only the latest unverified `recorded` candidate for each
question. Accepted, rejected, submission-failed, handed-off, and legacy Arena flags are excluded.
After a successful result write, the exported candidates become `handed_off`.
`solved` combines locally accepted question ids with the platform's solved list.
`fully_solved` comes from the platform challenge response or an instance's
`grace_kind=solved`, not from a model prediction.

`done` is used for a completed graph, authoritative `fully_solved=true`, or the
case where every known visible question is accepted or platform-solved, the
initial gated count is zero, and no known hidden/unresolved question or blocker
remains. Consequently `done` can coexist with `fully_solved=false` when the
static challenge does not expose per-instance completion. Confirmed completion
overrides a stale failure outcome and exits `0`.
An ordinary unfinished run may exit `0` with `incomplete`, `no_progress`,
`max_steps`, `explore_cap`, or `deadline`. Transport-fallback QA candidates produce
`answers_handed_off`; accepted QA verdicts can instead reach `done`. A structured
instance/refresh/gating blocker produces `blocked_by_infra` and exits `0` after writing the result.

Model/executor failures are distinct: `model_error` and `explore_failed` come
from the structured auto outcome and exit non-zero; an unexplained non-zero auto
return uses `auto_failed`. Configuration/protocol failures use `config_error`
or `unsupported_run_mode` and exit non-zero. Exit code `0` alone never means
`done`.

Reason calls retain bounded retries for non-timeout transient errors. Explore
calls and all engine timeouts are not retried wholesale. Planner and executor
prompts receive the per-dispatch hard time budget, which is also checked between
frontier intents so the finalization reserve is not consumed by another dispatch.
A timeout preserves bounded stdout/stderr tails with configured credentials
redacted. Each engine subprocess uses `start_new_session=True`; timeout,
exception, and normal return clean up its **process group** with `SIGKILL` and
bounded drain/reaping. A process that actively daemonizes or otherwise leaves
that group is outside this guarantee. Prompts therefore forbid nohup, background
`&`, wakeups, long polling, and work that waits for a later dispatch. Long
analysis must be split into bounded slices with checkpoints and concluded in
the current dispatch.

## Build and local verification

Build from the repository root:

```bash
docker build -f docker/Dockerfile -t sapper-ctfy:local .
```

Run the standard-library unit tests and local HTTP-platform integration tests:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash -n docker/entrypoint-ctfy.sh
python3 -m compileall -q lib tests
git diff --check
```

The integration tests submit only to a loopback mock platform. They cover
instance and QA handoff, direct timeouts, accepted-answer refresh failure,
instance and gating blockers, graph reuse across rounds, TCP endpoints, process
cleanup, and work-directory credential scans. They do **not** prove real model
solve quality or official platform conformance.

If the official CLI is already installed, run its manifest and local dry-run
checks against the built image:

```bash
ctfy agent check sapper-ctfy:local
```

A local mock pass must not be reported as an official conformance pass; use the
result from `ctfy agent check` itself.
