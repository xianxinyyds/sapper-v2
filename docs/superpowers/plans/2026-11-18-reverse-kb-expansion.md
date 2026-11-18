# CTFY Reverse Knowledge Base Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 CTFY 单题 Agent 增加可检索、可复核的 Reverse 静态证据、动态校验和壳/混淆/自研 VM 工作流，并以非 root 无网已知答案样本验证工具链。

**Architecture:** 保留现有 reason → explore 闭环与 token-overlap KB。`kb057` 作为总入口，`kb068`–`kb070` 承担细分方法；短提示词负责静态→动态/VM 路由。独立 `reverse_smoke.py` 在临时目录生成最小程序，验证真实偏移、动态行为、UPX 副本和独立 VM 解释器，不修改 runner 或自动检索。

**Tech Stack:** JSON KB、Markdown prompts、Python 标准库、`unittest`、gcc/binutils、radare2、gdb、strace/ltrace、UPX、YARA、Docker/Kali rolling。

**Spec:** `docs/superpowers/specs/2026-11-18-reverse-kb-expansion-design.md`

## Global Constraints

- 所有改动只留在 `worktree-ctfy-v3-solver`；不创建提交或推送，不修改既有 Pwn 成果或其他类别 KB。
- 新 KB 固定为 `kb068`–`kb070`；标签匹配 `^[a-z0-9][a-z0-9_-]*$`，英文关键词位于 title/description/tags/detail 前部。
- 每条新 KB 明确前置、最低成本证据、对照、负结果、closed-vector、有限本地轮次/时间，以及默认 20 次远程请求、并发 1、5 分钟预算。
- 真实 ELF 普通 solver 动态执行必须是静态审查后的独立副本，非 root、`env -i`、rlimit、timeout、`strace -f` 双 socket 注入；同 UID 不是强沙箱，无法满足即 blocker。
- live GDB/ltrace 仅限自写可信样本，或外部已验证的无网、无凭证、非 root、一次性容器；不得假定 solver 内可启动 Docker，不得为 ptrace 工具移除禁网。
- UPX/patch 只作用于副本并保留原 hash；不加入 Ghidra/angr/Frida/QEMU/Android/Sage，不运行时下载。
- 默认单元测试不依赖 Docker/gcc/gdb/radare2；`reverse_smoke.py` 不匹配 `test_*.py` discovery，当前镜像验收不接受 FAIL 或 SKIP。
- 不连接真实目标，不提交二进制/core/flag/凭证，不宣称 solve rate 提升。

## Review Focus

1. **VA 被当作文件偏移或 stripped/间接跳转被当成已覆盖：** Task 1 内容契约与 Task 3 static 的 VA↔offset 实算覆盖。
2. **ltrace/gdb 通过 ptrace 绕过真实 ELF 禁网：** Task 1/2 区域契约和 Task 3 文档/控制包装验证覆盖。
3. **strings 的 flag 形状或单次异常被当作答案：** Task 3 static/dynamic 必须有独立解码与正负行为对照。
4. **UPX 覆盖原件或解包行为不同：** Task 3 pack-vm 检查原 hash、独立输出路径和同一正确输入。
5. **VM 解释器硬编码 `VM_OK` 而没有执行 opcode：** Task 3 要求真实 bytecode 偏移、逐 opcode 状态变化、未知 opcode 负对照及 C/Python 输出一致。

---

### Task 1: Reverse 细分 KB 与检索/证据契约

**Files:**
- Modify: `kb/entries/kb057.json`
- Create: `kb/entries/kb068.json`, `kb069.json`, `kb070.json`
- Test: `tests/test_ctfy_run.py`（`CtfyKbContractTests`）

**Interfaces:**
- Consumes: `KnowledgeBase.search(query, tag=None, limit=5)` 和已批准 spec。
- Produces: `kb068` static、`kb069` dynamic、`kb070` pack/VM；`kb057` 保留 `created_at="2026-11-11T00:00:00Z"` 并路由三者。

- [ ] **Step 1: 写失败的精确检索测试**

新增 `test_reverse_workflow_entries_are_searchable`，对每个 case 使用 `base.search(query, tag="reverse", limit=3)`，断言第一结果 ID 和完整 tags：

