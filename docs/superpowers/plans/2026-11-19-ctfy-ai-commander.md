# CTFY AI Commander Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a one-prompt CTFY campaign mode in which an AI commander chooses challenge strategy while deterministic adapters handle discovery, instances, workers, submissions, capacity, and recovery.

**Architecture:** Add paginated competition discovery and a persistent campaign store, then expose the existing single-challenge local runner as a structured worker. A new `ctfy_commander` builds an authoritative CTFY board, asks the model for bounded `work/hire/fire/stop` decisions, and enforces all lifecycle and capacity constraints outside the model.

**Tech Stack:** Python 3 standard library, existing `ctfy`/`ctfy_local`/`sapper` modules, Docker, pytest/unittest, Markdown skills.

**Spec:** `docs/superpowers/specs/2026-11-19-ctfy-ai-commander-design.md`

## Global Constraints

- Python standard library only in product code; do not add a database, web server, tmux dependency, or new Python package.
- AI chooses strategy. `difficulty`, `solves_count`, and the default 30-minute stall threshold are advice, not a fixed ranking formula or forced kill.
- Do not fall back to deterministic round-robin when the commander model fails.
- First release supports `simple` instance and `qa`; mark `engagement`, AWD, defence, and unknown modes `unsupported` without starting them.
- Platform question progress is authoritative. Container exit 0, `handed_off`, or a model claim is not proof of a solve.
- Never automatically close a reused/pre-existing instance. Cleanup ownership must be explicit.
- Never write tokens, model keys, VPN keys, or answer values into `campaign.json` or `decisions.jsonl`.
- Do not run solver code or untrusted attachments on the host; keep them in the CTFY container.
- Preserve existing `run` CLI behavior and the hosted v3 single-challenge task schema.
- Preserve the pre-existing uncommitted compatibility changes in `lib/ctfy_engine.py`, `lib/ctfy_run.py`, `tests/test_ctfy.py`, and `tests/test_ctfy_run.py`; do not discard or silently rewrite them. Their tests are part of the baseline.
- Run the repository suite as `python3 -m pytest -q tests`, never bare `pytest` (which collects third-party tests under `runs/`).
- Baseline before Task 1: `264 passed, 1029 subtests passed`.

## Review Focus

- Malicious platform IDs and prompt-injection text: path traversal must be rejected and board text must remain data (Tasks 1 and 5).
- Inconsistent/empty pagination and duplicate platform objects: fail rather than loop forever or start duplicates (Tasks 1 and 6).
- Crash after instance creation but before manifest persistence: reconcile the platform instance without creating another (Task 6).
- Submission accepted remotely but followed by a transport timeout: refresh progress and do not blindly replay the POST (Tasks 3 and 4).
- Instance TTL shorter than a requested slice, deadline during a slice, and repeated Ctrl-C: clamp budgets and perform bounded, idempotent cleanup (Tasks 5, 6, and 7).

---

### Task 1: CTFY Competition Discovery and Safe IDs

**Files:**
- Modify: `lib/ctfy.py:44-138`
- Test: `tests/test_ctfy.py`

**Interfaces:**
- Consumes: existing `CtfyClient._request()` and `_segment()`.
- Produces:
  - `require_id(value: object, label: str = "id") -> str`
  - `CtfyClient.list_competitions(*, phase: str | None = None, page_size: int = 100) -> list[dict]`
  - `CtfyClient.list_challenges(competition_id: str, *, page_size: int = 100) -> list[dict]`

- [ ] **Step 1: Write failing discovery and ID tests**

Add `CtfyDiscoveryTests` using `LocalPlatform`:

- `test_list_competitions_paginates_with_phase`: queue two pages with `total=3`, page sizes 2 and 1; assert all three IDs and exact `phase=running`, `limit`, and `offset` query values.
- `test_list_challenges_paginates_and_returns_objects`: two pages, including difficulty and solves fields; assert unchanged field values and order.
- `test_empty_page_before_reported_total_fails`: `total=2` with `items=[]` raises `CtfyError` rather than looping.
- `test_malformed_page_and_duplicate_ids_fail`: non-object items or duplicate object IDs raise `CtfyError`.
- `test_require_id_rejects_path_traversal`: accept `AUTHORED-CDB35578` and UUIDs; reject empty, `../x`, `a/b`, `a\\b`, `.`, `..`, control characters, and values over 128 characters.

