# CTFY 初赛：修复版与回退版行动方案

日期：2026-11-20
适用：VPS 网络恢复后立即执行。两个方案互斥，按决策规则选择。

## 本次本地修复（相对比赛时的失败版本）

1. **指挥官模型调用走 HTTP 引擎**
   - `play` 在构造 `Commander` 前设置 `SAPPER_BIN=<--engine>`，并设置 `CTFY_ENGINE_NO_TOOLS=1`，决策请求不再依赖宿主 `claude` CLI，也不会陷入工具探索循环。
   - 引擎兼容裸网关地址：`LLM_BASE_URL` 缺少 `/v1` 时自动补全；`LLM_MODEL` 环境变量生效。
2. **失败不再静默**
   - `Commander.run()` 刷新/决策失败会写入 stderr，便于从 launcher 日志定位；连续失败仍按有界退避后进入 `model_error`。
3. **engagement VPN 全自动**
   - 新增 `lib/ctfy_vpn.py`：按实例下载 `vpn-config`，写 `/etc/wireguard/ctfy-<id>.conf`（600），`wg-quick up`，幂等复用；实例关闭时自动 `wg-quick down` 并删除配置。
   - `ctfy_local.run_challenge` 对 engagement 实例自动建隧道，Worker 容器使用 `--network host`，可解析 `*.internal` 并访问目标。
   - 不再需要人工执行 `wg-quick`。
4. **实例复用不依赖平台列表**
   - WorkerPool 把 campaign 清单中的实例 ID 通过 `--instance-id` 传给 Worker；即使平台 `list_instances` 返回空，也能复用已创建实例，避免重复启动触发 `team_capacity_exceeded`。
5. **容量拒绝不再死循环**
   - 平台容量拒绝后暂停新启动 5 分钟，而不是每 30 秒重试轰炸。
6. **保活（心跳 + 看门狗）**
   - Commander 每轮写 `runs/<competition>/heartbeat`。
   - 新增 `scripts/ctfy-watchdog.sh`：比赛窗口内每 5 分钟检查；
     - 锁空闲 → 启动服务；
     - 锁被持有但心跳超过 900 秒未更新 → 校验 PID 命令行后终止卡死的 `play`，由 launcher/服务重启；
     - 心跳正常 → 不做动作。
7. **启动脚本**
   - 新增 `scripts/ctfy-qualifier-launcher.sh`：单一入口，自动加载 root 专属环境文件、派生网关 `--add-host`、调用 `play`，无任何手工 VPN/实例参数。
8. 测试：`python3 -m pytest -q tests` → **416 passed, 1145 subtests passed**。

## 方案 A：部署修复版（首选）

1. 同步文件到 `/root/workspace/sapper-c870444`：
   - `lib/`（含新增 `ctfy_vpn.py`）
   - `openai_http_engine.py`
   - `scripts/ctfy-qualifier-launcher.sh`、`scripts/ctfy-watchdog.sh`
   - `templates/ctfy/`
2. 把 launcher 安装到 `/root/bin/ctfy-qualifier.sh`，并设置 VPS 变量（不要写死到仓库）：
   `CTFY_PROJECT=/root/workspace/sapper-c870444`、`CTFY_ENV_FILE=/root/.config/ctfy/env`、`CTFY_COMPETITION_ID=<初赛ID>`。
   若比赛早期手动创建的实例仍有效，可一次性收养，避免容量不足：
   `CTFY_ADOPT=<challenge_id>=<instance_id>`。被收养实例标记为非本 campaign 创建，永不自动关闭。
3. 确认 VPN 工具存在：`wg`、`wg-quick`、`resolvconf`。
4. 停用旧的手工 VPN（若存在）：`wg-quick down qualifier-vpn`，删除 `/etc/wireguard/qualifier-vpn.conf`；由代码按实例自动管理。
5. 失效过期的 `campaign.lock`/`campaign.json`：保留 live campaign，删除已过期 manifest；启动器会自动新建。
6. 重启服务并观察：
   - `journalctl -u ctfy-qualifier -f`
   - `runs/qualifier-play.log`
   - `runs/<competition>/decisions.jsonl` 出现 AI 决策；
   - `docker ps` 出现 `sapper-local-*` Worker；
   - `wg show` 出现 `ctfy-<instance>`。
7. **验收标准**：AI 决策 → Worker 启动 → 自动建 VPN → 目标 HTTP 可达 → 平台出现提交/进度；或至少 Worker 连续运行而非秒退。

## 方案 B：回退稳定版（昨日未加调度系统的版本）

回退点：`c870444`（“expand CTFY Pwn Reverse and Crypto knowledge bases”）。VPS 上还有 `sapper-c870444.tar.gz` 备份。

1. 停止并禁用调度单元：
   `systemctl stop ctfy-qualifier.service ctfy-qualifier-watchdog.timer ctfy-qualifier.timer`
2. 用备份恢复项目（或本地 `git archive c870444` 上传）。
3. 不启用定时器；按单题方式运行：
   `python3 lib/ctfy_local.py run <challenge_id> --competition <id> --image sapper-ctfy-web-test:latest --keep-instance --engine openai_http_engine.py`
4. 稳定版没有自动 VPN；engagement 题需要人工先建立该实例的 WireGuard 隧道并让容器使用宿主网络。若不想人工操作，应优先选择 QA/直连题，或在方案 A 修复后再启用。
5. 优点：避开新调度系统的未知缺陷；缺点：不具备“一句话打整场”和自动保活。

## 决策规则

- 网络恢复后先执行方案 A，最多尝试 2 次；每次用日志确认失败层（平台/模型/VPN/容器）。
- 若 2 次仍在 10 分钟内无法让任一 Worker 持续运行，立即执行方案 B，保证至少能用稳定单题路径拿分。
- 任何回退都保留 `runs/` 证据，不删除历史 attempt；只清理测试实例与测试 campaign。

## 执行前必须确认

- `/root/.config/ctfy/env` 含 `CTFY_INITIAL_TOKEN`、`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`、`OPENAI_BASE_URL`。
- 平台容量：`team_capacity_exceeded` 表示队伍已有 3 个实例；需等待他人释放或关闭本队闲置实例。
- 时间窗口：初赛 10:00–20:00（北京时间），所有定时器仅在此窗口动作。
