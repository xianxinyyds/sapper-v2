# CTFY-0001 v3 Solver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 sapper-v2 接入 CTFY-0001 v3 托管运行，完成单题、即时答案提交、gated questions、多轮恢复和结果文件的闭环。

**Architecture:** 新增 stdlib CTFY REST adapter，并通过 `sapper answer` 将平台 verdict 写入现有事实图。新的 Python 入口负责 dry-run、平台模型配置、单进程 `auto` 和最终 `result.json`；旧 TSecBench 入口和 commander 不参与 CTFY 运行。

**Tech Stack:** Python 3、标准库 `urllib`、现有 sapper Store/CLI、Claude Code CLI、OpenAI Codex CLI、bash、Docker、`unittest`。

**Spec:** `docs/superpowers/specs/2026-11-07-ctfy-v3-solver-design.md`

## Global Constraints

- 仅支持 `schema_version: 3`、`run_mode: instance`；其他 run mode 明确失败，不静默按 instance 处理。
- 当前 challenge/instance 只从 `$CTFY_WORK_DIR/task.json` 获取；不得新增列题、启动、关闭或其他跨题 API 调用。
- dry-run 必须在检查 token、模型 key 和启动 auto 之前结束。
- 平台 `LLM_*` 是唯一模型配置来源；清空 `SAPPER_KEY_POOL`，禁止个人 key fallback。
- `/ctfy/work` 中不得出现 `CTFY_TOKEN`、`LLM_API_KEY` 或复制后的凭证配置。
- 先记录答案再提交；以响应中的布尔 `correct` 判断正误，不以 HTTP 200 代替。
- 已 rejected、accepted 或 handed_off 的答案不进入最终 `answers`；只有未获得 verdict 的 `recorded` 候选答案由平台结束后兜底提交，并在结果写成功后标记 handed_off。
- 提交 POST 不自动重试；`Retry-After` 重试只用于幂等 GET/附件下载，避免重复消耗 wrong-attempt budget。
- 保留现有 48 个测试行为；使用 `unittest` 和 Python 标准库，不引入第三方 Python 包。
- 不修改旧 commander、多 worker、`lib/arena.py` 或 bench 算法。
- 未经用户明确要求，不执行 git commit 或 push。

## Review Focus

- **无凭证 dry-run：** task 只有 dry-run 字段时也必须快速写结果并退出 0；由 Task 4 的 `test_dry_run_precedes_config_validation` 验证。
- **错误答案重复消耗次数：** rejected 和已交接的 handed_off 答案不得被后续 rollup 再提交，submit POST 不自动重试；由 Task 1 的 `test_submission_is_not_retried` 和 Task 4 的 `test_finalize_marks_handed_off_and_does_not_roll_up_twice` 验证。
- **后续轮次新 question：** 不得覆盖旧图或重复 question facts；由 Task 6 的 `test_second_round_reuses_graph_and_adds_question` 验证。
- **恶意附件名与 secret 泄漏：** `../` 不得逃逸 attachments，token/key 不得落盘；由 Task 1 的附件测试和 Task 6 的 secret 扫描验证。
- **仅有 TCP endpoint：** 不得依赖 `CTFY_TARGET_URL` 或尝试当 HTTP 扫描；由 Task 6 的 `test_tcp_task_seeds_socket_service_without_target_url` 验证。
- **CLI 返回非 JSON 或空最后消息：** 不得被误认为成功模型响应；由 Task 3 的 Codex adapter 测试验证。

---

### Task 1: CTFY v3 Protocol Adapter

**Files:**
- Create: `lib/ctfy.py`
- Test: `tests/test_ctfy.py`

