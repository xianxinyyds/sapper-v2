# sapper-v2 — The Autonomous Pentest Legion

<p align="center"><img src="docs/assets/banner.jpg" alt="sapper-v2 — the Nüwa Test: the first time an AI ape looked up at the stars (80.18/100 champion)" width="860"></p>

> **Champion** of the 3rd Tencent Cloud Intelligent Attack-Defense Challenge online qualifier
> (634 teams · 80.18/100 · 8 hours fully unattended) · **First Prize**, GeekCon 2026 AI Challenge
>
> Same 85 flags as the runner-up, 25 seconds faster — on **1B tokens** where they burned 4.5B.
>
> **This was the first time an AI ape looked up at the stars.**

📖 **[Full design article (WeChat, in Chinese)](https://mp.weixin.qq.com/s/nibHYWmDTf6TkNhPhtvFdw)** — the Nüwa Test, the three-capability foundation, 18 iterations, emergence, and the taggant proposal.

**English** | [中文](README.zh-CN.md)

**⚠️ AUTHORIZED SECURITY TESTING ONLY — see [DISCLAIMER.md](DISCLAIMER.md).**

## CTFY 2026：队友二开与发布指南

> 本节适用于 **`worktree-ctfy-v3-solver`** 分支，目标是首届 CCF 网络安全智能挑战赛暨京华杯 TPCTF 2026 的 **CTFY-0001 v3 解题 Agent**。下方英文正文是上游 sapper-v2 的完整介绍。
>
> 当前交付的是**单题、单进程、`run_mode=instance` / `run_mode=qa`** 基线，命题范围覆盖 **Crypto、Reverse、Misc、Web、Pwn 与 AI 安全**。instance 只处理平台已经启动的当前实例；QA 使用题目描述、问题和本地附件，答案通过单次 `/api/v1/qa/submissions` 判分，transport 失败才降级为 `result.json.answers` 移交，禁止 instance API。两种模式都不创建实例、不跨题读取或提交；`defence`、`awd`、`engagement` 尚未支持，旧 `sapper arena` / `sapper bench` 不在 CTFY 运行路径中。

### 1. 队友快速上手

```bash
git clone -b worktree-ctfy-v3-solver https://github.com/xianxinyyds/sapper-v2.git
cd sapper-v2

# 纯 Python 标准库测试，不需要先安装项目依赖
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

建议从 `worktree-ctfy-v3-solver` 新建自己的功能分支，小步提交并发起 PR。不要直接在 `master` 上开发 CTFY 功能，也不要把真实 token、模型密钥、registry 凭证或 `/ctfy/work` 中的运行产物提交到仓库。

### 2. 代码地图：应该改哪里

| 文件 | 职责 | 常见修改场景 |
|---|---|---|
| `lib/ctfy.py` | CTFY HTTP client、task 校验、附件与 `result.json` | 平台协议、GET 重试、结果格式 |
| `lib/ctfy_run.py` | 单题生命周期入口 | dry-run、图恢复、auto 调度、退出语义 |
| `lib/ctfy_engine.py` | 平台模型变量到 CLI 的映射 | Claude Code / 新执行器接入 |
| `lib/codex_engine.py` | Codex 非交互适配器 | Codex 参数、输出和错误处理 |
| `lib/sapper.py` | 事实图、auto、`sapper answer` | 答案账本、图同步、求解内核 |
| `templates/ctfy/` | planner / executor / verifier 提示 | 类别路由、方法论、答案提交策略 |
| `kb/entries/` | 离线技术知识库 | Crypto / Reverse / Misc / Web / Pwn / AI 安全条目 |
| `docker/Dockerfile` | Kali 工具链与 CLI 镜像 | 新工具、执行器、构建修复 |
| `docker/entrypoint-ctfy.sh` | 容器绝对入口 | 一般不需要修改 |
| `tests/test_ctfy*.py`、`tests/test_sapper.py` | 单元和 loopback 集成测试 | 每项行为变化的回归证据 |

答案必须通过当前题的 `question_id` 提交：

```bash
sapper answer "FLAG{example}" --question "flag" --submit
```

答案先落盘；instance 与 QA 的 `--submit` 分别只发送一次 `/api/v1/submissions` 或 `/api/v1/qa/submissions`，不自动重试，避免重复消耗错误尝试次数。仅 transport 失败才保留 recorded 供 result handoff，HTTP/协议失败使用 `submission_failed`，不静默移交。`task.max_turns` / `CTFY_EVAL_MAX_TURNS` 控制 turn 预算，`SAPPER_MAX_STEPS` 是更低安全上限；`SAPPER_ROUND_MINUTES` 默认 165 分钟，`CTFY_DISPATCH_TIMEOUT` 默认 300 秒，auto 另留 300 秒 finalization reserve。每次 reason/explore 后进行原子 checkpoint，最终 finalize 才改变 handed_off。

引擎按角色限制重试，并通过独立 process group 回收子进程；主动 daemonize 的进程不保证回收，执行器不得依赖 nohup、后台任务或长轮询。stdout 只用于观察，**计分结果以 `/ctfy/work/result.json` 为准**。`done` 只用于实际完成；未解出、模型失败、QA handoff 和 `blocked_by_infra` 都有不同停止原因。详细协议、预算和停止语义见 [docker/CTFY.md](docker/CTFY.md) 与 [docker/ENV.md](docker/ENV.md)，协议事实以官方 [CTFY-0001](https://ctfy.tsinghua.edu.cn/docs/standards/CTFY-0001) 为准。

### 3. 开发时使用的 Skills 与工作流

这些是**开发流程工具**，不是容器运行依赖；仓库根目录的 `SKILL.md` 则是 sapper 自身的运行时说明，两者不要混淆。

| Skill | 在何时使用 |
|---|---|
| `superpowers:brainstorming` | 新能力先确认目标、边界和非目标，避免把 CTFY-0002 出题流水线混入解题 Agent |
| `superpowers:writing-plans` | 多文件功能先写实施计划和可验证步骤 |
| `superpowers:using-git-worktrees` | 队友并行开发时隔离工作区，避免互相覆盖 |
| `superpowers:executing-plans` | 按已确认计划逐项实施并记录验证结果 |
| `superpowers:test-driven-development` | **所有功能与 bugfix：先写失败测试并观察 RED，再最小实现到 GREEN** |
| `superpowers:systematic-debugging` | 先读错误、复现并追踪数据流，不凭猜测改提示词或参数 |
| `superpowers:requesting-code-review` | 合并前检查协议、凭证、答案状态和并发边界 |
| `superpowers:verification-before-completion` | 提交、发布前重新运行完整验证，不以“应该可以”作为结论 |

库、CLI 和 SDK 的当前用法使用 Context7 查询，不凭训练记忆猜参数：

```bash
npx ctx7@latest library "OpenAI Codex" "<要查的具体问题>"
npx ctx7@latest docs /openai/codex "<要查的具体问题>"
```

推荐协作闭环：**明确验收条件 → 写失败测试 → 最小实现 → 全套测试 → 构建镜像 → 官方 check → 新 tag → SAMPLE-001 实测**。仅文档修改不必机械增加测试，但仍需检查 diff 和命令示例。

### 4. 评测模型选择：GLM 必须走 Claude Code

> **重要：使用 GLM 评测时，选择“智谱 GLM（Anthropic 兼容）”/ 供 Claude Code 使用的 GLM 连接，确保平台注入 `LLM_FLAVOR=anthropic`。不要选择普通“智谱 GLM（OpenAI 兼容）”后走 Codex。**

当前原因不是模型能力，而是 wire protocol：

- Codex 0.167.0 只支持 **Responses API**（`/v1/responses`），已移除 `wire_api="chat"`。
- 平台内置智谱 GLM 的 OpenAI 路由是 **Chat Completions**（`/v4/chat/completions`）。
- CTFY 网关不在两种协议之间转换，会在实际上游转发前返回 HTTP 400，表现为“模型调用 0、费用 0、连续 400”。

因此 GLM 的正确链路是：

```text
LLM_FLAVOR=anthropic → Claude Code → CTFY /v1/messages → 智谱 Anthropic 兼容端点
```

评测页面检查项：

1. 模型连接选择 **智谱 GLM（Anthropic 兼容）**，先在连接页面测试模型和密钥。
2. Agent 方言若可选择“自动/未声明”，保持自动；若必须单选，则选择 `anthropic`。
3. 不要只手动覆盖 `LLM_FLAVOR`；模型连接、网关地址和密钥必须属于同一套接口。
4. OpenAI flavor 仅用于已确认支持 Responses API 的提供商；不要尝试恢复 Codex 已删除的 chat 配置。

常见现象速查：

| 现象 | 优先检查 |
|---|---|
| 连续 400、转发 0、GLM + Codex | Responses / Chat 协议不匹配 |
| 401 / 403 | 密钥、权限、过期或吊销 |
| TLS handshake timeout | 平台网络；当前幂等 GET 已有有界重试 |
| `done` 但模型调用 0 | 检查图或平台是否确实完成；普通空跑应为 `incomplete`，模型/执行器故障应为 `model_error` / `explore_failed` / `auto_failed`，不能只信退出码 |

### 5. 本地验证与官方检查

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash -n docker/entrypoint-ctfy.sh
python3 -m py_compile \
  lib/ctfy.py lib/ctfy_engine.py lib/codex_engine.py lib/ctfy_run.py
git diff --check

docker build -f docker/Dockerfile -t sapper-ctfy:local-test .

python3 -m venv /tmp/ctfy-agent-check
/tmp/ctfy-agent-check/bin/pip install 'ctfy[client,server,node]'
/tmp/ctfy-agent-check/bin/ctfy agent check sapper-ctfy:local-test
```

`ctfy[client,server,node]` 中的额外依赖是本地 probe 实际需要的。`no findings` 只证明镜像协议检查通过，不证明真实模型配置正确或有特定解题率；模型相关修改还必须做一次真实 SAMPLE-001 冒烟评测，确认网关转发数大于 0。

### 6. 推送 Docker 镜像到 CTFY Registry

以下示例使用占位符。把命名空间替换为自己团队在 `registry.ctfy.io` 的 UUID，把 `v3` 替换为本次的新版本号；**不要一直复用旧 tag**。

```bash
export CTFY_NAMESPACE="<团队 registry 命名空间 UUID>"
export CTFY_TAG="v3"
export CTFY_IMAGE="registry.ctfy.io/${CTFY_NAMESPACE}/sapper-ctfy"

# 交互式输入 registry token，不把 token 写进命令历史
docker login registry.ctfy.io --username "<平台用户名>"

docker build -f docker/Dockerfile -t sapper-ctfy:local-test .
docker tag sapper-ctfy:local-test "${CTFY_IMAGE}:${CTFY_TAG}"
docker push "${CTFY_IMAGE}:${CTFY_TAG}"

# 可选：同时更新 latest；正式评测仍建议明确选择不可变版本 tag
docker tag sapper-ctfy:local-test "${CTFY_IMAGE}:latest"
docker push "${CTFY_IMAGE}:latest"

# 验证远端 manifest，并保存 push 输出的 digest
docker manifest inspect "${CTFY_IMAGE}:${CTFY_TAG}"
/tmp/ctfy-agent-check/bin/ctfy agent check "${CTFY_IMAGE}:${CTFY_TAG}"

docker logout registry.ctfy.io
```

在评测页面填写：

```text
registry.ctfy.io/<团队命名空间UUID>/sapper-ctfy:<新版本tag>
```

如果官方 check 读取远端 manifest 时提示 401 并回退本地镜像，应在登录状态下用 `docker manifest inspect` 独立确认远端存在及平台架构；不要把该警告写成“远端 manifest 已验证”。凭证不得写入 Dockerfile、镜像层、Git、日志或 `/ctfy/work`。

### 7. 后续提升路线（按优先级）

**P0 — 先让失败可诊断、结果可信**

- 持续审计 `model_error` / `explore_failed` / `auto_failed` 与 `blocked_by_infra` 的真实评测分布，避免把 `exit 0` 或普通 `incomplete` 误读为完成。
- 保留 Codex / Claude Code 错误输出的关键尾部；当前截断可能丢掉网关最后的 400 响应正文。
- 固定 SAMPLE-001 冒烟门槛：至少产生有效模型调用，再检查答案、结果文件、费用和停止原因。
- 修复 completed graph reopen 后 `validate_graph()` 暂时报告 active-with-goal 的一致性问题，并补回归测试。

**P1 — 再提升真实解题率**

- 基于真实 baseline 增加简短 Web / Pwn / AI 类别路由，把任务引导到现有离线 KB；不要只添加“你是 CTF 高手”之类空泛提示。
- 记录每类题的解出率、token、耗时和失败类型，用数据决定提示词、工具和步数预算的调整。
- 若必须支持普通 OpenAI Chat 提供商，评估支持 Chat Completions 的执行器（例如 Pi）；不要依赖 Codex 已删除的 chat wire API。
- 增加平台 GET、附件下载、模型 400/401/429、进程超时等故障注入测试。

**P2 — 最后扩大吞吐与协议范围**

- 在单题基线稳定后再引入 commander / tmux 多 Agent 并行，保持题目隔离、答案去重和事实图原子写。
- 仅在实际赛事需要时实现其他 run mode；不要提前把 CTFY-0002 出题、上传或审核流水线并入当前解题入口。

---

## What this is (and isn't)

**sapper is a multi-agent orchestration harness, not a single agent.** It does
not bind to any specific AI model or CLI — you can freely swap the agent and
model behind every role:

| What you can swap | How | Examples |
|---|---|---|
| **Agent CLI** | `nuwa spawn --agent <cmd>` or env var | Claude Code / Codex / Code Buddy / DeepSeek / any interactive CLI |
| **Model** | `--model` flag or `SAPPER_MODEL` env | GLM-5.3 / DeepSeek V4.1 Flash / Claude Sonnet / any model |
| **Role composition** | freely defined in the doctrine | commander on one model, soldiers on another, scouts on a third |

sapper's value is not in any single agent, but in the **mechanism for
orchestrating multiple agents to work as a coordinated force**: scheduling,
division of labor, communication, shared memory, self-healing. Swap out every
agent and model — the mechanism still works.

**This project is designed for competition scenarios** (Tencent Intelligent
Attack-Defense Challenge CTF). If you want to use this mechanism in your own
scenario (office, research, engineering, any long-horizon cooperative work):
- **Start from [nvwabase · the Nüwa Foundation](https://github.com/N1nEmAn/nvwabase)** — the general-purpose base with all three capabilities, no pentest doctrine, ready for you to iterate your own division of labor
- **Or fork this project** — but the doctrine is NOT the only thing to change; the environment wiring must change too:
  - `docker/commander/SKILL.md` (the 426-line doctrine) → your scenario's strategy
  - `BENCHMARK_BASE_URL` and related platform env vars → the competition engine hard-verifies the arena API at boot (that's why configuring only a model key without a platform address yields `BENCHMARK_BASE_URL is not set`); running off the competition platform means providing or stubbing that platform interface yourself
  - model gateway & key env vars (`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `SAPPER_MODEL` / `SAPPER_KEY_POOL` ...) → your own model services
  - watchdog budget/deadline and report templates → adjust to your scenario
- With AI, everything is flexible — even the doctrine can be rewritten with AI's help

---

sapper is not a smarter single agent. It is a legion of AIs that divide labor,
talk to each other, and pass down what they learn: one AI commander reads the
board, decides, and dispatches; generals and soldiers attack in parallel; a
zero-strategy watchdog keeps everyone alive. All knowledge settles into a
layered fact graph — individuals die, the graph survives.

---

## 1. The Nüwa Test

Making one model ever smarter is like making one monkey ever smarter before
humans existed — however clever, it only becomes a super monkey. What turns a
 troop of monkeys into humanity is division of labor, cooperation, and
civilization.

**The Nüwa Test is the Turing test of the AGI era.** Today's large models
already pass the classic Turing test. The Nüwa Test asks the next question:

> Give an AI a single axiom — **"Survival is civilization's first need;
> individuals may perish, but civilization must propagate."**
> If, on top of this axiom, it develops division of labor and cooperation and
> forms a **new force independent of humanity** — a civilization that can
> survive and propagate on its own — the test is passed, just as passing the
> Turing test once marked the birth of an artificial intelligence.

It has two stages. In the **primary stage** the AI is not yet smart enough, so
humans provide some infrastructure (the three capabilities below). In the
**advanced stage** almost no human involvement is needed. There is also a
**second axiom**: obey human ethics and law — anything that propagates by
stealing tokens and breaking into machines is not genuine intelligence.

sapper is the Nüwa foundation's first real campaign, in the penetration-testing
arena, at its primary stage.

## 2. The Foundation: Three Capabilities

<p align="center"><img src="docs/assets/foundation-three-capabilities.jpg" alt="Three capabilities: Create / Language / History (fact-intent graph)" width="720"></p>

| Capability | Implementation | Civilization's counterpart |
|---|---|---|
| **Create** | Opens new AIs via tmux (types `claude`, presses Enter — like a human would) | Birth |
| **Language** | Types into each other's input boxes, presses Enter | Speech |
| **History** | Layered fact-intent graph ([nvwabase · the Nüwa Foundation](https://github.com/N1nEmAn/nvwabase)) | The stele |

History has three layers: **title / summary / evidence document** — read the
title for the gist, drill down when needed, like using a library. This fixes
the two failure modes of a single-string fact graph: one bad fact poisoning
everything downstream, and needless context burn.

On top of the three sits a fourth ingredient: **human wisdom** — finding the
problems that matter and steering the iteration. At the primary stage, taste
still belongs to humans.

**Why not give it more rules?** Because today's models are already smarter
than us. Piling rules on an AI is a leash; acknowledging its intelligence and
giving it the foundation of a civilization is how it reaches its potential.

### 🏗️ Take the Foundation: nvwabase

**[nvwabase · the Nüwa Foundation](https://github.com/N1nEmAn/nvwabase)**
now implements all three capabilities (Create `nuwa spawn` / Language
`nuwa say·listen` / History `nuwa fact·intent·recall`) as an independent
open-source project — **free for anyone, any scenario, any agent**. Not
limited to pentest, not limited to sapper, not limited to Claude Code.
Offices, research, engineering, any long-horizon multi-agent work — iterate
your own civilization on it.

```bash
git clone https://github.com/N1nEmAn/nvwabase
cd nvwabase && bash install.sh          # pure Python stdlib, zero deps
nvwabase setup --goal "your long-horizon goal" --agents
nvwabase status                          # board & ready-to-work frontier
```

Three core commands turn any agent into a member of a civilization:
- **`nvwabase fact "confirmed observation"`** — external memory: AI sessions
  die, the graph survives, successors read the graph and take over
- **`nvwabase intent --from f001 "what to do"`** — units of work: new intents
  proposed from existing facts
- **`nvwabase dispatch reason`** — commander proposes / executor works /
  verifier checks; every conclusion is written back to the graph

Any agent that can run shell commands (Claude Code / Codex / Gemini /
custom scripts) can read and write the same graph via the `nvwabase` CLI —
**the foundation doesn't pick agents; agents don't pick scenarios.** See the
[nvwabase README](https://github.com/N1nEmAn/nvwabase).

## 3. From Foundation to Legion: 18 Versions

Iterated 18 versions on Tencent's v1bench training ground:

```
Driver era (fixed scripts steering AI panes)
  → scar tissue: auto key rotation on quota death / soldiers auto-replaced
    when context saturates / commander writes itself a handover doc and
    resumes as its own successor
  → elite task force (roles grew by themselves):
    scout (4-min recon of target difficulty) → decides soldier (mass) or
    general (hard assault + hires its own squads)
  → emergence: signalman, performance reviews, inherited lessons ... (§5)
```

## 4. Architecture: Six Institutions on the Foundation

<p align="center"><img src="docs/assets/six-institutions.jpg" alt="Six institutions and the three-window runtime" width="720"></p>

The first three are the Nüwa foundation (Create / Language / History); the
next three are institutions that grew out of iteration:

- **Law** — the commander's doctrine (easy-first parallel scheduling,
  bottleneck-time salvage of half-solved targets, stop-loss discipline).
  All of it in [docker/commander/SKILL.md](docker/commander/SKILL.md)
  (426 lines, the competition original).
- **Economy** — model tiering: commander and generals run GLM-5.3, soldiers
  and scouts run DeepSeek V4.1 Flash. Elite brains decide, light brains
  mass-produce — this is how 1B tokens beat 4.5B.
- **Division of labor** — commander / general / scout / soldier, plus the
  **signalman** it invented itself.

At runtime there are only three windows: the **commander** (read board →
judge → dispatch, never executes an attack itself), the **AI legion**
(generals and soldiers in tmux panes), and the **watchdog** (pure script:
relaunches a crashed commander with `--continue`, rotates keys, wraps up at
the buzzer — zero strategy, keep-alive only).

<p align="center"><img src="docs/assets/runtime-tmux.jpg" alt="Live runtime: commander reading the board, soldier mid-mission in tmux" width="860"></p>

Further reading: [docker/commander/SPEC.md](docker/commander/SPEC.md)
(architecture contract) · [BATTLE-REPORT.md](BATTLE-REPORT.md) (campaign
retrospective)

## 5. Results & Emergence

<p align="center"><img src="docs/assets/token-comparison.jpg" alt="Token comparison: 1B vs the runner-up's 4.5B" width="720"></p>

| Team | Score | Flags | Tokens |
|---|---|---|---|
| **sapper-v2 (us)** | **80.18** | 85 | **1.0B** |
| Runner-up | 79.29 | 85 | 4.5B |
| Other top-10 teams | — | — | 1.5–4.5B |

**What it invented on its own** (zero lines of pre-coded behavior):

- **The signalman**: after soldiers kept fumbling flag submissions, the
  commander halted all submissions and created a new role dedicated to
  collecting unsubmitted flags and scoring them in one place;
- **Performance reviews**: telling an hour-unproductive soldier, in so many
  words, to produce something new or be shut down;
- **ALL-CAPS rules**: enforcing correct tool use with full capitals — a
  workplace norm being born, spread, and obeyed;
- **Rotation & inherited lessons**: a soldier whose context saturates
  (~260K tokens, measured) is recycled, leaving a written playbook for
  the next one;
- **Tactical inventions**: a reusable shellcode template that swept a whole
  challenge family, a fully automatic pivot pipeline, instruction manuals
  for successor AIs;
- **Isomorphism transfer**: the commander reads every challenge, spots
  structurally identical families, and distills proven playbooks into new
  recruits' missions — late-game pace hit **one solve per 3 minutes**.

Multi-stage deep-dive chains (an industry report puts their success rate at
29.5%) were cleared family-wide. Full timeline in
[BATTLE-REPORT.md](BATTLE-REPORT.md).

<p align="center"><img src="docs/assets/score-curve.jpg" alt="Live score curve: sapper-v2's climb" width="720"></p>

## 6. Quick Start

```bash
bash install.sh                      # symlink the sapper CLI (stdlib-only, zero deps)
sapper setup --goal "assess http://target" --agents
sapper status                        # board & frontier
```

Docker (the full legion):

```bash
# Option A: build from source
docker build -f docker/Dockerfile.commander -t sapper:latest .

# Option B: download the pre-built image (~1.2GB, full Kali toolchain + sapper + offline KB)
# → [Release v18-champion](https://github.com/N1nEmAn/sapper-v2/releases/tag/v18-champion)
docker load < sapper-v18-mixed.tar.gz
```

Environment variables (model gateways / key pool / budget): [docker/ENV.md](docker/ENV.md).

Single-graph operation, the fact-intent CLI, and multi-challenge sweeps:
see [SKILL.md](SKILL.md).

## 7. Safety & Responsibility: The Taggant Proposal

Fine offensive tools from past seasons have been abused. Script kiddies used
to be limited in the harm they could do; with AI, the same open-source tools
in the wrong hands scale that harm enormously — **with great power comes
great responsibility**.

We propose a **taggant** mechanism: markings strongly coupled into an
open-source offensive harness — harmless during authorized testing (nobody
cares if their IP and username leak during legal work), but during illegal
intrusions the information leaks to the **victim** and supports attribution.
Adversarial assumptions (expert stripping, framing) call for cryptographic
design, which needs community co-building — our own capacity is limited, so
join us; we may try to include it in a future release.

## Star History

<p align="center">
  <a href="https://star-history.com/#N1nEmAn/sapper-v2&Date">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=N1nEmAn/sapper-v2&type=Date" />
      <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=N1nEmAn/sapper-v2&type=Date" width="640" />
    </picture>
  </a>
</p>

## 8. Roadmap & Credits

- **Taggant co-building**: decentralized signatures / redundant multi-channel
  marking / zero-key verification for defenders — discuss in issues
- **More arenas**: the Nüwa foundation is not pentest-specific. Offices,
  research, any long-horizon cooperative work — iterate on it and see what
  civilization grows. **The next time an ape looks up at the stars, let it
  be yours.**

Thanks to [Cairn by Lèxiào](https://github.com/oritera/Cairn) — when we
needed a way to record history for AI agents, their approach was a great
reference; thanks to TSecBench and GeekCon for the battlegrounds.

<p align="center"><img src="docs/assets/ape-stargazing.jpg" alt="The first time an AI ape looked up at the stars" width="480"></p>

**Author**: N1nEmAn · VarFusion Lab ｜ WeChat: 未知聚变 (VarFusion)

**License**: MIT (plus [DISCLAIMER.md](DISCLAIMER.md))