```python
(
  ("reverse static offsets xrefs", "kb068",
   {"reverse", "static-analysis", "elf", "offsets", "xrefs", "objdump", "radare2"}),
  ("dynamic strace ltrace validation", "kb069",
   {"reverse", "dynamic-analysis", "gdb", "strace", "ltrace", "anti-debug", "validation"}),
  ("custom VM opcode dispatcher", "kb070",
   {"reverse", "packing", "upx", "obfuscation", "custom-vm", "opcode", "dispatcher"}),
)
```

保留 `reverse radare2` → `kb057` 既有回归。

- [ ] **Step 2: 写失败的内容、安全和来源契约**

新增 `test_reverse_kb_enforces_evidence_budget_and_execution_boundaries`：

- 三条 detail 均含前置、最低成本证据、对照、负结果、关闭、有限本地轮次/时间、20 次远程请求、并发 1、5 分钟；tags 合法、UTC 可解析、source 非空。
- `kb068` 含 `strings -a -t x`、`readelf`、`nm`、`objdump`、`radare2`、VA、文件偏移、stripped、间接跳转。
- `kb069` 含 `env -i`、`prlimit`、`timeout`、`strace -f`、双 socket 注入、`ptrace`、外部无网无凭证一次性容器和 blocker；动态命令本身必须带完整控制，ltrace/gdb 不得用于普通 solver 的真实 ELF 无网绕过。
- `kb070` 含 `upx -d -o`、原 hash、opcode、dispatcher、独立解释器、未知 opcode、z3/sympy 可用性检查。
- `kb057` 原时间不变且 detail 含 `kb068`–`kb070`。

- [ ] **Step 3: 运行 RED**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'reverse_' -v`
Expected: 新条目不存在/路由缺失导致 FAIL；无关测试不失败。

- [ ] **Step 4: 实现四个 KB 条目**

按 spec 填写，使用真实 UTC 和 `docs/superpowers/specs/2026-11-18-reverse-kb-expansion-design.md` source；只使用镜像已有工具。`kb057` 仅加入口路由，保留既有有效内容与时间。

- [ ] **Step 5: 运行 GREEN 和既有 Reverse 回归**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'reverse_' -v`
Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'lightweight_tools_are_discoverable' -v`
Expected: 全部 OK，三个精确查询第一命中。

---

### Task 2: Reverse 提示词路由与 tracer 边界

**Files:**
- Modify: `templates/ctfy/reason.md`
- Modify: `templates/ctfy/explore.md`
- Test: `tests/test_ctfy_run.py`（`CtfyPromptIntegrationTests`）

**Interfaces:**
- Consumes: `kb068`–`kb070` 和动态执行约束。
- Produces: reason 的静态→动态/VM 路由；explore 的证据、tracer 和 fail-closed 规则。

- [ ] **Step 1: 写失败的区域级提示词契约**

新增 `test_reverse_prompt_routes_static_dynamic_and_vm`：

- 以 `- Reverse:` 到 `- Misc:` 提取 reason_reverse，以 `For Reverse,` 到下一个顶层段落提取 explore_reverse，避免共享关键词造成假阳性。
- reason 必须含 `sapper kb search`、`kb068`、`kb069`、`kb070`、禁止无静态目标全程序单步、20 次远程请求、并发 1、5 分钟、closed-vector。
- explore 必须含真实文件偏移、反汇编、trace、opcode、独立解释器、`strace -f`、双 socket 注入、ltrace/gdb 的可信样本或外部无网无凭证容器限制，以及无法满足时的 blocker。
- 既有 Pwn/Web 区域契约保持不变。

- [ ] **Step 2: 运行 RED**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'reverse_prompt' -v`
Expected: 新路由和 tracer 边界缺失导致 FAIL。

- [ ] **Step 3: 实现简短提示词**

reason 用至多三条 Reverse bullet：首轮静态 kb068；有目标函数/协议后 kb069；有壳/分派器/VM 证据后 kb070，附预算和关闭条件。explore 用一个 Reverse 段落保存证据并落实完整 tracer/fail-closed 规则，不复制详细 payload。