**Interfaces:**
- Consumes: CTFY-0001 v3 task/environment and public REST API.
- Produces:
  - `class CtfyError(Exception)` with `status: int | None` and `code: str | None`.
  - `load_task(work_dir: Path) -> dict`.
  - `require_instance_task(task: dict) -> None`.
  - `CtfyClient.from_env() -> CtfyClient`.
  - `CtfyClient.get_challenge(challenge_id: str) -> dict`.
  - `CtfyClient.submit_answer(instance_id: str, question_id: str, answer: str) -> dict`.
  - `CtfyClient.download_attachment(instance_id: str, name: str, destination: Path) -> None`.
  - `ensure_attachments(task: dict, client: CtfyClient, work_dir: Path) -> None`.
  - `write_result(work_dir: Path, *, answers: dict[str, str], solved: list[str], fully_solved: bool, stop_reason: str) -> None`.

- [ ] **Step 1: Write failing task validation and result tests**

创建 `tests/test_ctfy.py`，用临时目录构造最小 v3 task。`test_task_requires_v3_and_instance` 断言 schema 2、缺 challenge id、缺 target instance、非 instance 都抛出 `CtfyError`，合法 HTTP 与 TCP task 均通过。`test_write_result_is_atomic_and_versioned` 断言最终文件为对象、`schema_version == 3`、字段值准确且没有临时文件残留。

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy.py' -v`
Expected: FAIL，提示无法导入 `ctfy`。

- [ ] **Step 3: Implement task loading and atomic result writing**

`load_task()` 只读取 `work_dir/task.json`，要求 JSON object 和 `schema_version == 3`，保留未知字段。`require_instance_task()` 只校验本任务需要的字段，不把 `CTFY_TARGET_URL` 当必需项。`write_result()` 在 work dir 内创建临时文件，flush + fsync 后用 `os.replace()` 替换 `result.json`。

- [ ] **Step 4: Write failing REST contract tests**

用本地 `HTTPServer` 实现 `test_submission_contract_and_verdict`：断言请求为 `POST /api/v1/submissions`、认证为 `Authorization: Bearer tok-x`、body 精确为 `{"instance_id":"i-1","question_id":"q1","answer":"FLAG{x}"}`，并分别返回 `correct: true` 和 `false`。实现 `test_retry_after_and_error_envelope`，对幂等 `get_challenge()` mock `time.sleep`，断言 429 使用 `Retry-After`，最终错误保留 HTTP status 和平台 code，且异常文本不包含 token。`test_submission_is_not_retried` 让 submit 返回 429/502，断言只收到一次 POST。

- [ ] **Step 5: Implement the scoped stdlib client**

使用 `urllib.request`；base URL 只取 `CTFY_PLATFORM_URL`，token 只取 `CTFY_TOKEN`。路径参数用 `urllib.parse.quote(..., safe="")`。只对幂等 GET/附件下载的 429、502、503、504 做最多两次重试，尊重秒数形式的 `Retry-After`；submit POST 不重试。提交响应必须是 object 且含布尔 `correct`。不要实现 list/start/stop instances 方法。

- [ ] **Step 6: Write and implement attachment tests**

`test_downloads_only_null_path_attachment` 断言已有 `path` 不请求，`path: null` 写入 `attachments/name` 且原子替换。`test_attachment_name_cannot_escape_work_dir` 对 `../escape` 和绝对路径抛出 `CtfyError`，目标目录外文件不存在。下载使用独立二进制流式请求，不把附件当 JSON。

- [ ] **Step 7: Run adapter and legacy tests**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy.py' -v && python3 -m unittest discover -s tests -p 'test_sapper.py' -v`
Expected: 新测试及既有 48 个测试全部 PASS。

---

### Task 2: CTFY Answer Ledger and Question Sync

**Files:**
- Modify: `lib/sapper.py:48-52,582-599,1717-1834,4555-4576`
- Test: `tests/test_sapper.py`
- Test: `tests/test_ctfy.py`

**Interfaces:**
- Consumes: Task 1 的 `load_task()`、`require_instance_task()`、`CtfyClient`。
- Produces:
  - `sync_ctfy_facts(store: Store, task: dict, challenge: dict | None = None) -> None`。
  - `cmd_answer(args) -> int`，CLI 为 `sapper answer VALUE --question QUESTION_ID [--submit] [--title TITLE] [--creator CREATOR]`。
  - answer fact 的 `data` 含 `platform=ctfy`、`challenge_id`、`instance_id`、`question_id`、`value`、`status`、`result`。
  - `Store.update_flag_fact()` 额外接受 `accepted` 和 `handed_off` 状态。
  - `sync_ctfy_facts()` 将 challenge 的 `my_solved_question_ids/my_fully_solved` 写入 project 的附加 `ctfy` 元数据，供后续轮次和 rollup 使用。

