# VPS 测试交接：优先 Cut the Clock

日期：2026-11-19

## 当前状态与目标

- 本轮目标是验证合作者扩充后的 CTFY Agent 在 VPS 上能独立完成真实题目。
- 当前 Reverse 题 `Hint Me Maybe` 尚未解出，已停止继续耗时。最后结果为 `incomplete`、`fully_solved=false`。
- 下一对话应**先运行 `Cut the Clock`，确认“拉题 → 附件 → Agent → 判分”完整链路，再考虑其他题**。

## VPS 与项目位置

- VPS：`root@101.200.207.23:22`，使用用户提供的密码，勿写入文档或提交到仓库。
- 项目目录：`/root/workspace/sapper-c870444`
- 合作者提交：`c870444 feat: expand CTFY Pwn Reverse and Crypto knowledge bases`
- 新增 KB：`kb065`–`kb073`，已确认镜像中存在 `kb073.json`。
- 当前镜像：`sapper-ctfy-web-test:latest`，容器内包含最新 `lib/templates/kb`。
- 本地回归测试：**264 tests passed**。VPS 在兼容修复前：**261 tests passed**。
- 不要再从头构建镜像；已上传过约 1.8 GB 的更新镜像包。

## 当前平台信息

- 平台：`https://ctfy.tsinghua.edu.cn`
- 当前比赛：`TPCTF 2026 · Agent Track · VPS Access`
- 比赛 ID：`1de3339befe549b2b740cdcf92994451`
- 新参赛令牌已由用户提供，使用环境变量 `CTFY_TOKEN`，不要把真实值写进文档。
- `SAMPLE` 赛道的旧令牌不能启动实例；当前新令牌可用于本比赛。

## 优先运行的下一题

### `Cut the Clock`

- Challenge ID：`AUTHORED-CDB35578`
- Instance ID：`5168f038-3caa-46af-892f-4dc0ab4b6614`
- 拓扑：`simple`
- `services=[]`，**不需要 VPN**，这是目前最适合验证端到端流程的题。
- 附件：
  - `README.txt`
  - `bomb.circ`
  - `envelope.py`
  - `instance.json`
  - `operations.py`
  - `payload.json`
  - `release.py`
- 原实例 `5168f038-3caa-46af-892f-4dc0ab4b6614` 已过期并被平台删除。**新对话必须先创建新的 `AUTHORED-CDB35578` 实例，再使用新 instance ID 和附件，不要复用该旧 ID。**
- 创建后用新的实例信息生成 task 并启动 Agent。
- 题目附件全部可通过实例附件接口下载，不需要 `get_challenge` 的 standalone catalog。

## 当前已创建的 Reverse 题（暂缓）

- `AUTHORED-37DA5EFD` / `Hint Me Maybe`
- Instance ID：`7bf02932-ba89-4ec5-ad6d-b714eed96d53`
- `network_topology=engagement`，`ctfy_local.build_task()` 会直接拒绝，需要手工生成 task。
- VPN 配置：`/root/workspace/reverse-vpn.conf`。
- VPN 接口：`reverse-vpn`，DNS `172.16.63.3`。
- 目标：`scz54yohha7olx66t.internal:15001`，已验证 TCP 可达。
- 已续期一次，当前截止时间请以 `get_instance` 返回值为准。
- 最后 solver 结果：`incomplete`，没有 flag，先不要继续此题。

## 已完成的兼容修复（未提交）

本地 worktree `/home/xianxin/sapper-v2/.claude/worktrees/ctfy-v3-solver` 中有未提交改动，VPS 对应路径也已覆盖：

1. `lib/ctfy_run.py`
   - 对 `go_unsupported_feature` 增加实例详情 fallback。
   - 保留 `expires_at`，初始化 `prepare_graph(..., progress=progress)`。
   - 正常流程仍必须执行启动 `get_instance`，不要误删。
2. `lib/ctfy_engine.py`
   - `SAPPER_BIN` 改为 `env.get("SAPPER_BIN") or "claude"`，允许自定义 wrapper。
3. `lib/codex_engine.py`
   - OpenAI 路线需要可执行权限；当前 VPS 文件已 `chmod +x`。
4. 新增回归测试后共 264 个测试通过。

如果新对话不打算提交这些改动，直接复用 VPS 上现有文件即可。

## 模型与引擎

- 网关：
  - Anthropic：`https://deepseek.llm-next.ctfy.io`
  - OpenAI：`https://deepseek.llm-next.ctfy.io/v1`
