# CTFY-0001 v3 单题解题 Agent 设计

## 意图

将 sapper-v2 从 TSecBench 的全场多题调度器，适配为 CTFY-0001 v3 托管容器中的单题自主解题 Agent。用户是参赛选手，本设计不包含 CTFY-0002 出题、上传或审核能力。

## 目标

- 每次运行只处理 `task.json` 指定的当前 `instance` 题目。
- 支持平台预置的 HTTP、HTTPS、TCP、UDP、SSH endpoint 和已复制附件。
- 尽快记录并提交候选答案，依据 `correct` 更新答案账本。
- 正确答案后刷新 challenge，将 gated questions 加入当前图。
- 每轮原子写入新的 `result.json`，正常未解出也退出 0。
- dry-run 不需要凭证、不调用模型，写结果并退出 0。
- 使用平台注入的 `LLM_FLAVOR/LLM_API_KEY/LLM_MODEL/LLM_BASE_URL`，支持 anthropic 与 openai CLI 路线。
- 后续轮次复用同一工作目录和事实图，不重复初始化或重复提交已知答案。

## 非目标

- 不实现 `qa`、`defence`、`awd`、`engagement` 模式；遇到时明确报不支持。
- 不实现出题、题目上传、AWD、跨题 sweep 或自行启动/关闭实例。
- 首阶段不启用 commander/tmux 多 worker；单进程 `auto` 闭环验证后再单独设计。
- 不物理删除旧 TSecBench adapter 和命令，只从 CTFY 入口断开。
- 不伪造 turns、token 或 tool-call 计数；没有真实统计时省略这些可选字段。

## 约束

- 只实现 CTFY-0001 `schema_version: 3`。
- `/ctfy/work` 是 cwd、任务目录、附件目录、结果目录和同题后续轮次的持久目录。
- `task.json` 是快照；`gated_questions > 0` 时正确答案后必须重新读取 challenge。
- 平台已启动目标；禁止 `POST /api/v1/instances`，禁止访问其他题目。
- `result.json` 是结果通道，stdout 不计分；最终 `answers` 会由平台提交。
- 已判错的答案不放入最终 `answers`，避免再次消耗 wrong-attempt budget。
- `LLM_API_KEY` 缺失时明确失败，不回退到个人 key 或 `SAPPER_KEY_POOL`。
- `LLM_BASE_URL` 和 `LLM_API_KEY` 必须一起使用；hosted run 必须实际调用平台网关。
- 不在 `/ctfy/work` 写入 `CTFY_TOKEN`、模型 key、key pool 或复制后的凭证配置。
- 新平台代码使用 Python 标准库；测试沿用 `unittest`。
- 保持现有图锁、原子写、typed facts 和提示注入防御。

## 架构

新增三个彼此独立的边界：

1. `lib/ctfy.py`：协议适配。读取 task、调用当前题的 REST API、下载缺失附件、提交答案、写结果。它不依赖事实图。
2. `lib/sapper.py` 的 `answer` 命令：答案账本和图同步。它从 task 获取当前 challenge/instance，先记录 `kind=flag` 的兼容事实，再提交并记录 verdict；正确答案后刷新 questions。
3. `lib/ctfy_run.py`：单次受评运行生命周期。dry-run、模型配置、单题图初始化/恢复、调用现有 `auto`、最终汇总结果。

模型层保留现有 `run_claude()` 契约：anthropic 直接调用 Claude Code；openai 通过一个小的 Codex 非交互适配器，将 Codex 最后一条消息转换成现有 `{"result": ...}` envelope。平台已经生成 Codex 配置，项目不复制旧 DeepSeek 配置。

CTFY 使用独立提示模板，防止旧 `arena start/list/hint/close` 指令进入受评运行。

## 文件结构

### 新增

- `lib/ctfy.py`：CTFY v3 task、HTTP client、附件、结果文件。
- `lib/ctfy_engine.py`：校验并映射平台模型环境，选择 Claude 或 Codex。
- `lib/codex_engine.py`：现有 Claude 风格调用到 `codex exec` 的适配。
- `lib/ctfy_run.py`：单题受评运行入口。
- `docker/entrypoint-ctfy.sh`：绝对路径容器入口。
- `templates/ctfy/{reason,explore,verify}.md`：单题 CTFY 提示。
- `tests/test_ctfy.py`、`tests/test_ctfy_run.py`：协议与生命周期测试。
- `docker/CTFY.md`：记录本地验证和运行约束。