- [ ] **Step 1: Write failing answer lifecycle tests**

在 `tests/test_sapper.py` 新增 `CtfyAnswerTests`。`test_answer_records_before_submit_and_applies_verdict` 用 fake client 记录调用顺序：读取 fact 时已存在且为 `recorded`，返回 `correct: true` 后同一 fact 为 `accepted`，响应保存于 `result`。再覆盖 `correct: false -> rejected` 和 transport error 后仍为 `recorded`。

- [ ] **Step 2: Write dedupe and scoped identity tests**

`test_answer_dedupe_key_includes_question` 断言相同 `(instance,question,value)` 拒绝重复，而相同 value 用于不同 question 可记录。断言 CLI 没有 challenge/instance 参数，身份来自 task；构造环境里的其他 challenge id 不得改变提交 body。`test_newly_unlocked_question_is_valid_against_old_task_snapshot` 先向图中同步 task 里没有的 q2，再用原 task 提交 q2，断言不能被 snapshot 校验拒绝。

- [ ] **Step 3: Implement `answer` without changing legacy `flag`**

新增动态 CTFY loader，将 `CtfyError` 桥接为 `FGError` 子类，沿用 Arena loader 的异常风格。`cmd_answer()` 先校验非空 value/question、当前 task，以及 question 位于 task 或已同步的 CTFY question facts，再以 `kind=flag` 记录；`--submit` 时调用 Task 1 的精确提交接口并写 verdict。传输失败保留答案并返回用户可见错误。不要改动 `cmd_flag()` 的旧行为。

- [ ] **Step 4: Write failing graph and gated-question tests**

`test_sync_ctfy_facts_is_idempotent_and_supports_tcp` 断言 challenge、每个 endpoint、附件和 question 都有 typed fact，第二次同步 fact 数不增加；TCP `host:port` 生成 `service`，无需 HTTP URL。`test_correct_answer_refreshes_gated_questions` 让 fake client 返回带新 question 的 challenge，断言 correct 后调用 `GET challenge`，新 question fact 可被 reason 看到。

- [ ] **Step 5: Implement idempotent graph synchronization**

`sync_ctfy_facts()` 使用 `data.platform=ctfy` 加稳定业务键识别已同步节点，不依赖随机 fact id。challenge 参数存在时以其 `questions` 为准，否则使用 task snapshot；只在遇到新业务键时创建 fact。同步 task 的 `scope/rules_of_engagement`，并将 challenge 的 solved ids/fully solved 写入 project 的 `ctfy` 附加元数据。`cmd_answer()` 在每次 correct 后刷新 challenge 并同步，不只限于当前已知的 gated 数量。

- [ ] **Step 6: Register CLI and run focused tests**

在 argparse 增加 `answer`，保留旧 `flag/arena` parser。扩展 `update_flag_fact()` 状态白名单以接受 `accepted`、`handed_off`，并保留既有状态测试。

Run: `python3 -m unittest discover -s tests -p 'test_sapper.py' -v && python3 -m unittest discover -s tests -p 'test_ctfy.py' -v`
Expected: 全部 PASS。

---

### Task 3: Platform Model Configuration and Codex Engine Adapter

**Files:**
- Create: `lib/ctfy_engine.py`
- Create: `lib/codex_engine.py`
- Test: `tests/test_ctfy.py`

**Interfaces:**
- Consumes: `LLM_FLAVOR/LLM_API_KEY/LLM_MODEL/LLM_BASE_URL` 与现有 `run_claude()` CLI 契约。
- Produces:
  - `configure_model(environ: MutableMapping[str, str] | None = None) -> str`，返回 `LLM_MODEL` 并设置 `SAPPER_BIN`。
  - `codex_engine.py` 接收现有 Claude 风格参数，从 stdin 读 prompt，成功时 stdout 仅输出 `{"result":"<last message>"}`。