IDs use `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$` plus explicit rejection of `.` and `..`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest -q tests/test_ctfy.py -k 'CtfyDiscoveryTests'`
Expected: FAIL with missing `require_id` or missing client methods.

- [ ] **Step 3: Implement paginated discovery and ID validation**

Implement the three interfaces above. Use `urllib.parse.urlencode` for queries. Start at `offset=0`, advance by the number of returned items, stop at reported `total` or a short page, and reject an empty/invalid page before the reported total. Require unique nonempty object IDs within the aggregate result.

- [ ] **Step 4: Run focused and full CTFY protocol tests**

Run: `python3 -m pytest -q tests/test_ctfy.py`
Expected: all tests in the file PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-11-19-ctfy-ai-commander-design.md docs/superpowers/plans/2026-11-19-ctfy-ai-commander.md
git commit -m "docs: design CTFY AI commander"
git add lib/ctfy.py tests/test_ctfy.py
git commit -m "feat: add CTFY competition discovery"
```

### Task 2: Persistent Campaign State, Deadlines, and Locks

**Files:**
- Create: `lib/ctfy_campaign.py`
- Create: `tests/test_ctfy_campaign.py`

**Interfaces:**
- Consumes: `ctfy.require_id()` from Task 1.
- Produces:
  - `resolve_deadline(ends_at: str | None, minutes: int | None, *, now: float) -> float`
  - `make_campaign(competition: dict, *, deadline: float, stall_minutes: int, max_workers: int, now: float) -> dict`
  - `CampaignStore(root: pathlib.Path)`
  - `CampaignStore.load() -> dict`
  - `CampaignStore.save(state: dict) -> None`
  - `CampaignStore.append_decision(record: dict) -> None`
  - `CampaignStore.lock(*, blocking: bool = True) -> contextmanager`
  - `CampaignStore.challenge_lock(challenge_id: str) -> contextmanager`

- [ ] **Step 1: Write failing deadline and persistence tests**

Cover:

- Earlier of `ends_at` and `now + minutes`; either one alone; `Z` ISO parsing.
- Neither reliable `ends_at` nor `minutes` raises `CtfyError`; invalid/past deadlines raise rather than running indefinitely.
- `make_campaign()` emits `schema_version=1`, competition ID/title, fixed deadline, 30-minute default, empty `challenges`, `workers`, and `instances` mappings.
- Save/load uses atomic replace and preserves a deadline across reopen; a restart does not receive a new budget.
- A nonblocking second campaign lock raises `CtfyError`; separate challenge IDs have independent locks; `../x` is rejected by `challenge_lock`.
- `decisions.jsonl` appends one valid JSON object per line.
- Normal state/decision writes do not contain environment values named `CTFY_TOKEN` or `LLM_API_KEY`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest -q tests/test_ctfy_campaign.py`
Expected: collection FAIL with `ModuleNotFoundError: ctfy_campaign`.

- [ ] **Step 3: Implement campaign persistence**

Use `fcntl.flock`, `contextlib.contextmanager`, and the existing atomic-JSON style. Keep all timestamps as Unix seconds in JSON for deterministic comparisons. Hold the campaign lock for the whole commander run; use per-challenge locks for preparation, startup, and handoff. Do not serialize environment variables or full challenge/service credential objects into campaign state.

- [ ] **Step 4: Run campaign tests**

Run: `python3 -m pytest -q tests/test_ctfy_campaign.py`
Expected: all new tests PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/ctfy_campaign.py tests/test_ctfy_campaign.py
git commit -m "feat: add persistent CTFY campaign state"
```

### Task 3: Structured, Reusable Single-Challenge Worker

**Files:**
- Modify: `lib/ctfy_local.py:17-390`
- Modify: `lib/ctfy_engine.py` (preserve the existing custom-`SAPPER_BIN` fix)
- Modify: `lib/ctfy_run.py` (preserve the existing instance-detail fallback)
- Test: `tests/test_ctfy_local.py`
- Test: `tests/test_ctfy.py`
- Test: `tests/test_ctfy_run.py`

