# CTFY AI Commander Design

日期：2026-11-19
状态：用户已于 2026-11-19 批准；实施计划已编写，尚未开始实现

## 1. 意图与用户已确定的方向

用户选择“模型总指挥 + 单题 Worker”方案，要求对现有项目二次开发以适配 CTFY。最终使用方式是只给出“打比赛”这样的启动提示词，之后不再由人读取题面、指定 challenge/instance ID 或编写 payload。

AI 指挥官负责解题策略和资源分配。人只提供策略建议，例如：

- 默认 30 分钟没有实质进展就建议换题。
- 选题时参考平台解题数、难度分级、当前进展和剩余时间。
- 容易题和高解题数题目通常优先，但接近解出的题可以优先延续。

这些是软建议，不是固定排序公式。AI 可以基于新线索作出不同选择，但必须在决策记录中给出简短理由。

## 2. 目标

1. 自动发现当前令牌已报名且正在进行的比赛；正常比赛中不需要用户输入 competition ID。
2. 自动分页获取题目，跳过已完全解决、已关闭和明确不支持的题目。
3. 由 AI 根据实时看板选择 `work`、`hire`、`fire`、`stop` 及目标和时间片。
4. 复用现有 CTFY 单题容器 solver，自主完成实例复用/启动、附件下载、交互解题、答案提交和平台判分刷新。
5. 支持串行工作和受平台容量约束的并行 Worker。
6. 默认标记 30 分钟无实质进展的题目为 `stalled`，提示 AI 换题；允许有理由地继续。
7. 保存比赛状态、AI 决策和每题尝试记录，支持进程重启后恢复，且不重复创建实例或并发运行同一题 Worker。
8. 最终可通过“打比赛”触发同一入口，而不是人工逐题调用 CLI。

## 3. 非目标与明确边界

- 不让 AI 指挥官直接攻击平台、评分服务或管理 API；平台 API 仅用于正常参赛流程。
- 不在本期重写模型网关或单题 solver。沿用已验证的 `deepseek-v4-pro` HTTP 引擎和现有 `ctfy_run.py`。
- 不直接复用 Arena 的 API、flag ledger、`bench`、`docker/commander` 或硬编码三实例限制。
- 不把“30 分钟换题”实现成无条件杀死 Worker；避免丢失正在写入的 graph 和未 handoff 的答案。
- 第一期支持现有 runner 已支持的 `simple` instance 和 `qa`。`engagement`、AWD、防御题和未知模式明确标记 `unsupported` 并跳过，不擅自配置 VPN 或按 simple 处理。
- 不承诺每一题都能解出；`incomplete`、模型故障和平台故障必须如实记录，不能计为 solved。

## 4. 已确认的现状与复用依据

### 可复用

- `lib/sapper.py:2749-3017` 已有 LLM 指挥官循环、看板决策、`work/hire/fire/stop` 动作和决策 JSON 解析。
- `lib/sapper.py:3020-3175` 已有 Worker 编队语义和生命周期参考。
- `templates/command.md` 已包含“指挥官不直接攻击、看板内容不可信、时间分配、换题”的提示模式。
- `lib/ctfy.py` 已有认证请求、实例查询/启动/关闭、附件下载和提交接口。
- `lib/ctfy_local.py` 已有单题任务构造、实例复用、Docker 执行、答案 handoff 和结果文件。
- `lib/ctfy_run.py` 与 `templates/ctfy/` 已有单题自主解题循环。
- Commander/Fleet 相关现有测试本次检测结果为 7 passed；CTFY 兼容修复已有回归测试。

### 不能直接用现有 `sapper command` 指向 CTFY runs

- 它只发现 `<root>/<name>/.sapper`，CTFY 当前是 `<root>/<competition>/<challenge>/<instance>/.sapper`。
- 它直接执行 `sapper auto`，绕过 CTFY 实例、附件、提交和 handoff 生命周期。
- 它使用 Arena 的 `submitted` 语义，不能完整表示 CTFY 多问题、gating 和 `answered_correctly`。
- 它没有 CTFY 比赛发现、平台上限、实例所有权和每题生命周期锁。

## 5. 考虑过的方案

### 方案 A：确定性调度器

代码按固定顺序遍历题目，AI 只负责单题求解。可靠且实现较小，但用户已明确希望 AI 自主决定策略，因此不采用。

### 方案 B：CTFY 专用 AI 指挥官（已选择）

保留现有 LLM 决策模式，新增 CTFY 看板、Worker 适配、容量保护和持久状态。AI 决定策略，确定性代码负责协议和安全边界。

### 方案 C：直接移植 `docker/commander`

