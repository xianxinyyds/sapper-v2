# CTFY Crypto Knowledge Base Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 CTFY 单题 Agent 增加可检索、可验证的 RSA/ECC/格、分组/流密码与协议、PRNG/弱随机工作流，并以非 root 无网已知答案 oracle 验证恢复和预测。

**Architecture:** 保留现有 reason → explore 闭环与 token-overlap KB。`kb041` 作为总入口，`kb071`–`kb073` 承担细分方法；提示词按密码构造路由。独立 `crypto_smoke.py` 生成本地小参数和 oracle，先证明恢复/预测再验证工具结果，不修改 runner 或自动检索。

**Tech Stack:** JSON KB、Markdown prompts、Python 标准库、`unittest`、pycryptodome、ecdsa、fpylll、sympy/gmpy2/z3、RsaCtfTool、OpenSSL、xortool、Docker/Kali rolling。

**Spec:** `docs/superpowers/specs/2026-11-18-crypto-kb-expansion-design.md`

## Global Constraints

- 所有改动只留在 `worktree-ctfy-v3-solver`；不创建提交或推送，不修改既有 Pwn/Reverse 成果或其他类别 KB。
- 新 KB 固定为 `kb071`–`kb073`；合法 tags、前置英文关键词、真实 UTC 和可核验 source。
- 每条新 KB 明确前置、最低成本 oracle、对照、负结果、closed-vector、观测数和预算：侦查默认 20 请求、并发 1、5 分钟；恢复攻击先计算请求公式，预算不足即关闭。
- 每个恢复先在本地已知答案上通过，再做独立加解密/签名/预测往返验证；候选必须唯一，hex/raw bytes 长度一致，逐字节 checkpoint 不覆盖已验证前缀。
- RsaCtfTool 只运行一个明确本地 attack，禁止 `all`、factordb/sendtofdb；不把工具候选或退出码单独当答案。
- 不加入 Sage/重型格平台/在线服务，不把 nonce 密文 XOR 直接当 GHASH 认证绕过，不把 `/dev/urandom` 当可预测 MT。
- 默认单元测试不依赖 Docker/第三方包；`crypto_smoke.py` 不匹配 `test_*.py`，当前镜像验收不接受 FAIL/SKIP。
- 不连接真实目标，不提交真实密钥/flag/凭证，不宣称 solve rate 提升。

## Review Focus

1. **数学候选未独立验证或 RsaCtfTool 只凭退出码成功：** Task 3 public-key 必须独立解密/签名验证。
2. **hex/raw 长度错位、全部候选命中或恢复前缀被覆盖：** Task 3 modes-protocol 的唯一候选和 checkpoint 覆盖。
3. **理论可解但服务请求预算不足仍继续：** Task 1/2 契约和 Task 3 有界 oracle/负对照覆盖。
4. **MT 输出不足、位宽/字节序错误或 CSPRNG 被误当 MT：** Task 1 内容契约和 Task 3 PRNG 观测记录/预测覆盖。
5. **nonce 复用的机密性证据被误称 GHASH 认证绕过：** Task 1 文本闸门和 Task 3 CTR/GHASH 边界检查覆盖。

---

### Task 1: Crypto 细分 KB 与检索/oracle 契约

**Files:**
- Modify: `kb/entries/kb041.json`
- Create: `kb/entries/kb071.json`, `kb072.json`, `kb073.json`
- Test: `tests/test_ctfy_run.py`（`CtfyKbContractTests`）

**Interfaces:**
- Consumes: `KnowledgeBase.search(query, tag=None, limit=5)` 和 spec。
- Produces: `kb071` public-key、`kb072` modes/protocol、`kb073` PRNG；`kb041` 保留 `created_at="2026-10-15T14:14:39Z"` 并路由三者。

- [ ] **Step 1: 写失败的精确检索测试**

新增 `test_crypto_workflow_entries_are_searchable`，对每个 case 使用 `base.search(query, tag="crypto", limit=3)`，断言第一结果 ID 和完整 tags：

```python
(
  ("rsa wiener coppersmith", "kb071",
   {"crypto", "rsa", "ecc", "lattice", "wiener", "coppersmith", "nonce", "fpylll"}),
  ("cbc padding oracle ghash", "kb072",
   {"crypto", "ecb", "cbc", "padding-oracle", "ctr", "ghash", "dh", "protocol"}),
  ("mt19937 lcg xorshift", "kb073",
   {"crypto", "prng", "mt19937", "lcg", "xorshift", "seed", "untemper"}),
)
```