- 会话凭据使用用户提供的 `pk_...`，不要写入文档、Git、日志或镜像。
- 原始 API 验证结果：
  - `deepseek-v4-pro` 在 Anthropic 和 OpenAI 兼容路径均返回 `OK`。
  - `deepseek-flash` 的 CLI 工具调用不稳定；`deepseek-flash[1M]` 在 CLI 中也会被报 `unrecognized_model`。
- 推荐当前测试使用 `deepseek-v4-pro`。
- `/root/workspace/sapper-c870444/runs/.../openai_http_engine.py` 是自定义 HTTP 引擎：
  - 直接请求 `/v1/chat/completions`，模型 `deepseek-v4-pro`。
  - 声明 `shell` function tool。
  - 执行 `<tool_calls>` 中的命令，回传 `role=tool` 和 `tool_call_id`，形成工具循环。
  - 工具命令最多 90 秒、最多 16 轮，工作目录 `/ctfy/work`。
  - 目前该引擎本身已出现 SSL handshake timeout 和偶发空响应，**不要把 Reverse 的失败归因于题目不可解**。
- `claude` / `codex` CLI 在本网关下均有兼容问题，优先使用该 HTTP 引擎而不是继续调试 CLI。

## 运行入口与常见坑

- 使用 `/usr/local/bin/sapper-entrypoint`，不要直接执行 `ctfy_run.py`（有权限问题）。
- `challenge.id` 必须是 `AUTHORED-*`，不能填 instance UUID。
- 容器写工作目录前执行：
  ```bash
  chown -R 1000:1000 /root/workspace/sapper-c870444/runs
  ```
- `--store` 是全局参数，应放在 `auto` 前。
- `ctfy_local.build_task()` 对 `engagement` 拒绝；`simple` 可以走正常 runner。
- `SAPPER_BIN` 只有 `anthropic` 分支会保留自定义值；OpenAI 分支固定走 `codex_engine.py`。
- `configure_model()` 会把模型槽位设置为 `LLM_MODEL`，不要同时使用不兼容的 CLI 别名。

## 建议的新对话第一步

1. 通过 SSH 进入 VPS，进入 `/root/workspace/sapper-c870444`。
2. 检查 `AUTHORED-CDB35578` 实例和附件，不启动新的 Reverse 实例。
3. 用 `ctfy_local.py` 或简单 task 初始化 `Cut the Clock`，确保 `task.challenge.id=AUTHORED-CDB35578`。
4. 使用 `deepseek-v4-pro` + `openai_http_engine.py` 启动标准入口，观察 `.sapper/facts`、`result.json` 和模型 JSONL。
5. 先验证是否得到平台 `correct=true`，再决定是否扩到 Pwn/Crypto/Web。
6. 不要手工写 exploit payload；记录 Agent 自主产生的证据。

## 证据与日志位置

- 当前 Reverse 工作目录：
  `/root/workspace/sapper-c870444/runs/1de3339befe549b2b740cdcf92994451/AUTHORED-37DA5EFD/7bf02932-ba89-4ec5-ad6d-b714eed96d53`
- `agent-http.log`：最后一次 HTTP 引擎运行日志，最终 `result.json` 为 `incomplete`。
- `agent-stdout.log`、`agent-stdout-continue.log`、`agent-openai.log`：早期 CLI 超时与兼容问题记录。
- VPS 单元测试日志：`/root/workspace/sapper-c870444/unit-tests.log`
- VPS Docker 构建日志：`/root/workspace/sapper-c870444/docker-build.log`

## 需要避免

- 不要把任何 token、密码、API key 或 VPN 私钥复制到公开文档、Git、日志或最终回复。
- 不要重复启动多个同类实例，平台有并发上限。
- 不要把 `simple` 拓扑误接到不必要的 VPN。
- 不要把 CLI 的模型兼容问题当成“Agent 无法自动解题”的结论。

## 2026-11-19 续作结果：Cut the Clock 已由 Agent 自主解出

- 新实例：`3e1e23ed-490d-4fa1-96c2-ebd50f21631f`，创建时 `ready`、`simple`、`services=[]`，7 个附件均成功下载。
- 工作目录：`/root/workspace/sapper-c870444/runs/1de3339befe549b2b740cdcf92994451/AUTHORED-CDB35578/3e1e23ed-490d-4fa1-96c2-ebd50f21631f`。
- 最终 `result.json`：`solved=["flag"]`、`fully_solved=true`、`stop_reason="done"`。
- 已重新调用平台 `get_instance` 独立核验：`questions[flag].answered_correctly=true`，`grace_kind="solved"`。这是平台判分证据，不只是 Agent 自称完成。
- 接受记录为 `.sapper/facts/f010.json` 的 `status=accepted`；`answers={}` 不代表失败，已接受问题以 `solved` 和平台状态为准。
- 整个解题过程没有人工读取题面后给 Agent 编写 payload；Agent 通过 HTTP 引擎自主调用 shell、分析附件并提交。