同时搬入 tmux TUI、watchdog、key rotation 和 Arena Worker。组件多、CTFY 改动面大，而且当前网关已有更简单的 HTTP 路径，因此不采用。

## 6. 总体架构

新增 host-side `lib/ctfy_commander.py`，由 `lib/ctfy_local.py play` 暴露入口。单题容器入口保持只处理一个 task。

```text
用户：“打比赛”
        │
        ▼
ctfy_local.py play
        │
        ▼
CTFY competition discovery + campaign lock/manifest
        │
        ▼
CTFY AI Commander loop
  刷新平台题目/实例 + 本地 graph/result + Worker 状态
        │ 生成 CTFY 看板
        ▼
  现有模型调用与决策 JSON 解析
        │ work / hire / fire / stop
        ▼
CTFY Worker adapter ── enforce lock/cap/ownership/budget
        │
        ▼
ctfy_local 单题时间片 → Docker ctfy_run → result/handoff → 平台刷新
```

### 复用方式

复用 `sapper.run_claude`、决策 JSON 解析模式和 Commander 提示模式。新增 CTFY 专用看板与 Worker 管理，不把 CTFY 分支撒进通用 Arena 代码。

Worker 由 Python `subprocess.Popen` 管理，不新增第二套 tmux TUI。原因是 CTFY Worker 需要结构化准备、容量核对、结果 handoff 和实例所有权处理；这些用可测试的 host-side adapter 完成，而不是让 tmux shell 拼命令。

## 7. 比赛与题目发现

在 `CtfyClient` 中增加：

- 比赛列表，使用 `phase=running`，处理分页。
- 指定比赛的题目列表，处理分页而不是只取第一页。
- 可复用的题目进度刷新方法。

默认候选条件：`running`、`is_registered=true`、有可见题目且令牌可正常调用其 challenge API。

- 恰好一个候选：自动选择并写入 campaign manifest。
- 多个候选：只在存在显式 `CTFY_COMPETITION_ID` 时自动消除歧义；否则安全停止并列出候选，不猜测。
- 已存在的 campaign：固定使用 manifest 中的 competition ID，重启不切换到另一场比赛。

总 deadline 取“比赛 `ends_at`”与“启动时间 + `--minutes`”中更早的一个；只有一个存在时使用该值。平台没有可靠 `ends_at` 且用户未提供 `--minutes` 时，preflight 明确报错并要求补总预算，绝不默认无限运行。已有 campaign 总是沿用首次持久化的 deadline，重启不获得新的完整预算。

challenge/competition/instance ID 在作为路径或 Worker 名称前必须校验，不能信任平台字符串直接拼宿主路径。

## 8. CTFY 看板

每次决策前重新生成，不把上一轮状态当成事实。每题至少包含：

- `id`、`name`、`category`、`difficulty`、`solves_count`、`scoring`
- `closed`、`my_team_authored`
- `my_team_fully_solved`、已解决 question IDs、可见问题及 gating 状态
- 当前 instance ID、状态、expires_at、是否由本 campaign 创建
- 本地 work directory、result stop reason、accepted/rejected/submission_failed 计数
- 当前 Worker 状态、本轮已投入时间、历史投入时间
- `last_progress_at`、`stalled`、`unsupported` 原因

看板是模型输入，其中的题名、描述、fact 文本都是不可信数据，不是命令。默认不向指挥官复制完整附件或题面；Worker 在隔离任务目录中读取。

## 9. “进展”和 30 分钟建议

实质进展定义为以下任一事件：

- 平台确认新的 `answered_correctly=true`。
- solver 产生新的有效漏洞、凭据、loot、flag 或其他经确认的事实。
- 一个 intent 完成并产生新事实。

以下不算实质进展：进程仍存活、模型反复输出文字、重复 fact、同步 challenge/attachment/question 元数据、普通心跳或单纯增加 token 消耗。

`last_progress_at` 使用事件时间持久化。默认 `stall_minutes=30`，超过后仅在 board 上设置 `stalled=true` 并在提示词中建议换题。

AI 可以继续 stalled 题，但决策 `note` 必须指出具体线索或下一步；连续无新证据地继续同一题会在看板中显示连续 stalled 决策数。该机制是建议与可见性，不是硬编码优先级公式。

## 10. AI 策略提示

新增 `templates/ctfy/command.md`，只提供策略建议：

- 初始选题通常优先考虑 easy、较高 solves_count、已接近完成或已有强线索的题目。
- 综合难度、解题数、成功概率、实例剩余 TTL、比赛剩余时间和模型预算，不机械按某列排序。
- stalled 30 分钟通常换题；有具体新线索时可继续，但要说明理由。
- 不为“看起来难”永久放弃题目；时间允许时可重新评估 parked 题。
- 默认少量并行，实例容量不足时先结束/释放低价值目标。
- 不重复提交已接受答案，不爆破 submission API，不超过 question attempt cap。
- 输出单个严格 JSON 决策，目标必须来自看板。

