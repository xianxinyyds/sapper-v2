# CTFY 初选赛：AI 指挥官部署与赛前验证

日期：2026-11-20（北京时间）
比赛：TPCTF 2026 · Agent Track · Qualifier
比赛 ID：`665edcdb007046939a5da83a546f5660`
赛程：2026-11-20 10:00–20:00（北京时间，UTC 02:00–12:00）

## 1. 本轮完成内容

- 新增 CTFY AI 指挥官：AI 依据实时看板自主选择 `work/hire/fire/stop`；30 分钟无实质进展仅作换题建议，难度/解题数/剩余 TTL/已投入时间均为软参考。
- 确定性层负责比赛发现、分页、实例容量、同题互斥、campaign/challenge 锁、答案 handoff、成功判分刷新、实例所有权与退出清理。
- 新增 `ctfy_local.py play` / `cleanup` 与可安装的 `ctfy-play` 技能（触发词“打比赛”）。
- 单题 Worker 沿用已验证的容器路径；修复了复用实例被误关、引擎安装符号链接覆盖宿主文件、分页总数不一致、重复事实刷新进展、已解决 ID 回退、信号处理重入、过期 campaign 静默空跑等缺陷。
- 全量测试：`python3 -m pytest -q tests` → **403 passed, 1146 subtests passed**。

## 2. VPS 部署

- 项目：`/root/workspace/sapper-c870444`
- 启动器：`/root/bin/ctfy-qualifier.sh`
- 预检：`/root/bin/ctfy-preflight.py`
- 凭据：`/root/.config/ctfy/env`（权限 600，不写入仓库）
  - `CTFY_TOKEN`（上一场 VPS Access）
  - `CTFY_INITIAL_TOKEN`（初选赛令牌）
  - 模型会话变量 `OPENAI_*` / `ANTHROPIC_*`（会话凭据不落文档）
- 主办方已把模型网关解析设为 `10.0.26.75`；未保留任何手写网关 IP 固定。启动器在运行时从 `LLM_BASE_URL` + `getent ahostsv4` 派生容器 `--add-host`。
- 镜像沿用 `sapper-ctfy-web-test:latest`；运行时只读挂载宿主 `lib/`，未重建镜像。

## 3. 定时任务

- `ctfy-qualifier.service`：`oneshot`，执行启动器；`TimeoutStartSec=11h`。
- `ctfy-qualifier.timer`：`OnCalendar=2026-11-20 10:00:00`，`AccuracySec=1s`，`Persistent=true`。
- 已启用：`systemctl enable --now ctfy-qualifier.timer`。
- 定时器机制已用临时 `systemd-run --on-active` 实测：定时触发脚本并成功完成网关预检。
- 启动器行为：
  1. 预检网关与比赛运行状态，最多等待 10 分钟。
  2. 以显式比赛 ID 启动 `play --stall-minutes 30 --max-workers 2 --poll-seconds 30`。
  3. 若题目列表尚未发布，`play` 正常退出后启动器会继续重试（最多约 20 分钟），直到看板出现题目，避免开赛瞬间空跑。

## 4. 赛前连通性证据

- 初赛令牌可访问平台：`GET /api/v1/competitions?phase=upcoming` 返回 Qualifier；`GET /competitions/<id>/challenges` 返回 200（赛前 0 题）。
- 模型网关：新会话经主办方 IP 返回 `ok`。
- `play` 赛前调用安全失败并给出清晰原因（比赛未运行），不会创建实例。
- VPS 上未发现 Qualifier 的遗留 `campaign.json`，开赛将从干净状态开始。

## 5. 赛中查看

```text
/root/workspace/sapper-c870444/runs/qualifier-play.log
/root/workspace/sapper-c870444/runs/665edcdb007046939a5da83a546f5660/campaign.json
/root/workspace/sapper-c870444/runs/665edcdb007046939a5da83a546f5660/decisions.jsonl
journalctl -u ctfy-qualifier -f
```

停止：首次 `SIGINT` 保留实例并有序收尾；第二次中断进入有界强制路径。比赛结束后可用 `cleanup --competition` 关闭本 campaign 创建的遗留实例。

## 6. 已知边界

- 初选赛开始前无法验证真实题目实例的完整“拉题→附件→解题→判分”链路；该链路此前已在 Cut the Clock 上由 Agent 自主解出并以平台 `answered_correctly=true` 验证。
- 模型决策质量不纳入代码测试；测试验证的是边界、安全与状态机。
- `engagement`、AWD、防御题与未知模式会被标记 `unsupported` 并跳过，不会按 simple 处理。
- 30 分钟无进展是建议而非强制换题；AI 可基于具体线索继续，但看板会显示连续 stalled 决策。