- [ ] **Step 4: 运行 GREEN 和全部 prompt 回归**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'prompt' -v`
Expected: 全部 OK。

---

### Task 3: 完整 Reverse 已知答案 smoke、文档与总验收

**Files:**
- Create: `tests/reverse_smoke.py`
- Modify: `docker/CTFY.md`
- Test: `tests/test_ctfy_run.py`（新增 `CtfyReverseSmokeContractTests` 和工具声明）
- Conditional: `docker/Dockerfile`（仅实测缺失工具）

**Interfaces:**
- Produces:
  - `build_fixtures(root: Path) -> dict[str, Path]`；
  - `check_static(fixtures: dict[str, Path]) -> None`；
  - `check_dynamic(fixtures: dict[str, Path]) -> None`；
  - `check_pack_vm(fixtures: dict[str, Path]) -> None`；
  - `main() -> int`，三组全部 PASS 才返回 0，FAIL/SKIP 非零。

- [ ] **Step 1: 写失败的文档和 smoke 接口契约**

新增 `test_reverse_smoke_documentation_contract`：要求 `Offline Reverse`、`tests/reverse_smoke.py`、`--network none`、`--user agent`、`strace -f`、双 socket 注入、ptrace 冲突、ltrace/gdb 外部边界、UPX 副本和验证限制。扩充既有工具声明，固定 binutils 命令、radare2、UPX、YARA、gdb/strace/ltrace 的现有安装路径。运行 `-k 'reverse_smoke'` 确认文档 RED。

- [ ] **Step 2: 实现 static 已知答案**

`build_fixtures` 生成带符号、无 PIE 的校验/解码 C ELF 和正确/错误/边界输入。`check_static` 必须：

- 运行 file/hash、`strings -a -t x`、readelf/nm/objdump、radare2 xrefs，保存原始输出。
- 用 ELF 节区把目标 VA 映射为真实文件偏移，提取编码常量并由独立 Python 解码；正确输入产生唯一 `REV_OK`，错误输入不产生，strings 本身不作为通过依据。
- 保存正常/失败分支反汇编、VA/offset 映射和未知项；间接跳转或 stripped 不得被描述为已覆盖。

- [ ] **Step 3: 实现 dynamic 已知答案**

`check_dynamic` 对可信 fixture 使用正确/错误/边界输入，验证唯一标记、退出码、strace/ltrace 目标调用差异；在无网容器内用 gdb batch 在目标函数记录寄存器/内存。另以自写 socket 尝试样本验证完整 `env -i` + prlimit + timeout + `strace -f` 双注入得到 EPERM，不在 live GDB 外叠加 strace。所有运行有超时和证据文件。

- [ ] **Step 4: 实现 UPX 与独立 VM 已知答案**

`check_pack_vm` 必须：

- 仅在副本上 `upx -9 -o packed ...` 和 `upx -d -o unpacked ...`，原 hash 不变；保存节区/导入/熵差异，以同一正确输入验证 unpacked 的唯一 `REV_OK`。
- 构造小型 C VM，定位 bytecode 符号 VA→文件偏移和 `vm_run`/handlers 反汇编；独立 Python 解释器逐 opcode 更新栈/寄存器并输出 `VM_OK`，与 C VM 完全一致。
- 未知 opcode 必须抛出明确错误，不能把 `VM_OK` 硬编码为解释器结果。

- [ ] **Step 5: 编写 Offline Reverse 文档并运行 smoke**

文档列出实际工具、静态优先、tracer 边界、UPX 副本、VM 证据和验证限制，给出：

```bash
docker run --rm --network none --user agent \
  --entrypoint python3 -v "$PWD/tests:/tests:ro" -w /tmp \
  sapper-ctfy-web-test:latest /tests/reverse_smoke.py
```

Expected: `PASS static`、`PASS dynamic`、`PASS pack-vm`，无 FAIL/SKIP，退出 0。若工具或 UPX 行为失败，修复具体样本/环境，不得删除对照或伪造通过。

- [ ] **Step 6: 完整验证和边界报告**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'reverse' -v`
Run: `python3 -m unittest discover -s tests -p 'test_*.py' -v`
Run: `bash -n docker/entrypoint-ctfy.sh`
Run: `python3 -m compileall -q lib tests`
Run: `git diff --check`
Run: `git status --short`

Expected: 全部 OK/exit 0；无二进制/core/临时证据。报告实际测试数、镜像 ID、radare2/UPX/ltrace/YARA 版本、三组 smoke 结果、Dockerfile 是否变化，以及未做真实题 A/B 和其他平台验证。
