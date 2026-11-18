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

## Offline Web tooling

All Web analysis dependencies are installed at image build time. Runtime containers
must not download packages, gadget chains, wordlists, or other dependencies, and
must not run package installers to supplement the image.

- **HTTP/2 differential analysis:** use `nghttp` for explicit challenge requests and
  `httpx.Client(http2=True)` or `h2` for scripted protocol comparisons.
- **Session and WebSocket scripts:** use `httpx` for session/state checks and
  `websockets` for WebSocket clients against the current challenge only.
- **Flask sessions:** decode with `flask-unsign --decode --cookie '<cookie>'` and
  sign reviewed test data with `flask-unsign --sign --cookie "{'logged_in': True}"
  --secret 'test-only-secret'`.
- **PHP analysis:** list gadgets with `phpggc -l`, generate a selected PHPGGC chain
  with `phpggc <chain> [parameters]`, and use `php` only for minimal, reviewed,
  standalone snippets.

Do not start or execute a complete untrusted attachment project. Extract and inspect
attachments, then run only the minimal reviewed snippets needed to reproduce a
specific behavior.

PHPGGC is the upstream `master` snapshot at commit
[`f8aebde3a1abb88b02042fd12a71b4c61d6cfe2c`](https://github.com/ambionics/phpggc/commit/f8aebde3a1abb88b02042fd12a71b4c61d6cfe2c)
(the upstream repository has no tags), licensed under Apache-2.0. The build context
archive comes from
`https://codeload.github.com/ambionics/phpggc/tar.gz/f8aebde3a1abb88b02042fd12a71b4c61d6cfe2c`;
`docker/phpggc.tar.gz` has SHA256
`566d9f270585de42effc5e407070c0c815fefc30bb8842595e7367b37cc1eeba`.
It contains the root `phpggc` executable and no `.git`, `.venv`, `vendor`, or
`node_modules` tree.

## Offline Pwn tooling

The retained `sapper-ctfy-web-test:latest` image is reused without a Dockerfile
change. `checksec`, `gdb`, `strace`, `patchelf`, `gcc`, `ROPgadget`, `ropper`,
pwntools, `one_gadget`, `seccomp-tools`, and `pwninit` support local ELF
inspection, minimal fixture compilation, gadget analysis, loader/libc matching,
and bounded debugging. Runtime containers must not download packages, libc or
loader files, gadgets, or other dependencies, and must not run package
installers. Apply `patchelf` and `pwninit` only to copies in a separate work
directory; supply the existing local `--libc` and `--ld` to `pwninit`, preserve
the original attachment hash, and never fetch a missing runtime.

Execute only a reviewed, real standalone ELF with staged input, a clean
environment, CPU/address-space/file-size limits, new-socket rejection, and a hard
timeout; do not start an untrusted attachment project. For example, after
verifying the patched copy with `readelf` and `patchelf`:

```bash
env -i PATH=/usr/local/bin:/usr/bin:/bin TERM=xterm \
  DEBUGINFOD_URLS= DEBUGINFOD_PROGRESS=0 \
  timeout 5s prlimit --cpu=5:5 --as=268435456:268435456 \
  --fsize=1048576:1048576 \
  strace -f -qq -o work/pwn1.strace -e trace=socket,socketcall \
  --inject=socket:error=EPERM --inject=socketcall:error=EPERM \
  ./work/pwn1_patched < work/input.txt
```

These controls are not a strong sandbox. A process running with the same UID
(同 UID) as the caller can still access that user's files and other permitted
resources, so use a disposable unprivileged container without credentials or
network access when execution isolation is required. The `strace`
`socket`/`socketcall` EPERM injection blocks those new-socket syscalls; it does
not prove that an arbitrary ELF is sandboxed.

Run the offline known-answer smoke from the repository root:

```bash
docker run --rm --network none --user agent \
  --entrypoint python3 -v "$PWD/tests:/tests:ro" -w /tmp \
  sapper-ctfy-web-test:latest /tests/pwn_smoke.py
```

`tests/pwn_smoke.py` compiles only self-authored temporary fixtures and checks
the toolchain, stack, loopback transport, format-string writes, and current-glibc
SafeLinking/tcache behavior. A successful run has all five groups `PASS` and
exits `0`; any failure or version skip is nonzero. These local known answers do
not validate a real challenge target, older glibc versions, or solve rates.

## Offline Reverse tooling

The retained `sapper-ctfy-web-test:latest` image includes the Reverse tools at
these existing paths: `/usr/bin/file`, `/usr/bin/sha256sum`, the binutils
commands `/usr/bin/strings`, `/usr/bin/readelf`, `/usr/bin/nm`, and
`/usr/bin/objdump`, `/usr/bin/r2` (radare2), `/usr/bin/upx` (the installed
`upx-ucl` alternative), `/usr/bin/yara`, `/usr/bin/gdb`, `/usr/bin/strace`, and
`/usr/bin/ltrace`. GCC and Python support self-authored fixtures and independent
decoders; z3/sympy are used only after a local import availability check.
Runtime containers must not download packages, rules, symbols, or other tools.

Start with static evidence: preserve file/hash output, `strings -a -t x`,
sections, symbols, relocations, disassembly, and targeted radare2 xrefs. Translate
a target VA to a file offset through its file-backed ELF section before
extracting bytes. Independently decode constants and validate normal, wrong, and
boundary inputs; a flag-shaped `strings` hit or YARA match is only a lead. Record
unknown stripped regions and unresolved indirect jumps rather than claiming
coverage.

For a reviewed, real standalone challenge ELF, use only a separate copy and
staged input with the complete controlled wrapper:

```bash
env -i PATH=/usr/local/bin:/usr/bin:/bin TERM=xterm \
  DEBUGINFOD_URLS= DEBUGINFOD_PROGRESS=0 \
  timeout 10s prlimit --cpu=10:10 --as=268435456:268435456 \
  --fsize=1048576:1048576 \
  strace -f -qq -o work/rev1.strace -e trace=socket,socketcall \
  --inject=socket:error=EPERM --inject=socketcall:error=EPERM \
  ./work/rev1 < work/input.txt
```

This is not a strong sandbox: the same UID can still access that user's files and
permitted processes. Use a disposable unprivileged container without credentials
or network access when isolation is required. Live GDB and `strace -f` have a
**ptrace conflict** and must not be combined; ltrace also depends on ptrace and
is not a way to remove socket injection. Direct gdb/ltrace inspection is allowed
for self-authored trusted fixtures inside the required `--network none`
container. For any other ELF, live GDB or ltrace requires an externally verified,
`--network none`, credential-free, non-root, disposable container while retaining
resource/time limits. If that environment cannot be verified, record a blocker
and retain static analysis or the complete strace wrapper; do not assume the
solver container can start Docker.

Run UPX only on copies with separate `upx -9 -o packed ...` and
`upx -d -o unpacked ...` outputs. Preserve the original hash and compare
sections, imports, entropy, and behavior; UPX does not identify every packer. For
a custom VM, save the bytecode VA/file-offset mapping, dispatcher and handler
disassembly, per-opcode stack/register transitions, and an independent
interpreter's output. Unknown opcodes must fail explicitly rather than being
skipped or converted into a success marker.

Run the offline known-answer smoke from the repository root:

```bash
docker run --rm --network none --user agent \
  --entrypoint python3 -v "$PWD/tests:/tests:ro" -w /tmp \
  sapper-ctfy-web-test:latest /tests/reverse_smoke.py
```

`tests/reverse_smoke.py` creates only temporary self-authored C/Python fixtures
and evidence. Success requires `PASS static`, `PASS dynamic`, and `PASS pack-vm`,
with no `FAIL` or `SKIP`, and exit `0`. These known answers exercise the local
x86-64 toolchain only; they do not establish real Reverse challenge A/B results,
other platforms, official platform conformance, or solve rates.

## Offline Crypto tooling

The retained `sapper-ctfy-web-test:latest` image is reused without a Dockerfile
change. Its actual offline Crypto dependencies are pycryptodome 4.4.0, ecdsa
0.19.2, fpylll 0.6.4, sympy 1.14.0, RsaCtfTool 0.1.0, xortool 1.1.0, and
OpenSSL 3.6.5. Runtime containers must not download packages, oracle data, or
other dependencies and must not run package installers.

Select an attack only after recording parameters, encoding, observations, and a
local known-answer oracle. RsaCtfTool runs exactly one explicitly selected local
attack, for example `RsaCtfTool --attack wiener --publickey work/key.pub
--private`; preserve its raw output, parse the candidate key, and require
独立验证 by known-ciphertext decryption rather than trusting its exit code.
禁止 `--attack all`。禁止 `factordb`。禁止 `sendtofdb`。Lattice and ECDSA checks
must similarly verify a known short vector or recovered private scalar, validate
curve/order/generator parameters, and reject invalid inputs explicitly.

Every oracle has a recorded query count and a predeclared 预算. After ECB mode,
block size, and suffix length are established, secret-suffix recovery uses at
most `257L` queries (`4112` for `L=16`), requires one unique candidate per byte,
and checkpoints every byte. For one block, CBC padding recovery uses
4097 recovery-counter queries (one valid-padding baseline plus 4096 candidate
probes) and one separate invalid-padding observation: `4097 + 1 = 4098` total.
The generic total is `2 + 4096B` only with unique candidates and immediate closure on
ambiguity. Any future disambiguation requires a separately declared bounded `D`
term (`2 + 4096B + D`); this smoke adds no disambiguation and closes its
ambiguous-first-byte negative without selecting a candidate. Non-deterministic
ECB and uniform CBC errors close recovery instead of producing candidates. CBC
bit-flip is verified independently from padding recovery. CTR nonce reuse
supports confidentiality recovery only: CTR
ciphertext XOR is not a GHASH or other authentication bypass. The smoke checks
GCM tamper rejection as an integrity boundary but does not claim GHASH key/tag
recovery. DH uses validated parameters and a bounded 11-candidate subgroup.

MT19937 recovery requires exactly 624 consecutive untruncated 32-bit outputs,
with byte order, observation count, untemper, state alignment, and later-output
prediction recorded; 623 outputs are a closed negative, not a complete state.
LCG and xorshift checks replay later outputs. Time/PID seed recovery enumerates
only the declared finite window and closes on no hit. OS CSPRNG output is never
used as a prediction target.

Run the offline known-answer smoke from the repository root:

```bash
docker run --rm --network none --user agent \
  --entrypoint python3 -v "$PWD/tests:/tests:ro" -w /tmp \
  sapper-ctfy-web-test:latest /tests/crypto_smoke.py
```

`tests/crypto_smoke.py` writes only temporary self-authored parameters, raw tool
output, oracle logs, checkpoints, and independent verification evidence. Success
requires `PASS public-key`, `PASS modes-protocol`, and `PASS prng`, with no
`FAIL` or `SKIP`, and exit `0`. **限制:** these are small local known answers,
not exhaustive attacks or real-service budget validation. Boneh-Durfee,
Coppersmith, partial-key recovery, GHASH key recovery, unbounded discrete logs,
and other platforms are not exercised. The run establishes no
real Crypto challenge A/B results, official platform conformance, or solve rates.

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

## Local preliminary-round driver

`lib/ctfy_local.py` is a host-side companion, not the CTFY-0001 container entrypoint.
It uses only the Python standard library and raw REST JSON, so optional SDK model
changes cannot block an instance read. It lists challenges, reuses one matching
running instance or starts one and waits for readiness, builds the v3 task, runs the
image, submits any unverified answers left in `result.json`, then stops the instance.
Only the host driver calls lifecycle APIs; the container entrypoint still has no
start/stop logic. A local player token may nevertheless have broader permissions
than the hosted run's scoped token, so use a temporary, least-privilege token.

```bash
export CTFY_PLATFORM_URL=https://ctfy.tsinghua.edu.cn
export CTFY_TOKEN='<temporary player token with instance write access>'
export LLM_FLAVOR=anthropic
export LLM_API_KEY='<model key>'
export LLM_MODEL=glm-5.3
export LLM_BASE_URL=https://open.bigmodel.cn/api/anthropic

python3 lib/ctfy_local.py challenges --competition '<competition-id>'
python3 lib/ctfy_local.py instances --competition '<competition-id>'
python3 lib/ctfy_local.py run CTFARENA-000 --competition '<competition-id>' \
    --minutes 20 --max-turns 8 --ttl 3600
python3 lib/ctfy_local.py stop '<instance-id>'
```

The default work directory is `runs/<competition>/<challenge>/<instance>` (or `qa`),
which is ignored by Git. An explicit `--work-dir` is reusable only for the same
challenge, competition and instance, preventing an old answer ledger leaking into a
fresh instance. Startup failures, container errors and container timeouts trigger
cleanup of the owned resources. A container timeout removes only this run's named
container; no broad Docker cleanup is performed.

By default a non-shared instance is stopped even when it was reused. Use
`--keep-instance` to continue working against the same instance and work directory.
Shared instances are never stopped by this driver. `--prepare-only` downloads and
validates the task without calling the model; combine it with `--keep-instance` if
the target must remain running. Unsupported engagement networking is refused
explicitly rather than silently probing unreachable internal addresses.

For `is_qa_only=true`, no instance is started. Static attachments, including nested
paths, are downloaded to the work directory and passed as local QA paths; the local
runner adds no extra attachment-size budget beyond platform limits. Instance
attachments are still obtained through the current instance. TCP access metadata
and any supplied endpoint credentials are retained in the graph.

`result.json.answers` is handed off once through the correct instance or QA grader
**before** instance teardown. Outcomes, including transport or HTTP errors, are in
`handoff.json`; do not interpret `answers_handed_off` as proof of acceptance.
Accepted answers and current instance state remain authoritative for completion.
Host-side failures exit non-zero; an unfinished or infrastructure-blocked inner
solver is not equivalent to a solved challenge.

This driver is for local development and preliminary-round play. It does not replace
the official hosted runner, does not implement challenge authoring, AWD or VPN, and
should be used with a temporary, competition-scoped token. Registration is separate:
a token without `teams:write` cannot register Solo for a new competition.

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