- [ ] **Step 1: Write failing model configuration tests**

`test_anthropic_config_uses_platform_gateway_without_fallback` 预置个人 key 和 `SAPPER_KEY_POOL`，调用后断言 key pool 被清除，Anthropic SDK/CLI 变量来自 `LLM_*`，model 返回 `LLM_MODEL`。`test_openai_config_selects_codex_adapter` 断言选择同目录的 `codex_engine.py`，OpenAI 变量同样来自平台。`test_missing_key_or_unknown_flavor_fails` 覆盖空 key、空 model、未知 flavor。

- [ ] **Step 2: Implement provider-neutral configuration**

anthropic 设置 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、可用的 `ANTHROPIC_BASE_URL` 和模型槽位，并选择 `claude`。openai 设置 `OPENAI_API_KEY`、可用的 `OPENAI_BASE_URL`、`OPENAI_MODEL`，选择绝对路径 Codex adapter。不要读或写入个人 key，不生成含 key 的配置文件；`LLM_BASE_URL` 未设置时删除同 flavor 的旧 BASE_URL 环境值，确保使用 SDK default 而非遗留个人网关。

- [ ] **Step 3: Write failing Codex adapter tests**

在 PATH 中放 fake `codex`，记录 argv/stdin，并向 `--output-last-message` 指定文件写 `{"accepted":true,"data":{}}`。`test_codex_adapter_returns_claude_envelope` 断言使用 `codex exec`、显式 `-` 从 stdin 读 prompt、`--skip-git-repo-check`、现有 bypass flag、`-c model=<LLM_MODEL>`，最终 stdout 可解析为现有 envelope。`test_codex_adapter_rejects_empty_or_failed_result` 覆盖非零退出和空 last message。

- [ ] **Step 4: Implement the minimal Codex adapter**

解析现有 `run_claude()` 会传入的 `-p`、`--output-format json`、`--model`、`--dangerously-skip-permissions`、`--append-system-prompt`。将 append-system 文本与 stdin prompt 组合，使用临时目录保存 last message；调用 `codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -c model=<model> --output-last-message <path> -`。成功且 last message 非空时输出 JSON envelope；否则非零退出，stderr 不打印凭证。

- [ ] **Step 5: Run model and regression tests**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy.py' -v && python3 -m unittest discover -s tests -p 'test_sapper.py' -v`
Expected: 全部 PASS。

---

### Task 4: Single-Challenge Run Lifecycle

**Files:**
- Create: `lib/ctfy_run.py`
- Modify: `lib/sapper.py` (only `Store.reopen_project()` and idempotent reuse of an existing goal edge when active)
- Test: `tests/test_ctfy_run.py`

**Interfaces:**
- Consumes: Tasks 1–3 的 task/client/result、`sync_ctfy_facts()`、`configure_model()` 和现有 `sapper.main()`。
- Produces:
  - `collect_ledger(store: Store) -> tuple[dict[str, str], list[str], bool]`。
  - `prepare_graph(work_dir: Path, task: dict, challenge: dict | None = None) -> Store`。
  - `Store.reopen_project() -> None`，仅在 completed 且有新工作时恢复 active。
  - `finalize_result(work_dir: Path, store: Store, stop_reason: str) -> None`，写结果后将本次 answers 标为 handed_off。
  - `run(work_dir: Path) -> int`。
  - `main() -> int`，work dir 来自 `CTFY_WORK_DIR`，默认 `/ctfy/work`。

- [ ] **Step 1: Write failing dry-run and configuration tests**

`test_dry_run_precedes_config_validation` 只提供 `{"schema_version":3,"dry_run":true}`，清空 token/key，patch `sapper.main` 和 `configure_model`，断言二者均未调用、退出 0、结果精确包含 `solved=[]`、`fully_solved=false`、`stop_reason=dry_run`。`test_invalid_task_and_unsupported_mode_fail_cleanly` 断言可写结果时先写错误 stop reason，再返回非零。