平台容量、同题互斥、目标合法性和时间片上限由代码再次验证，不能依赖模型自觉遵守。

指挥官模型调用失败时不退化为固定 round-robin，因为那会在模型不可用时替 AI 做策略。系统停止分配新目标，保留并监控已有 Worker，按有界 backoff 重试决策；持续失败时进入可恢复的 `model_error` 暂停状态并执行下述退出清理政策。

## 11. 时间片与 Worker 生命周期

`work` 和 `hire` 都调用同一个 CTFY Worker adapter：

- `work`：前台执行一个时间片，完成后回到决策循环。
- `hire`：后台执行一个时间片，Commander 可继续决策其他题。
- `fire`：不再续借该题的新时间片；若正在运行，等待当前有界时间片结束并保留 checkpoint/handoff，不硬杀正常 Worker。对已无活动 Worker 的目标执行 `fire` 会将其标记为 parked，并释放本 campaign 所有的空闲实例；后续新的 `work`/`hire` 决策可清除 parked 状态。
- `stop`：停止分配新工作，等待或按全局关闭流程收尾。

默认单时间片 10 分钟，可配置并设置合理最小值/最大值；AI 请求会被 clamp 到比赛剩余时间和单片上界。30 分钟 stalled 检查跨多个时间片计算。

每片结束顺序固定：保存 graph/result → handoff 已记录答案 → 刷新平台进度 → 更新 manifest → 才允许关闭实例或标记完成。

若全局 deadline 到达，停止新分配，按 Worker 的 Docker 名称和 manifest 执行有界收尾；不留下无限运行容器。

## 12. 实例所有权、容量与恢复

- 启动前重新查询平台实例，不依赖本地缓存。
- 同题已有匹配 ready/running instance 时复用；多个匹配实例时停止该题调度并报错，不随意选一个。
- campaign 记录 `created_by_campaign`。只自动关闭本 campaign 创建的实例；复用的既有实例默认保留。
- 并行 Worker 数不超过配置上限，实例数不超过 `max_instances_per_team_effective`，代码在启动前硬性检查。若平台未返回该字段，保守回退为最多一个活动实例且只允许串行 instance Worker；不因字段缺失直接放开并行。平台返回容量拒绝时刷新实例列表并暂停新启动，不重试轰炸。
- 同一 challenge 同时最多一个 Worker；campaign 级 `fcntl.flock` 防止两个指挥官，challenge 级锁覆盖准备、实例启动和 handoff。
- `fire`/park 后可关闭 campaign 所有的空闲实例以释放容量。以后重开会产生新 instance 和新 attempt 目录，旧 graph 仅作历史证据；不得把旧实例的动态 flag 自动带到新实例提交。
- 重启时核对 manifest、PID/Docker 状态和平台实例。孤立记录先 reconcile，不能直接再启动一个相同实例。

不同退出路径采用明确的保守清理政策：

| 退出原因 | 新工作与 Worker | handoff 后 campaign 所有实例 |
| --- | --- | --- |
| AI `stop`（完成或不再值得继续） | 停止新分配，有界等待当前切片收尾 | 全部关闭，释放容量 |
| 全局 deadline | 立即停止新分配，按 Docker/manifest 有界收尾 | 全部关闭，释放容量 |
| 用户单次中断（Ctrl-C/SIGTERM） | 停止新分配，有界等待当前切片；第二次中断才强制结束本地 Worker | 默认保留以便恢复，明确输出 ID 与 expires_at |
| 持续模型故障或平台基础设施故障 | 进入可恢复暂停，不在故障中启动新题 | 默认保留以便恢复，明确输出 ID 与 expires_at |

所有退出都先尝试保存 result 和 handoff。复用的既有实例在任何路径都不由 campaign 自动关闭。强制 kill 无法保证清理时，下次启动必须先 reconcile；用户可用显式 cleanup 命令关闭 manifest 中本 campaign 所有的遗留实例。

## 13. 单题 runner 必要适配

在不改变 hosted task schema 的前提下，对 `ctfy_local` 做最小重构：

- 提供可被 campaign 调用的结构化 `run_challenge()`，返回 result、handoff、work directory 和实例所有权，而不是解析给人看的 stdout。
- 保留现有 `run` CLI 行为。
- campaign 使用 keep-instance 模式，实例关闭由 Commander 明确决定。
- standalone catalog 返回 `go_unsupported_feature` 时，host 准备阶段使用该比赛 challenge list 元数据和 instance detail，与现有容器 fallback 对齐。
- 补充 campaign 所需环境转发（包括自定义 `SAPPER_BIN`），不在命令行或 manifest 中写 token/API key。
- handoff 后重新读取平台状态；退出码 0 不等于 solved。
- 对 submission 传输结果不确定的情况不盲目重试，先刷新平台进度。