### 修改

- `lib/sapper.py`：新增 `answer` CLI、答案状态与 question 同步；旧 `flag/arena/bench` 保持独立。
- `docker/Dockerfile`：复制新入口和代码，安装 Codex，移除旧比赛默认预算，准备 `/ctfy/work`。
- `SKILL.md`：补充 CTFY 单题命令，标明 CTFY 与旧 arena 的边界。

### 不在本阶段修改

- `docker/commander/*`、`docker/Dockerfile.commander`、旧 `docker/entrypoint.sh`。
- `lib/arena.py` 和旧 `bench/sweep/fleet` 实现。

## 答案与结果语义

答案事实继续使用现有 `kind=flag`，避免破坏图和渲染，但 `data` 扩展为：

- `platform=ctfy`
- `challenge_id`
- `instance_id`
- `question_id`
- `value`
- `status=recorded|accepted|rejected|handed_off`
- `result`：截断后的平台响应，不含请求头或 token

去重键为 `(instance_id, question_id, value)`，不再只按 flag 字符串去重。question 可以来自当前 task，也可以来自本轮 correct 后已同步到图中的新 question；不得只用旧 task snapshot 拒绝新解锁问题。

提交响应以 `correct: true|false` 为准，不复用旧 Arena 的 HTTP 200 判断。提交 POST 不自动重试，避免响应不明时重复消耗 wrong-attempt budget。传输失败时答案保留为 `recorded`，由最终结果兜底提交；成功写入结果后将其标为 `handed_off`，后续轮次不再自动携带。已 `accepted` 的答案计入 `solved`，不重复放入最终 `answers`；已 `rejected` 或 `handed_off` 的答案也不放入。

每轮开始读取当前 challenge briefing；instance 模式提交正确后刷新当前实例，以 `answered_correctly` 更新逐题状态，以 `grace_kind=solved` 更新整体完成状态。静态 challenge 的 `my_solved_question_ids/my_fully_solved` 不作为实例进度。QA 仍使用 challenge 的对应字段。`fully_solved` 不来自 Agent 猜测，结果只包含有依据的字段和 `stop_reason`。

## 多轮与完成条件

同一 `$CTFY_WORK_DIR/.sapper` 跨轮保留。每轮重新读取 task、刷新 challenge briefing，并同步新 endpoint、附件和 questions；已有 question fact 不重复创建。平台已 solved 的 question 不依赖本地提交记录也能识别。

平台明确 fully solved 时可以完成。静态字段未表示 fully solved 时，仅允许在初始 gated 数为零、所有已知可见问题均已 accepted 或列为 solved、没有已知隐藏或未解问题且无 blocker 时完成；单题 accepted 不能覆盖其他可见或 gated 问题。此时 `stop_reason=done` 与 `result.fully_solved=false` 可以并存。若模型提前完成图而后续轮次出现新工作，入口使用受测的 `Store.reopen_project()` 恢复 active；已有 goal edge 在再次完成时幂等复用，不重复创建。

## 错误与退出

- dry-run：写 `stop_reason=dry_run`，退出 0。
- auto 正常结束：写账本结果，退出 0，无论是否解出。
- auto 返回非零：尽量写结果，退出非零。
- 配置或协议错误：能写结果时先写 `stop_reason=config_error` 或 `unsupported_run_mode`，再非零退出。
- 无法读取工作目录/task 或无法写结果：非零退出并输出不含 secret 的错误。

## 验证

- 现有 48 个测试保持通过。
- 本地 HTTP mock 验证 Bearer、精确路径、提交体、verdict、429/5xx 重试和附件文件名安全。
- dry-run 在无 token/key 时快速退出 0。
- HTTP 与纯 TCP task 都能初始化，不依赖 `CTFY_TARGET_URL`。
- answer 在提交前先落盘；correct/wrong/transport failure 状态正确。
- 正确答案后新 question 出现在图中；重复运行不重复建 fact。
- rejected answer 不进入最终 answers；未提交 candidate 能进入，且写入结果后变为 handed_off，下一轮不重复携带。
- 后续轮次重新生成 result，保留图，且不出现 token/key。
- 容器入口使用绝对路径，不调用 `sapper bench`。