保留 `rsa fpylll` → `kb041` 既有回归。

- [ ] **Step 2: 写失败的内容、预算与来源契约**

新增 `test_crypto_kb_enforces_oracles_budgets_and_negative_controls`：

- 三条 detail 均含前置、本地已知答案 oracle、对照、负结果、关闭、观测数、侦查 20 请求、并发 1、5 分钟和恢复请求公式；tags 合法、UTC/source 有效。
- `kb071` 含 Boneh-Durfee、Coppersmith、部分私钥泄露、广播、invalid-curve、small-subgroup、nonce 复用和独立验证。
- `kb072` 含 ECB、CBC bit-flip、padding oracle、CTR、CFB/OFB、GHASH、DH、KDF、协议状态机、唯一候选、hex/raw bytes 和预算不足关闭。
- `kb073` 含 624、32-bit、untemper、部分输出、LCG、xorshift、时间/PID 窗口、`/dev/urandom` 禁止预测。
- `kb041` 原时间不变且 detail 含 `kb071`–`kb073`；所有命令无 `attack all`、factordb/sendtofdb 或运行时安装。

- [ ] **Step 3: 运行 RED**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'crypto_' -v`
Expected: 新条目/路由缺失导致 FAIL；无关测试不失败。

- [ ] **Step 4: 实现四个 KB 条目**

按 spec 填写，使用真实 UTC 和 `docs/superpowers/specs/2026-11-18-crypto-kb-expansion-design.md` source；只使用已有离线工具。`kb041` 仅加入口路由，保留原有效内容与时间。

- [ ] **Step 5: 运行 GREEN 和既有 Crypto 回归**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'crypto_' -v`
Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'lightweight_tools_are_discoverable' -v`
Expected: 全部 OK，三个精确查询第一命中。

---

### Task 2: Crypto 提示词构造路由与预算

**Files:**
- Modify: `templates/ctfy/reason.md`
- Modify: `templates/ctfy/explore.md`
- Test: `tests/test_ctfy_run.py`（`CtfyPromptIntegrationTests`）

**Interfaces:**
- Consumes: `kb071`–`kb073` 和 oracle/预算约束。
- Produces: reason 的 public-key/modes-protocol/PRNG 路由；explore 的恢复证据与负结果规则。

- [ ] **Step 1: 写失败的区域级提示词契约**

新增 `test_crypto_prompt_routes_construction_oracles_and_budgets`：

- 以 `- Crypto:` 到 `- Reverse:` 提取 reason_crypto；以 `For Crypto,` 到 `For Web,` 提取 explore_crypto，避免共享关键词假阳性，并断言分隔符存在。
- reason 必须含 `sapper kb search`、`kb071`、`kb072`、`kb073`、本地已知答案 oracle、请求公式、并发 1、5 分钟、预算不足关闭和 closed-vector。
- explore 必须含原始 bytes、候选唯一、独立恢复/预测验证、逐字节 checkpoint、禁止 `attack all`/factordb/sendtofdb、禁止 `/dev/urandom` 预测，以及负结果覆盖和预算。
- 既有 Pwn/Reverse/Web 区域契约保持不变。

- [ ] **Step 2: 运行 RED**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'crypto_prompt' -v`
Expected: 新构造路由和预算/证据规则缺失导致 FAIL。

- [ ] **Step 3: 实现简短提示词**

reason 用至多三条 Crypto bullet：识别构造和本地 oracle；按 public-key/modes-protocol/PRNG 选择 KB；先计算请求公式并在预算不足时关闭。explore 在 `For Web,` 前插入一个 `For Crypto,` 段落，避免改变 Reverse/Pwn 区域边界；保存独立验证和负结果，不复制攻击模板。

