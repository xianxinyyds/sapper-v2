# CTFY-0001 v3 运行环境清单

本文只描述当前单题解题镜像的 `run_mode=instance` 与 `run_mode=qa` 入口，覆盖
Crypto、Reverse、Misc、Web、Pwn 与 AI 安全。旧
`sapper bench` / `sapper arena` 的 `BENCHMARK_*`、个人 key pool 和多题计时变量
不属于此运行路径。

## 1. 平台与任务提供

| 变量或字段 | 说明 |
|---|---|
| `CTFY_WORK_DIR` | 工作目录，默认 `/ctfy/work` |
| `CTFY_PLATFORM_URL` | 当前 CTFY 平台基址 |
| `CTFY_TOKEN` | 当前 run 的 scoped Bearer token |
| `task.max_turns` | 正整数时作为本轮 turn 预算，优先于环境变量 |
| `CTFY_EVAL_MAX_TURNS` | `task.max_turns` 缺失或无效时的 turn 预算 |
| `CTFY_EVAL_DRY_RUN=1` | 无凭证 dry-run；不写死到正式评测配置中 |

Instance 模式的 `target_instance` 是平台已经启动的实例 ID。Agent 不创建实例，
只访问当前题的 challenge、instance、附件和 submission API。QA 模式的
`target_instance=null` 是正常输入；附件必须已有本地 `path`。QA 使用任务中的
`competition_id` 和 `challenge_id`，答案先记录，再单次调用
`POST /api/v1/qa/submissions` 判分；只有 transport 失败才通过 `result.json.answers`
降级移交。QA 不调用 instance API。

## 2. 平台模型连接

| 变量 | 说明 |
|---|---|
| `LLM_FLAVOR` | `anthropic` 使用 Claude Code；`openai` 使用 Codex |
| `LLM_API_KEY` | 平台提供的模型凭证 |
| `LLM_MODEL` | 请求平台网关的准确模型 ID |
| `LLM_BASE_URL` | 可选提供商端点覆盖 |

GLM 评测应选择供 Claude Code 使用的 Anthropic 兼容连接，使
`LLM_FLAVOR=anthropic`。普通 Chat Completions 连接不能直接交给只支持
Responses API 的 Codex。模型连接、网关和密钥必须来自同一套平台配置。

入口会清理 `SAPPER_KEY_POOL`，不会回退到个人密钥。不要把 token、模型 key、
registry 凭证或 key pool 写入 Dockerfile、镜像层、Git、日志或 `/ctfy/work`。

## 3. 可选预算与可靠性参数

| 变量 | 语义 |
|---|---|
| `SAPPER_MAX_STEPS` | 对任务/平台 turn 预算再施加更低安全上限，不是独立默认值 |
| `SAPPER_MAX_EXPLORE` | explore dispatch 上限；失败 dispatch 也计数，默认等于 turn 预算 |
| `SAPPER_ROUND_MINUTES` | 本轮 auto 的真实 wall-clock 分钟预算，默认 `165` |
| `CTFY_DISPATCH_TIMEOUT` | 单次 reason/explore dispatch 上限，默认 `300` 秒，优先于 `SAPPER_TIMEOUT` |
| `SAPPER_TIMEOUT` | 未设置 `CTFY_DISPATCH_TIMEOUT` 时的 dispatch 上限覆盖 |
| `SAPPER_RETRY_MODEL` | reason 的非 timeout 瞬态错误有界重试；explore 与 timeout 不整体重试 |

没有任何平台 turn 预算时默认 `8`。预算环境变量必须是正整数，否则写
`config_error`。turn 数不能当作秒数；需要真实倒计时才设置
`SAPPER_ROUND_MINUTES`。绝对 deadline 取该预算与可解析 `challenge.close_at`
的较早值；auto 额外保留 300 秒 finalization reserve，预留期内不启动新 dispatch。
每次 reason/explore 后原子 checkpoint，中间结果为 `incomplete`，不改变答案状态；
最终 finalize 才把 recorded candidate 标为 `handed_off`。

Challenge GET 和附件下载使用有界网络重试，答案 POST 始终单次。Instance
health GET 也始终单次；持续 `instance_not_found` 只形成 blocker，不做长轮询。
引擎以 `start_new_session=True` 启动并回收 process group；主动 daemonize、
脱离进程组的程序不保证可回收，因此不要在执行命令中使用 nohup 或后台 `&`。

## 4. 结果检查

stdout 不是计分通道。检查 `/ctfy/work/result.json`：

- `done` 只表示图已完成或平台确认 `fully_solved=true`。
- 普通未解出可以是 `incomplete`、`no_progress`、`max_steps`、`explore_cap`
  或 `deadline`，不应误报为完成。
- QA transport 降级移交使用 `answers_handed_off`；HTTP/协议失败使用
  `submission_failed`，不会静默移交。
- Instance、refresh、gating 或 `platform_unreachable` blocker 使用
  `blocked_by_infra`，答案仍保留。
- 模型/执行器失败使用 `model_error`、`explore_failed` 或 `auto_failed`，
  与配置错误 `config_error` 区分。

发布前运行完整测试、本地镜像构建和无凭证 dry-run；只有本机已经安装官方
`ctfy` CLI 时才运行 `ctfy agent check`。本地 loopback 通过不等于真实评测
解题率或官方平台验证通过。