**Interfaces:**
- Consumes: `CtfyClient.list_challenges()` and `require_id()` from Task 1.
- Produces:
  - `get_challenge_for_run(client: CtfyClient, challenge_id: str, competition_id: str) -> dict`
  - `run_challenge(args: argparse.Namespace, *, client: CtfyClient | None = None, challenge: dict | None = None, on_event: collections.abc.Callable[[str, dict], None] | None = None) -> dict`
  - Structured outcome keys: `work_dir`, `instance_id`, `created_instance`, `prepare_only`, `container_exit_code`, `result`, `handoff`.
  - `on_event("prepared", data)` and `on_event("container", data)` callbacks for live attempt tracking.
  - `run` options `--engine PATH` and repeatable `--add-host HOST:IP`.

- [ ] **Step 1: Write failing worker compatibility tests**

Add tests proving:

- `get_challenge_for_run` falls back only on `code="go_unsupported_feature"`, selects the exact ID from `list_challenges(competition_id)`, and does not fallback on other errors.
- A newly started instance returns `created_instance=True`; a reused instance returns `False`; `--keep-instance` stops neither.
- `run_challenge` returns the existing result/handoff fields and `run_local` still prints that object and returns the container exit code.
- `--engine /tmp/engine` copies the file executable into the work directory and sets container `SAPPER_BIN=/ctfy/work/.ctfy-engine`, without placing secrets in argv.
- Docker args include a read-only bind of the host `lib/` to `/opt/sapper/lib`, plus each `--add-host` mapping; this lets the current VPS use tested source without an image rebuild.
- `SAPPER_BIN` is forwarded through `_FORWARDED_ENV`.
- Re-running handoff for the same question+answer with an existing boolean verdict does not POST again; a prior transport-uncertain candidate is not blindly replayed.
- A new answer for the same question remains a distinct candidate.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest -q tests/test_ctfy_local.py -k 'fallback or structured or engine or handoff_replay or add_host'`
Expected: FAIL because the structured API/options are absent.

- [ ] **Step 3: Refactor the host worker without changing CLI semantics**

Move the body of `run_local` into `run_challenge`; keep `run_local` as client creation, JSON printing, and exit-code adaptation. Validate path-bound IDs. Emit `prepared` after task preparation with `work_dir`, `instance_id`, and `created_instance`; emit `container` with the Docker name before execution.

Always mount the current host `lib/` read-only for this source-driven local runner. Copy `--engine` to `.ctfy-engine` and override only the child container environment. Preserve pending-instance cleanup on startup failure and all current `--keep-instance`/shared behavior.

Make handoff merging idempotent by question+answer. Persist transport-uncertain outcomes as unresolved, refresh platform state in the caller, and do not automatically POST them again.

- [ ] **Step 4: Run all local-runner and compatibility tests**

Run: `python3 -m pytest -q tests/test_ctfy_local.py tests/test_ctfy.py tests/test_ctfy_run.py`
Expected: all tests PASS, including the pre-existing compatibility tests.

- [ ] **Step 5: Commit**

```bash
git add lib/ctfy_local.py lib/ctfy_engine.py lib/ctfy_run.py tests/test_ctfy_local.py tests/test_ctfy.py tests/test_ctfy_run.py
git commit -m "refactor: expose structured CTFY worker outcomes"
```

### Task 4: Authoritative CTFY Board and Meaningful Progress

**Files:**
- Create: `lib/ctfy_commander.py`
- Create: `tests/test_ctfy_commander.py`

**Interfaces:**
- Consumes: Task 1 data shapes, Task 2 campaign records, Task 3 work directories/results.
- Produces:
  - `build_board(challenges: list[dict], instances: list[dict], state: dict, *, now: float) -> list[dict]`
  - `render_ctfy_board(rows: list[dict]) -> str`
  - Campaign challenge records updated with `first_seen_at`, `last_progress_at`, `platform_solved_question_ids`, latest `work_dir`, attempt time, and result summary.

- [ ] **Step 1: Write failing board and progress tests**

Cover:

- Board rows preserve `id`, `name`, `category`, `difficulty`, `solves_count`, `scoring`, `my_team_authored`, team solved IDs, instance ID/status/expiry/ownership, Worker state, and accumulated minutes.
- Input ordered hard/low-solves before easy/high-solves remains in that order; no deterministic priority score or hidden sorting is introduced.
- Solved, closed, engagement, defence, and unknown-mode rows expose the correct `status`/`unsupported` reason.
- Solver-created vuln/cred/loot/flag/other confirmed facts and completed intents that produce new facts update `last_progress_at`; creator `ctfy` challenge/question/attachment sync records, unfinished intents, and duplicate facts do not.
- Newly observed `answered_correctly=true` or team-solved question IDs update progress at the supplied `now`.
- At 29 minutes `stalled=False`; at 30 and 31 minutes `stalled=True` with the default threshold. Repeated stalled decisions increment `stalled_decisions`; new meaningful progress resets it to zero.
- A transport-uncertain handoff does not count as accepted; a later platform `answered_correctly=true` does, without another submission.
- `render_ctfy_board` returns JSON containing no answer values or service credentials.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest -q tests/test_ctfy_commander.py`
Expected: collection FAIL with `ModuleNotFoundError: ctfy_commander`.