- [ ] **Step 2: Write failing ledger rollup tests**

构造同一 question 的 recorded、rejected、accepted、handed_off facts 和普通 legacy flag。`test_collect_ledger_excludes_rejected_and_accepted` 断言最终 answers 仅包含未获 verdict 的 CTFY candidate，solved 合并 accepted facts 与 project `ctfy.solved_question_ids`，legacy flag 不混入；从 accepted 响应和 challenge 元数据得出 fully solved。相同 question 多个 recorded candidate 只保留最新一个。`test_finalize_marks_handed_off_and_does_not_roll_up_twice` 连续执行两次 finalize，断言第一次 result 含 candidate、fact 变为 handed_off，第二次 answers 为空。

- [ ] **Step 3: Implement graph preparation, handoff, and ledger rollup**

`prepare_graph()` 使用 `$CTFY_WORK_DIR/.sapper`：首次 `Store.init()`，origin 含当前 challenge id；已存在时校验 origin 不属于其他 challenge，并调用 `sync_ctfy_facts()`，不强制覆盖或清空。`collect_ledger()` 只处理 `data.platform=ctfy`，按 fact 创建时间/id 确定最新 candidate，读取 project `ctfy` 元数据，不推测分数。`finalize_result()` 先成功写 result，再将本次实际写入的 recorded facts 标为 handed_off；下一轮 collect 不再携带它们。

- [ ] **Step 4: Write failing normal-run orchestration tests**

patch `configure_model()`、`ensure_attachments()` 和 `sapper.main()`。`test_run_initializes_single_graph_and_invokes_auto` 断言每轮先刷新当前 challenge，auto 收到当前 store、`LLM_MODEL`、CTFY templates、`--skip-permissions`，不调用 bench，且 max steps 使用正整数 `task.max_turns`；`max_turns: null` 使用 `SAPPER_MAX_STEPS` 默认 8。auto 返回 0 且无答案时 run 返回 0 并写 `stop_reason=done`；auto 非零时仍写结果并返回非零。`test_graph_from_other_challenge_is_rejected` 和 `test_completed_graph_reopens_when_new_question_unlocked` 分别阻止跨题污染和 completed graph 空转；再次完成时已有 goal edge 幂等复用。

- [ ] **Step 5: Implement the run function with guaranteed result finalization**

顺序固定为 load task → dry-run → require instance → client/refresh challenge → prepare/resume graph → configure model → attachments → auto → finalize。若平台 fully solved 为 false 而 project 已 completed，调用受锁保护的 `Store.reopen_project()`；调整 `complete_project()` 仅在“active 且已有 goal edge”时幂等设置 completed，保持 completed 状态重复完成仍报错。模型或平台配置错误要能写 `config_error` 后非零返回；普通未解出不是异常。将 `--max-explore` 设为 `min(6,max_steps)`，首版不启用 verify，不伪造计数或 wall-clock deadline。所有异常文本仅含路径、状态码和平台错误，不打印环境值。

- [ ] **Step 6: Run lifecycle and all unit tests**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -v`
Expected: 全部 PASS。

---

### Task 5: CTFY-Specific Reason and Explore Prompts

**Files:**
- Create: `templates/ctfy/reason.md`
- Create: `templates/ctfy/explore.md`
- Create: `templates/ctfy/verify.md`
- Test: `tests/test_ctfy_run.py`

**Interfaces:**
- Consumes: 现有 `{graph_yaml}`、`{intent_description}`、`{time_budget}` 等模板变量。
- Produces: Task 4 传给 auto 的独立 CTFY 模板目录；不修改 legacy templates。

- [ ] **Step 1: Write failing prompt contract tests**

读取三个模板，断言 reason/explore 包含 `sapper answer`、当前 `question_id`、gated refresh 和只攻击 task endpoints 的约束；断言不包含 `arena start`、`arena challenges`、`POST /api/v1/instances`、hint 扣分、470 分钟或跨题调度。断言保留 graph/target output 为 untrusted data 的防护和原始 JSON 输出契约。

- [ ] **Step 2: Create CTFY templates from the existing structure**

只保留现有模板中与单题求解相关的图、recall、证据、时间盒和 JSON 规则。executor 获得答案时立即执行 `sapper answer "<value>" --question "<id>" --submit`；不得假定 flag 格式或仅有 `flag` question。reason 只有平台 fully solved 且无未解/gated question 时才返回 complete。verify 沿用现有可验证证据规则，并加入 CTFY answer 状态检查。

- [ ] **Step 3: Run prompt and full tests**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -v`
Expected: 全部 PASS。