- [ ] **Step 4: 运行 GREEN 和全部 prompt 回归**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'prompt' -v`
Expected: 全部 OK。

---

### Task 3: 完整 Crypto 已知答案 smoke、文档与总验收

**Files:**
- Create: `tests/crypto_smoke.py`
- Modify: `docker/CTFY.md`
- Test: `tests/test_ctfy_run.py`（新增 `CtfyCryptoSmokeContractTests` 和工具声明）
- Conditional: `docker/Dockerfile`（仅实测缺失工具）

**Interfaces:**
- Produces:
  - `check_public_key(root: Path) -> None`；
  - `check_modes_protocol(root: Path) -> None`；
  - `check_prng(root: Path) -> None`；
  - `result_exit_code(failures: int, skips: int) -> int`：0/0→0，有 failure→1，仅 skip→2；
  - `main() -> int`，三组全部 PASS 才返回 0。

- [ ] **Step 1: 写失败的文档和 smoke 接口契约**

新增 `test_crypto_smoke_interface_and_exit_contract`：导入 `crypto_smoke`，用 `inspect.signature` 固定四个公开接口参数，并断言 `result_exit_code(0, 0) == 0`、`(0, 1) == 2`、`(1, 0) == 1`、`(1, 1) == 1`。第三方密码库必须在对应检查组内延迟导入，默认 unit-test discovery 不依赖这些包。

新增 `test_crypto_smoke_documentation_contract`：要求 `Offline Crypto`、`tests/crypto_smoke.py`、`--network none`、`--user agent`、RsaCtfTool 单 attack、禁止 all/factordb/sendtofdb、独立验证和限制说明。扩充现有声明，固定 pycryptodome、ecdsa、fpylll、sympy、RsaCtfTool、xortool 和 OpenSSL。运行 `-k 'crypto_smoke'` 确认模块/文档 RED。

- [ ] **Step 2: 实现 public-key 已知答案**

`check_public_key` 必须：

- 验证 RSA 小指数精确开方、共模、共享因子；生成满足 Wiener 的小 d 公钥，先检查本地前置，再仅运行 `RsaCtfTool --attack wiener`，解析恢复私钥并独立解密已知密文。
- 用 fpylll 验证小格已知短向量；用 ecdsa 同 nonce 双签名恢复私钥并完成签名验证。
- 曲线/阶/生成元检查拒绝明确非法输入；保存参数、工具输出和独立验证，不依赖 RsaCtfTool 退出码本身。

- [ ] **Step 3: 实现 modes-protocol 已知答案**

`check_modes_protocol` 必须：

- 本地 AES ECB secret-suffix 逐字节恢复，唯一候选和 checkpoint；CBC bit-flip 与可区分 padding oracle 恢复；CTR nonce 复用恢复已知明文。
- 小规模 DH 子群/离散日志与非法参数负对照；记录所有 oracle 查询数，超预算即失败。
- 非确定性 ECB 和统一 CBC 错误负对照必须关闭；GHASH 仅验证已证明边界，不把 CTR 密文 XOR 称为认证绕过。

- [ ] **Step 4: 实现 PRNG 已知答案**

`check_prng` 必须：

- 624 个 MT19937 32-bit 输出 untemper、状态对齐并预测后续序列；记录位宽/字节序/观测数。
- 连续 LCG 输出恢复参数并预测；xorshift 状态回放；有限时间/PID 窗口候选验证及无命中负对照。
- 不调用 OS CSPRNG 作为可预测对象，部分输出不得冒充完整 MT 状态。

- [ ] **Step 5: 编写 Offline Crypto 文档并运行 smoke**

文档列出实际库、单 attack、oracle/预算和验证限制，给出：

```bash
docker run --rm --network none --user agent \
  --entrypoint python3 -v "$PWD/tests:/tests:ro" -w /tmp \
  sapper-ctfy-web-test:latest /tests/crypto_smoke.py
```

Expected: `PASS public-key`、`PASS modes-protocol`、`PASS prng`，无 FAIL/SKIP，退出 0。若库/攻击行为失败，修复具体参数或实现，不得删除负对照或伪造通过。

- [ ] **Step 6: 完整验证和边界报告**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'crypto' -v`
Run: `python3 -m unittest discover -s tests -p 'test_*.py' -v`
Run: `bash -n docker/entrypoint-ctfy.sh`
Run: `python3 -m compileall -q lib tests`
Run: `git diff --check`
Run: `git status --short`

Expected: 全部 OK/exit 0；无生成密钥/core/临时证据。报告实际测试数、镜像 ID、库版本、三组 smoke 结果、Dockerfile 是否变化，以及未做真实题 A/B 和其他攻击/平台验证。