- [ ] **Step 3: Implement board projection**

Read only bounded metadata from task/result/handoff/fact files. Treat solver-created facts as progress while excluding CTFY synchronization metadata. Preserve, rather than decrease, the latest progress timestamp. Track platform solved-ID transitions in campaign state. Compute `stalled` as `now - last_progress_at >= stall_minutes * 60`, using `first_seen_at` when no progress exists.

Render a compact JSON board, not the Arena `submitted` table. Never include fact `value`, answer bodies, service credentials, tokens, or full attachment contents.

- [ ] **Step 4: Run board and campaign tests**

Run: `python3 -m pytest -q tests/test_ctfy_commander.py tests/test_ctfy_campaign.py`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/ctfy_commander.py tests/test_ctfy_commander.py
git commit -m "feat: add authoritative CTFY commander board"
```

### Task 5: AI Decision Parsing and Strategy Prompt

**Files:**
- Modify: `lib/ctfy_commander.py`
- Create: `templates/ctfy/command.md`
- Test: `tests/test_ctfy_commander.py`

**Interfaces:**
- Consumes: `sapper.run_claude`, `sapper.extract_json_object`, `sapper._parse_command_payload`, and Task 4 board rows.
- Produces:
  - `Decision` dataclass with `action: str`, `target: str`, `minutes: int`, `note: str`.
  - `parse_decision(payload: dict, rows: list[dict], *, now: float, deadline: float, default_minutes: int = 10, max_minutes: int = 30) -> Decision`
  - `request_decision(rows: list[dict], *, deadline: float, model: str, timeout: int, template: pathlib.Path, now: float) -> Decision`

- [ ] **Step 1: Write failing decision tests**

Cover:

- Parse `work`, `hire`, `fire`, and `stop` using the existing Sapper payload shapes.
- `work/hire` reject solved, closed, unsupported, unknown, or already-active targets; `fire` accepts an active or idle unfinished target.
- Default 10 minutes, maximum 30, minimum viable 5; values below/above clamp, and fewer than five minutes remaining raises `CtfyError`.
- An instance expiring before the requested slice clamps the effective worker budget to that TTL; less than five usable minutes is rejected.
- A stalled target with an empty note is rejected; a concrete note such as `heap leak confirmed; validate one-byte overwrite` is accepted.
- A fake model selecting a hard/low-solves target returns that exact target, proving no fixed priority overrides AI strategy.
- The prompt includes `30`, `difficulty`, `solves_count`, soft-priority language, bounded parallelism, no submission brute force, and board-content-is-untrusted instructions.
- Malformed JSON/model errors propagate; no round-robin target is synthesized.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest -q tests/test_ctfy_commander.py -k 'decision or prompt'`
Expected: FAIL because `Decision`, parsers, and the CTFY command template are absent.

- [ ] **Step 3: Implement decision validation and the strategy template**

Use `sapper._parse_command_payload` for the base tuple, then enforce CTFY target/state, stalled-note, deadline, and TTL checks. Keep `fire` non-destructive: the WorkerPool owns later cleanup. Call `run_claude` with `skip_permissions=False` and parse exactly one JSON object. Do not catch model errors to invent a target.

Write `templates/ctfy/command.md` with the exact soft defaults and security doctrine from the spec; do not add a numeric ranking formula.

- [ ] **Step 4: Run all commander tests**