---

### Task 6: Container Entrypoint, End-to-End Mock, and Operator Documentation

**Files:**
- Create: `docker/entrypoint-ctfy.sh`
- Modify: `docker/Dockerfile`
- Create: `docker/CTFY.md`
- Modify: `SKILL.md`
- Test: `tests/test_ctfy_run.py`

**Interfaces:**
- Consumes: Tasks 1–5 的全部公开接口。
- Produces: `ENTRYPOINT ["/usr/local/bin/sapper-entrypoint"]` 启动绝对路径 `/opt/sapper/lib/ctfy_run.py`，不再进入 `sapper bench`。

- [ ] **Step 1: Write a failing end-to-end local platform test**

启动本地 mock platform，提供正确 verdict 和新 challenge questions。patch `configure_model()` 返回测试 model，patch `sapper.main()` 模拟 executor 调用真实 `cmd_answer()` 后返回 0。`test_instance_run_submits_and_writes_result` 断言只收到当前 instance 的提交、图含 accepted answer、结果 solved/fully_solved 正确，且没有重复最终 answer。

- [ ] **Step 2: Add TCP, second-round, and secret-scan tests**

`test_tcp_task_seeds_socket_service_without_target_url` 清除 `CTFY_TARGET_URL`，断言 TCP endpoint 被写入图且 auto 仍执行。`test_second_round_reuses_graph_and_adds_question` 在同一 work dir 先运行一轮，再更新 task/README、refresh challenge 并删除 result 后运行第二轮，断言 project/fact 保留、新 question 只增加一次、result 重新生成，且上一轮 handed_off answer 不在第二轮 answers。两轮使用 `tok-super-secret` 和 `llm-super-secret`，递归扫描 work dir 文本文件，断言两个值均未落盘。

- [ ] **Step 3: Implement the absolute container entrypoint and Docker wiring**

`entrypoint-ctfy.sh` 仅设置非交互 CLI/遥测环境并 `exec python3 /opt/sapper/lib/ctfy_run.py`，不做旧 BENCHMARK 预检。Dockerfile 复制并 chmod 新 Python 入口和 Codex adapter，安装 `@openai/codex`，创建 agent 可写的 `/ctfy/work`，移除旧 `SAPPER_MODEL/SAPPER_TOTAL_MINUTES/SAPPER_MINUTES_PER_CHALLENGE/SAPPER_ROOT` 默认值，不复制 commander 的 DeepSeek Codex 配置。

- [ ] **Step 4: Document exact local verification**

`docker/CTFY.md` 写明支持的 run mode、平台环境、dry-run、本地 mock 测试、镜像构建命令、结果语义和不支持的 AWD/qa。`SKILL.md` 增加 `answer` 与 CTFY 入口，明确旧 `arena/bench` 不属于 CTFY 路径；不记录真实 token 或赛事凭证。

- [ ] **Step 5: Run final local verification**

Run:
```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash -n docker/entrypoint-ctfy.sh
python3 -m py_compile lib/ctfy.py lib/ctfy_engine.py lib/codex_engine.py lib/ctfy_run.py
git diff --check
```
Expected: 全部测试 PASS，shell/Python 语法检查通过，无 whitespace error。

- [ ] **Step 6: Report platform-only verification honestly**

检查 `docker --version` 和 `ctfy --help` 是否可用；可用则运行镜像 build 与 `ctfy agent check`/dry-run，不可用则在交付中明确标为“未运行”，不得以本地 mock 代替官方 conformance 成功的结论。