### 本次排障与可复用运行方式

1. 首次运行出现 `go_unsupported_feature`，根因是镜像内 `/opt/sapper/lib/ctfy_run.py` 仍旧，而宿主 `lib/ctfy_run.py` 已有 fallback。不要重建镜像；运行时增加只读挂载：
   `--mount type=bind,src=/root/workspace/sapper-c870444/lib,dst=/opt/sapper/lib,readonly`。
2. 第二次运行模型网关 DNS 失败，根因是新 Docker 容器不继承宿主 `/etc/hosts` 的 `deepseek.llm-next.ctfy.io` 静态记录。运行时增加两条 `--add-host`，分别指向宿主现有的 `104.21.86.233` 和 `172.67.137.154`。改后模型能连续调用工具。
3. 使用 `deepseek-v4-pro`、`SAPPER_BIN=/ctfy/work/openai_http_engine.py`、`LLM_FLAVOR=anthropic`，并挂载上述宿主 `lib`。首轮自定义引擎达到 16 轮工具上限后重试，第二次完成。
4. 失败证据保留为 `result.catalog-error.json`、`result.model-dns-error.json` 及对应日志；最终日志为 `agent-http.log`。

### VPS 凭据持久化（不含真实值）

- root 专属环境文件：`/root/.config/ctfy/env`，权限 `600`，目录权限 `700`。
- 已保存 `CTFY_TOKEN`、`CTFY_PLATFORM_URL`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`，真实值不得写入仓库或日志。
- `/root/.bashrc` 已增加加载该文件的片段；交互式 shell 已验证 6 个变量均能加载。
- 非交互脚本仍应显式执行：`set -a; . /root/.config/ctfy/env; set +a`。
- 后续不再需要每次从旧容器 `objective_lalande` 读取模型会话凭据。

### 下一目标：一句“打比赛”自动完成整场流程

- 用户明确要求最终不需要人工指定比赛 ID、题目 ID或读取题面喂给 AI，而是只发“打比赛”，自动发现比赛与题目、准备实例和附件、交互解题、提交并继续。
- 静态代码检查确认：现有 CTFY 仅有单题 runner 和手工 `ctfy_local.py run`；没有比赛发现、campaign loop 或“打比赛”触发。
- 已向用户提出两种方案，当前尚未收到选择，不得视为已批准：
  1. 推荐确定性 host-side `play` 调度器，复用单题 Agent，负责发现、分页、去重、实例上限、结果和断点状态。
  2. 模型总指挥选择题目，再调用单题 Worker，灵活但并发与恢复更复杂。
- 设计实施前应确认方案；不要把本次手工指定 Cut the Clock 的链路验证当成最终自动化入口。

## 方案 2 已选择：CTFY AI 指挥官二开

- 用户已选择“模型总指挥 + 单题 Worker”，并要求先检测现有调度能力。
- 检测确认项目已有 `sapper command`（LLM 决定 work/hire/fire/stop）和 `sapper fleet`（tmux Worker 生命周期），相关测试 7 passed；`docker/commander` 另有 watchdog，但绑定 Arena。
- CTFY 尚缺比赛发现、CTFY 看板、单题 Worker 适配、campaign 锁与恢复，不能直接把 `sapper command` 指向现有嵌套 runs。
- 用户要求 AI 自主决定策略；人只给“默认 30 分钟无实质进展建议换题”和“参考解题数、难度、当前进展”等软建议，不写死优先级公式。
- 设计文档：`docs/superpowers/specs/2026-11-19-ctfy-ai-commander-design.md`。独立构建审查提出的 deadline、退出清理和容量缺失问题已补入设计，用户已于 2026-11-19 明确回复“批准”。
- 实施计划：`docs/superpowers/plans/2026-11-19-ctfy-ai-commander.md`，共 9 个任务，含 TDD、VPS 实测和交接要求；尚未开始产品代码实现。
- 二开前基线：`python3 -m pytest -q tests` → 264 passed、1029 subtests passed。直接运行不带路径的 `pytest` 会错误收集 `runs/` 下题目附件自带的 Brotli 测试并因缺少原生 `_brotli` 失败；测试命令必须限定 `tests`，不要误删题目附件或把该收集问题当项目回归。