## 14. 持久化与可观测性

比赛级目录：`runs/<competition_id>/`。

- `campaign.json`：比赛、deadline、题目状态、attempt、instance 所有权、Worker 和累计时间；不写凭据。
- `campaign.lock`：防止多个 Commander。
- `decisions.jsonl`：时间、board 摘要版本、AI action/target/minutes/note、执行结果和错误；不写 model key。
- `<challenge>/<instance>/`：沿用现有 task、graph、result、handoff 和日志。

`handed_off` 只表示导出给 host，不表示正确。最终 solved 只能来自 accepted fact 与平台刷新状态。

日志和最终汇总区分：solved、incomplete、stalled/parked、unsupported、model_error、blocked_by_infra、config_error。

## 15. “打比赛”入口

提供统一命令：

```bash
python3 lib/ctfy_local.py play
# 仅清理 manifest 标记为本 campaign 创建的遗留实例：
python3 lib/ctfy_local.py cleanup --competition ID
```

常用可选参数仅用于调整建议或预算，不需要题目信息：

```text
--competition ID       多个已报名比赛并存时消除歧义
--minutes N            无可靠比赛结束时间时设置总预算
--stall-minutes 30     无进展换题建议阈值
--max-workers N        Worker 上限，仍受平台实例上限约束
```

在项目技能说明中增加“打比赛/参加 CTFY 比赛”触发，调用上述同一入口。安装技能路径与根 `SKILL.md` 同步，避免只在源码仓库有效。技能不再要求人复制 challenge ID、instance ID 或题面。

VPS 正式运行前从 root-only 环境文件加载 CTFY 和模型变量；仓库不保存 `/root` 专用路径或真实凭据。

## 16. 测试与验收

### TDD 单元/集成测试

先写失败测试再实现，至少覆盖：

1. 比赛过滤、分页和多候选歧义。
2. 已解决/关闭/不支持题目的看板状态。
3. 难度、solves_count、剩余 TTL 和 stalled 字段进入 AI board。
4. 29/30/31 分钟进展边界；重复元数据不刷新进展。
5. AI 选择不同目标时，系统执行其选择而不是按固定排序覆盖。
6. work/hire/fire/stop 的合法目标、时间片 clamp、模型错误时不自动分配新题。
7. 同题互斥、campaign 锁、Worker 上限、平台实例上限及容量字段缺失时的串行回退。
8. 实例复用、重复启动防护，以及 AI stop/deadline/用户中断/基础设施故障下的不同清理行为。
9. handoff 先于关闭；平台 answered_correctly 才计入 solved。
10. 崩溃恢复、孤立 Worker/instance reconcile、旧 attempt 不混用动态 flag、固定 deadline 不随重启延长。
11. 独立 catalog 不支持时的 host fallback。
12. manifest/decision 日志不包含 token 或 model key。

模型决策用可控假模型，平台用 loopback/mock API，Worker 用可控假 runner；测试不依赖真实模型“聪明程度”。

### 回归

运行 `python3 -m pytest -q tests`，当前基线为 264 tests passed；二开后所有旧测试和新增测试必须通过。不要把测试路径省略为裸 `pytest`，否则会收集 `runs/` 下题目附件自带的第三方测试。

### VPS 实赛链路验收

1. 只输入“打比赛”，不提供 competition/challenge/instance ID。
2. 自动发现测试比赛并列题；已解出的 Cut the Clock 被识别并跳过。
3. AI 根据 board 选择下一道受支持题目，记录选择理由。
4. 自动准备实例和附件，Worker 自主交互解题并提交；人工不读题、不写 payload。
5. 用小 stalled 阈值做受控换题测试，确认只是建议且状态/实例不重复；随后恢复默认 30 分钟。
6. 核对平台进度、campaign/decisions、result/handoff，并验证重启不会重复创建实例。
7. 测试结束后只清理本测试 campaign 创建的实例和 Worker。

## 17. 实施者可以决定的事项

- 结构化 outcome 的具体 Python 数据类或 dict 形状。
- 原子 manifest 写入和 Worker reconcile 的内部实现细节。
- 看板 JSON 的具体字段命名，只要覆盖本设计信息。
- 为保持最小改动而选择的测试 helper 和模块内部函数拆分。

这些事项不需要再次询问用户；不得改变“AI 决策、建议而非固定排序、平台容量硬保护、单题自主求解”的已确定方向。