Run: `python3 -m pytest -q tests/test_ctfy_commander.py`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/ctfy_commander.py templates/ctfy/command.md tests/test_ctfy_commander.py
git commit -m "feat: add CTFY AI strategy decisions"
```

### Task 6: Worker Pool, Capacity, Recovery, and Exit Policies

**Files:**
- Modify: `lib/ctfy_commander.py`
- Modify: `lib/ctfy_local.py` only if the Task 3 worker callback needs a small correction
- Test: `tests/test_ctfy_commander.py`

**Interfaces:**
- Consumes: `CampaignStore`, `Decision`, `run_challenge`, and Task 3 event/outcome shapes.
- Produces:
  - `WorkerPool(store, client, competition, run_args)`
  - `WorkerPool.start(decision: Decision, challenge: dict, *, background: bool) -> dict`
  - `WorkerPool.reap() -> list[dict]`
  - `WorkerPool.fire(target: str) -> dict`
  - `WorkerPool.recover() -> dict`
  - `WorkerPool.cleanup(reason: str) -> dict`, where reason is `stop`, `deadline`, `interrupt`, `model_error`, or `infra_error`
  - `worker_main(argv: list[str] | None = None) -> int` for one locked campaign worker process.

- [ ] **Step 1: Write failing pool and recovery tests**

Using fake clients, temporary campaign stores, mocked `Popen`, and fake outcomes, cover:

- Same-challenge concurrent start is rejected; a second distinct challenge is allowed only within worker/instance caps. Two ready/running platform instances for the same challenge fail closed rather than selecting one arbitrarily.
- Reported instance cap 2 permits two and rejects a third before any start POST.
- Missing instance cap falls back to one active instance and serial instance work; capacity rejection refreshes instances and does not retry in a loop.
- `work` waits for one slice; `hire` returns a live background record. `fire` on an active worker sets `cancel_after_slice` without killing it; idle `fire` marks parked.
- Reaping records result/handoff/work_dir/minutes, then closes an idle instance only when it is campaign-owned and cancellation/cleanup requires it. An event-order assertion proves graph/result save → handoff → platform refresh → manifest update → close.
- `stop` and `deadline` close campaign-owned instances after handoff; `interrupt`, `model_error`, and `infra_error` preserve them and report IDs/expiry. Reused instances are never closed.
- Recovery with a live recorded worker adopts it; a stale PID/unknown command line is reconciled through attempt files and `list_instances`, not another start POST.
- A crash record with no instance manifest entry but a platform-ready matching instance adopts that instance and records ownership conservatively as unknown/reused.
- Starting a replacement instance creates a new attempt directory and never copies or submits flag candidates from an older instance attempt.
- Repeated cleanup and a second Ctrl-C are bounded and idempotent; unresolved handoff is not submitted twice.
- Worker signal handling requests bounded Docker cleanup and writes an interrupted outcome without logging secrets.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest -q tests/test_ctfy_commander.py -k 'pool or capacity or recovery or cleanup or worker_signal'`
Expected: FAIL because `WorkerPool` and `worker_main` are absent.

- [ ] **Step 3: Implement the process-based worker adapter**

Launch `python3 lib/ctfy_commander.py worker` with argv lists and inherited environment, never shell interpolation. Redirect output to per-attempt logs. The child holds `CampaignStore.challenge_lock`, calls `run_challenge(..., keep_instance=True, on_event=...)`, and atomically writes prepared/container/outcome records so the parent can monitor live graph progress and Docker names.

Use `Popen.poll()`/`wait()` for reaping. Track PIDs only as hints; during recovery verify process identity and reconcile against platform instances. Do not import tmux or the Arena commander stack.

Implement graceful first-interrupt handling and bounded forced cleanup. Use Task 3 idempotent handoff for any final checkpoint. Close only IDs explicitly marked `created_by_campaign=True`; preserve unknown/reused ownership.

- [ ] **Step 4: Run commander and local worker tests**

Run: `python3 -m pytest -q tests/test_ctfy_commander.py tests/test_ctfy_local.py`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/ctfy_commander.py lib/ctfy_local.py tests/test_ctfy_commander.py
git commit -m "feat: manage CTFY workers and instance capacity"
```

### Task 7: Campaign Loop and `play` / `cleanup` CLI

**Files:**
- Modify: `lib/ctfy_commander.py`
- Modify: `lib/ctfy_local.py:349-390`
- Test: `tests/test_ctfy_commander.py`
- Test: `tests/test_ctfy_local.py`

**Interfaces:**
- Consumes: all prior interfaces.
- Produces:
  - `select_competition(competitions: list[dict], competition_id: str = "") -> dict`
  - `Commander.run() -> int`
  - `play(args: argparse.Namespace) -> int`
  - `cleanup(args: argparse.Namespace) -> int`
  - CLI: `ctfy_local.py play [--competition ID] [--minutes N] [--stall-minutes 30] [--max-workers N]`
  - CLI: `ctfy_local.py cleanup --competition ID`

- [ ] **Step 1: Write failing end-to-end loop tests with fakes**

Use fake platform/model/pool implementations to prove:

- Exactly one running, registered, visible-challenge competition is auto-selected; zero or multiple candidates fail safely unless `--competition` disambiguates.
- An existing campaign keeps its competition and original deadline even if other competitions are available.
- Each iteration refreshes challenges/instances, reaps workers, builds the board, executes the model-selected action, saves state, and appends a redacted decision record.
- `stop` and deadline invoke `cleanup("stop"/"deadline")`; all solved/closed/unsupported with no workers ends without starting anything.
- Model/API failure starts no new target, uses bounded backoff, then enters `model_error`/`infra_error` cleanup rather than round-robin.
- SIGINT/SIGTERM invokes `cleanup("interrupt")` once and preserves instances; the second interrupt follows the bounded forced path.
- `cleanup --competition` stops only manifest-owned leftover instances and never starts a worker.
- Parser tests show `play` needs no challenge ID and defaults to 30-minute stall guidance and two maximum workers, still capped by the platform.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest -q tests/test_ctfy_commander.py tests/test_ctfy_local.py -k 'play or campaign_loop or select_competition or cleanup_command'`
Expected: FAIL because campaign CLI and loop are absent.

- [ ] **Step 3: Implement discovery, loop, and CLI wiring**

`select_competition` filters `phase="running"`, `is_registered=True`, and a positive/non-null challenge count; an explicit ID must still be validated against the visible competition. Before fresh discovery, scan `runs/*/campaign.json`: exactly one resumable manifest is reopened; multiple manifests require `--competition` rather than guessing. `play` then creates/reopens `runs/<competition>/campaign.json` under the campaign lock and constructs the real pool/model adapter.

Default `--stall-minutes=30`, `--max-workers=2`, `--poll-seconds=30`, and model from `LLM_MODEL`/`SAPPER_MODEL`; pass worker `--image`, `--engine`, `--add-host`, turns, dispatch/startup timeouts through a shared parser helper. No secret values appear in argv.

Loop until deadline or no viable work. Refresh before every decision, enforce a bounded model-error backoff, and never start new work while the model/API is unavailable. Register signal handlers only in the CLI entrypoint, not on import.

- [ ] **Step 4: Run complete repository tests**

Run: `python3 -m pytest -q tests`
Expected: all old and new tests PASS; 0 failures.

- [ ] **Step 5: Commit**

```bash
git add lib/ctfy_commander.py lib/ctfy_local.py tests/test_ctfy_commander.py tests/test_ctfy_local.py
git commit -m "feat: add autonomous CTFY play command"
```

### Task 8: One-Prompt Skill and Operator Documentation

**Files:**
- Create: `ctfy-play/SKILL.md`
- Modify: `SKILL.md:21-64`
- Modify: `install.sh:1-94`
- Modify: `docker/CTFY.md`
- Create: `tests/test_ctfy_play_skill.py`

**Interfaces:**
- Consumes: Task 7 CLI.
- Produces: installable `ctfy-play` skill triggered by “打比赛” while leaving `sapper-setup` unchanged.

- [ ] **Step 1: Write failing skill/install tests**

Assert:

- `ctfy-play/SKILL.md` has frontmatter, includes “打比赛”, invokes `python3 lib/ctfy_local.py play`, and contains no real token/API key or hard-coded challenge/instance ID.
- The skill tells the operator agent not to read the challenge for the solver or hand-write payloads, and to expose AI decisions/results faithfully.
- The documented VPS path sources `${CTFY_ENV_FILE:-$HOME/.config/ctfy/env}` only when present, maps existing OpenAI session variables to `LLM_*`, uses `deepseek-v4-pro` only as the current test default, and never prints secret values. The committed skill contains neither `/root` nor real credential values.
- The skill forwards existing `openai_http_engine.py` via `--engine` when present and derives `--add-host` mappings at runtime from `LLM_BASE_URL` plus `getent ahostsv4`; it contains no hard-coded gateway IP and does not rebuild the image.
- With `HOME` pointed at a temp directory, `bash install.sh` creates both `sapper-setup` and `ctfy-play` symlinks; `--uninstall` removes both and leaves project graph state untouched.
- `bash -n install.sh` succeeds.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest -q tests/test_ctfy_play_skill.py`
Expected: FAIL because the skill and installer support are absent.

- [ ] **Step 3: Implement the dedicated skill and documentation**

Keep the skill as a thin launcher around `play`, not a second scheduler. Explain optional `--minutes`, `--stall-minutes`, `--max-workers`, `cleanup`, result categories, and resume behavior. Use `$HOME`-relative optional credentials and runtime gateway resolution; no host-specific paths or IP literals in committed files. Update root CTFY docs to distinguish single-challenge hosted mode from AI-commanded local campaign mode.

Add installer creation/removal of `~/.claude/skills/ctfy-play`; do not change global hooks or the existing `sapper-setup` link behavior.

- [ ] **Step 4: Run skill tests and full suite**

Run: `python3 -m pytest -q tests/test_ctfy_play_skill.py && python3 -m pytest -q tests`
Expected: both commands PASS.

- [ ] **Step 5: Commit**

```bash
git add ctfy-play/SKILL.md SKILL.md install.sh docker/CTFY.md tests/test_ctfy_play_skill.py
git commit -m "feat: add one-prompt CTFY play skill"
```

### Task 9: VPS Live Validation and Handoff

**Files:**
- Modify: `docs/superpowers/handoff/2026-11-19-vps-cut-the-clock-handoff.md`
- Create: `docs/superpowers/handoff/2026-11-19-ctfy-ai-commander-validation.md`

**Interfaces:**
- Consumes: the complete Tasks 1-8 implementation and existing root-only VPS environment file.

- [ ] **Step 1: Run preflight and static secret checks on the tested revision**

Run the full local suite and inspect the diff. Sync only reviewed source/template/skill files to `/root/workspace/sapper-c870444`; do not rebuild the image or copy local credentials. On the VPS, load `/root/.config/ctfy/env` and verify variable presence without printing values.
Expected: all tests PASS; no secrets in synced diffs, campaign files, or command lines.

- [ ] **Step 2: Exercise the actual one-prompt entrypoint**

Invoke the installed `ctfy-play` behavior with only “打比赛” as the user instruction. Use the test competition’s existing model/session configuration and a bounded test budget. Do not supply a challenge ID, read the challenge for the Worker, or write a payload.
Expected: competition and paginated challenge discovery succeed; the already-solved Cut the Clock row is skipped; AI selects a supported unfinished target and records a strategy note.

- [ ] **Step 3: Verify autonomous Worker and authoritative submission evidence**

Observe campaign state, decisions, prepared task/attachments, Worker logs, result, and handoff. Query platform progress independently.
Expected: no manual exploit assistance; correct submissions are marked by the platform; incomplete/model/infra outcomes remain honest.

- [ ] **Step 4: Test stall advice and restart reconciliation**

Run a bounded separate test with `--stall-minutes 1`, create no duplicate instance, and wait for a stalled board row. Restore the default 30 afterward. Stop and restart the Commander during an idle/reconcilable state.
Expected: the row becomes stalled; AI may switch or justify continuing; restart retains deadline and adopts/reuses state without a second matching instance or duplicate submission.

- [ ] **Step 5: Test cleanup and write evidence**

Exercise explicit campaign cleanup and verify only instances created by this validation campaign are closed. Record commands, outcomes, platform evidence, limitations, and any deferred issues in the validation handoff; do not include tokens, model keys, VPN material, or answer values.

Run: `python3 -m pytest -q tests`
Expected: final suite PASS and VPS state matches the report.

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/handoff/2026-11-19-vps-cut-the-clock-handoff.md docs/superpowers/handoff/2026-11-19-ctfy-ai-commander-validation.md
git commit -m "docs: record CTFY AI commander validation"
```

## Plan Self-Review Notes

- Spec sections 1-3 map to Tasks 5, 7, and 8; discovery to Tasks 1 and 7; persistence/deadlines to Task 2; worker compatibility to Task 3; board/progress to Task 4; process/capacity/recovery/cleanup to Task 6; live acceptance to Task 9.
- No task introduces a fixed challenge score or forced 30-minute kill. AI selection is tested with a choice that contradicts naive difficulty ordering.
- All later consumers use the exact interfaces declared in earlier task Interface blocks. `run_local` remains the compatibility wrapper around `run_challenge`.
- Every Review Focus item has at least one owning test. The live test verifies behavior that mocks cannot prove, without treating a soft stall suggestion as a deterministic switch requirement.
