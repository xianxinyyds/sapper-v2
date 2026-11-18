# CTFY Pwn Knowledge Base Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 CTFY 单题 Agent 增加可检索、可执行、可停止的 Pwn 本地到远程、栈/格式化字符串和现代堆工作流，并用非 root 离线已知答案样本验证工具链。

**Architecture:** 保留现有 reason → explore 闭环和 token-overlap KB。`kb042` 作为总入口，`kb065`–`kb067` 承担细分方法；短提示词负责首轮路由，详细方法留在 KB。独立冒烟脚本在临时目录编译最小 C 样本，以同一 payload 验证本地和 loopback transport，不修改 runner 或自动检索。

**Tech Stack:** JSON KB、Markdown prompts、Python 标准库、`unittest`、pwntools、gcc/gdb、checksec、ROPgadget、ropper、pwninit/patchelf、strace、one_gadget、seccomp-tools、Docker/Kali rolling、glibc。

**Spec:** `docs/superpowers/specs/2026-11-18-pwn-kb-expansion-design.md`

## Global Constraints

- 所有改动只留在 `worktree-ctfy-v3-solver`；不创建 Git 提交或推送，不删除既有 untracked handoff。
- 新 KB 固定为 `kb065`–`kb067`；标签匹配 `^[a-z0-9][a-z0-9_-]*$`，关键英文词放在 title、description、tags 和 detail 前部。
- 每个新条目明确前置条件、最低成本证据、对照、负结果、closed-vector；默认每个 intent 不超过 20 次远程连接、并发 1、5 分钟，重连计入预算。
- 真实题目 ELF 仅限静态审查后的独立 stdin/stdout 样本，在副本目录以非 root、`env -i`、超时/资源限制和 strace socket 拒绝运行；不运行安装器、完整项目、未知依赖或要求额外权限的二进制。
- 同 UID 隔离不是强沙箱；`unshare -n` 已实测不可用，不得把它写成前提。strace 使用 `--inject=socket:error=EPERM --inject=socketcall:error=EPERM`，但不得宣称完整隔离。
- 不修改自动 KB 注入、检索评分、runner、答案提交、完成条件、其他类别 KB 或旧 arena/commander/tmux。
- 复用 `sapper-ctfy-web-test:latest`；只有真实冒烟证明工具缺口才修改 Dockerfile，且不加入重型平台或运行时下载。
- 冒烟脚本不匹配套件 discovery 的 `test_*.py`；普通单元测试不依赖 Docker/gcc/gdb/pwntools。指定镜像的最终验收不接受 FAIL 或 SKIP。
- 不连接真实比赛目标做开发验证，不提交二进制、core dump、凭证或 flag，不宣称 solve rate 提升。

## Review Focus

1. **真实 ELF 的 gdb/patch 后运行动态绕过禁网或清洁环境：** 所有动态路径都应遵守同一边界；由 Task 2 的提示词契约和 Task 4 的文档契约覆盖。
2. **中英混合查询被无关 Pwn 条目稀释：** 精确查询应在 `tag="pwn", limit=3` 时把目标条目排在第一；由 Task 1 的检索测试覆盖。
3. **把 `--help` 当成真实工具验证，或 pwninit 修改原文件/尝试下载：** 必须对样本运行工具、固定 libc/loader、比较原 hash；由 Task 3 的容器冒烟覆盖。
4. **本地 exploit 只靠硬编码地址或正例，没有崩溃/正常输入对照：** 栈和格式化字符串均需唯一模式、负对照和唯一成功标记；由 Task 3、Task 4 的已知答案检查覆盖。
5. **旧 glibc 的 SKIP 被汇总成成功，或 double-free 只凭任意非零退出：** 版本函数和退出码由 Task 4 单元测试固定，当前 glibc 2.43 的真实堆/abort 行为由容器冒烟覆盖。

---

### Task 1: Pwn 细分 KB 与检索契约

**Files:**
- Modify: `kb/entries/kb042.json`
- Create: `kb/entries/kb065.json`
- Create: `kb/entries/kb066.json`
- Create: `kb/entries/kb067.json`
- Test: `tests/test_ctfy_run.py:1387-1530`（`CtfyKbContractTests`）

**Interfaces:**
- Consumes: `KnowledgeBase.search(query: str, tag: str | None = None, limit: int = 5) -> list[dict]`。
- Produces: `kb065`（local/remote）、`kb066`（stack/format string）、`kb067`（heap/glibc）；`kb042` 保留 `created_at="2026-10-15T14:14:39Z"` 并路由到三者。

- [ ] **Step 1: 写失败的精确检索测试**

在 `CtfyKbContractTests` 新增 `test_pwn_workflow_entries_are_searchable`：

```python
cases = (
    ("pwn local remote patchelf", "kb065",
     {"pwn", "local-remote", "pwntools", "patchelf", "libc", "protocol"}),
    ("stack ret2libc format string", "kb066",
     {"pwn", "stack", "ret2libc", "rop", "stack-pivot", "srop", "format-string", "fsop"}),
    ("heap tcache SafeLinking", "kb067",
     {"pwn", "heap", "glibc", "tcache", "uaf", "double-free", "safelinking", "pointer-mangling"}),
)
```

对每个 case 调用 `base.search(query, tag="pwn", limit=3)`，断言 `results[0]["id"] == expected` 且 tags 集合完全相等。保留并运行既有 `pwn patchelf` → `kb042` 测试。

- [ ] **Step 2: 写失败的内容与来源契约**

新增 `test_pwn_kb_enforces_bounded_evidence_and_versions`：逐个读取 `kb065`–`kb067`，断言：

- `detail` 同时包含前置/预算/对照/负结果/关闭语义，以及明确的 `20`、`并发 1`、`5 分钟`；命令不得包含 `apt `、`pip `、`curl `、`wget `、`angr`、`ghidra`、`sage`。
- `created_at` 能被 `datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")` 解析，`source` 非空，所有 tags 匹配 `module.VALID_TAGS_RE`。
- `kb065` 文本含 `SHA256`、`loader`、`timeout`、`reconnect`、`env -i`、`socketcall`、`同 UID`；`kb066` 含 `ret2plt`、`ret2libc`、`SROP`、`FSOP`、`Canary`；`kb067` 含 `2.31`、`2.35+`、`SafeLinking`、`pointer guard`、`double-free`。
- `kb042` 的原始 `created_at` 未变，detail 含 `kb065`、`kb066`、`kb067`。

- [ ] **Step 3: 运行测试并确认失败原因**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'pwn_' -v`

Expected: 两个新测试因 `kb065`–`kb067` 不存在或路由缺失而 FAIL；既有无关测试不失败。

- [ ] **Step 4: 实现四个 KB 条目**

按 Spec 的知识库设计填写，不新增其他条目。三个新条目使用实施时真实 UTC 时间和 `docs/superpowers/specs/2026-11-18-pwn-kb-expansion-design.md` 作为可核验 source；命令只列镜像已有工具。`kb042` 只补共同入口和分流，保留原有有效命令与既有时间。

- [ ] **Step 5: 运行 KB 契约回归**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'pwn_' -v`
Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'lightweight_tools_are_discoverable' -v`

Expected: 全部 `OK`，三个精确查询均排第一，既有 `kb042` 查询仍命中。

---

### Task 2: Pwn 首轮提示词路由与执行边界

**Files:**
- Modify: `templates/ctfy/reason.md:18-36`
- Modify: `templates/ctfy/explore.md:12-51,87-108`
- Test: `tests/test_ctfy_run.py:1533-1630`（`CtfyPromptIntegrationTests`）

**Interfaces:**
- Consumes: Task 1 的 `kb065`–`kb067` 和受控 ELF 执行约束。
- Produces: reason 的本地→远程/栈/堆分流；explore 的证据、禁网、同 UID 残余风险和停止要求。其余类别提示词保持不变。

- [ ] **Step 1: 写失败的 Pwn 提示词契约**

新增 `test_pwn_prompt_routes_local_remote_stack_and_heap`：

- 对 `reason` 断言包含 `kb065`、`kb066`、`kb067`、`本地已知答案`、`20 次远程连接`、`并发 1`、`5 分钟`。
- 对 `explore` 断言包含 `真实题目 ELF`、`env -i`、`socketcall`、`同 UID`、`远程重连计入预算`。
- 对二者合并文本断言包含 `sapper kb search`、`closed-vector`，且既有 Web 双入口关键词仍存在。

- [ ] **Step 2: 运行测试并确认缺失路由**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'pwn_prompt' -v`

Expected: FAIL，缺失新 KB id、连接预算或 ELF 执行边界；不修改断言来绕过。

- [ ] **Step 3: 实现简短双提示词路由**

`reason.md` 将 Pwn 两行替换为三条以内：首轮附件/libc/loader/协议核对和 KB 检索；按假设选择 `kb065`–`kb067`；本地证据不足时不直接远程 exploit，并声明 20/1/5 分钟及 closed-vector。

`explore.md` 增加一个 Pwn 小段落：保存脚本、最小输入和本地/远程对照；真实 ELF 动态执行遵守静态审查、副本、`env -i`、资源限制、strace `socket/socketcall` 拒绝；同 UID 不是强沙箱；重连计预算，已证伪链无新证据停止。详细 payload 不复制进提示词。

- [ ] **Step 4: 运行提示词和现有类别回归**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'prompt' -v`

Expected: 全部 `OK`，Web、提交语义、后台任务禁止等既有提示词契约不回归。

---

### Task 3: 工具、栈与 loopback 已知答案冒烟

**Files:**
- Create: `tests/pwn_smoke.py`
- Conditional: `docker/Dockerfile`（仅真实工具缺失时）

**Interfaces:**
- Consumes: 镜像已有工具和 Task 2 的执行边界。
- Produces:
  - `build_fixtures(root: Path) -> dict[str, Path]`，至少返回 `stack`；
  - `check_toolchain(fixtures: dict[str, Path]) -> None`；
  - `check_stack(fixtures: dict[str, Path]) -> bytes`，返回 loopback 复用的 payload；
  - `check_loopback(fixtures: dict[str, Path], payload: bytes) -> None`；
  - `main() -> int`，本任务中任何异常均输出诊断并非零退出。

- [ ] **Step 1: 写最小样本和真实工具检查**

`tests/pwn_smoke.py` 只用标准库管理文件、进程、socket 和版本；pwntools 在栈/loopback 检查内延迟导入，避免普通单元测试导入本文件时依赖它。用 `TemporaryDirectory` 生成源码和产物，所有命令有超时并设置 `TERM=xterm`、空 debuginfod 配置。

栈 C 样本固定为无 Canary、无 PIE、保留 frame pointer 的 32 字节缓冲区 ret2win，成功只打印 `STACK_OK`。`check_toolchain` 必须：

- 对样本运行 `file`、`readelf`、`checksec --file=<stack>`、`ROPgadget`、`ropper`、`patchelf --print-interpreter`，检查返回码和关键 ELF/保护/gadget 输出；不得使用无效的 `checksec --version`。
- 用 gdb batch 对 `cyclic` 崩溃输入保存 RIP/RSP 证据；正常短输入不得出现 `STACK_OK`。
- 在独立子目录用 `pwninit --bin <copy> --libc /usr/lib/x86_64-linux-gnu/libc.so.6 --ld /lib64/ld-linux-x86-64.so.2`，禁止网络，断言原文件 SHA256 不变、生成 patched 副本且 interpreter 为指定 loader。
- 对实际 libc 运行 `one_gadget`，对 `seccomp-tools` 运行真实子命令帮助；记录 glibc、pwntools、pwninit 版本。
- 用 strace socket/socketcall 注入包裹一个尝试 `socket.socket()` 的本地 Python 子进程，只有得到 `PermissionError`/EPERM 才通过。

- [ ] **Step 2: 写栈正负对照与 payload 生成**

用 pwntools `cyclic`/`cyclic_find`、ELF 符号和 ROP gadget 构造 payload；断言崩溃偏移与样本布局一致，gadget/符号来自当前 ELF 而非远程硬编码。先断言正常输入和纯溢出输入都不产生 `STACK_OK`，再执行 ROP payload 并要求唯一成功标记。`check_stack` 返回完全相同的 bytes 给 loopback。

- [ ] **Step 3: 写有界 loopback transport 检查**

用 `socketserver` 绑定 `127.0.0.1:0`，handler 只把自写栈样本的 stdin/stdout/stderr 接到当前连接。最多三次连接：一次无 banner 的有界 timeout、一次成功 payload、一次关闭后的 reconnect 成功 payload。每次显式 pwntools timeout，断言关闭能被观察到、两次成功标记一致、没有第四次连接或外部地址。

- [ ] **Step 4: 在非 root 无网容器中运行**

Run:

```bash
docker run --rm --network none --user agent \
  --entrypoint python3 -v "$PWD/tests:/tests:ro" -w /tmp \
  sapper-ctfy-web-test:latest /tests/pwn_smoke.py
```

Expected: `PASS toolchain`、`PASS stack`、`PASS loopback`，退出 0；glibc 记录为实际版本（当前核对为 2.43）。若 gdb/ptrace 或工具行为失败，保留原始错误并修复具体环境或样本问题，不得改成 SKIP、删除负对照或放宽成功标记。

- [ ] **Step 5: 仅在证实工具缺口时做最小修复**

若失败来自测试样本，修正样本并保持断言；若确为镜像命令缺失，先报告缺失命令，再只修改必要 Dockerfile 声明并重建测试镜像。工具已存在时 Dockerfile 保持零改动。

---

### Task 4: 格式化字符串、现代堆、文档与完整验收

**Files:**
- Modify: `tests/pwn_smoke.py`
- Modify: `docker/CTFY.md:137-180`（新增 Offline Pwn 小节）
- Test: `tests/test_ctfy_run.py:1212-1305,1387-1530`
- Conditional: `docker/Dockerfile`（仅 Task 3 已证明的缺口）

**Interfaces:**
- Consumes: Task 3 的 `build_fixtures()`、结果输出约定和 Docker 运行命令。
- Produces:
  - `check_format_string(fixtures: dict[str, Path]) -> None`；
  - `check_heap(fixtures: dict[str, Path]) -> None`；
  - `supports_safelinking(version: tuple[int, int]) -> bool`；
  - `result_exit_code(failures: int, skips: int) -> int`：`0/0 → 0`，有 failure → `1`，仅 skip → `2`；`main()` 汇总五组结果，FAIL/SKIP 均为非零。

- [ ] **Step 1: 写失败的版本、退出码和文档契约**

在 `tests/test_ctfy_run.py` 新增 `CtfyPwnSmokeContractTests`，固定两个方法名，确保后续 `-k 'pwn_smoke'` 能选中：

- `test_pwn_smoke_version_and_exit_contract`：通过 discovery 的 tests 路径 `import pwn_smoke`，断言 `supports_safelinking((2, 31))` 为 False，`(2, 32)`、`(2, 43)` 为 True；断言 `result_exit_code(0, 0) == 0`、`result_exit_code(0, 1) == 2`、`result_exit_code(1, 0) == 1`、`result_exit_code(1, 1) == 1`。
- `test_pwn_smoke_documentation_contract`：断言 `docker/CTFY.md` 含 `Offline Pwn`、`tests/pwn_smoke.py`、`--network none`、`--user agent`、`env -i`、`socketcall` 和同 UID 非强沙箱说明。
- 扩充既有轻量工具声明测试，至少固定 `checksec`、`gdb`、`strace`、`patchelf`、`gcc`、`ropper`、pwntools、ROPgadget、one_gadget、seccomp-tools 和 pwninit 的现有声明。

- [ ] **Step 2: 运行测试并确认失败**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'pwn_smoke' -v`

Expected: 新函数缺失且 Pwn 文档小节不存在，测试 FAIL。

- [ ] **Step 3: 实现格式化字符串已知答案**

扩展 `build_fixtures()` 生成无 PIE 的最小格式串样本和专用全局 `target`。`check_format_string` 必须：

- 普通 `HELLO` 原样回显且不出现 `FMT_OK`；带唯一 marker 的 `%p` 输入能定位参数 offset。
- 用 `fmtstr_payload`、当前 ELF 的 `target` 地址和明确写宽度生成 payload；不得硬编码远程地址。
- 将 target 改为 `0x41424344` 后得到唯一 `FMT_OK`，同时保存 offset、地址和正负对照输出。

- [ ] **Step 4: 实现当前 glibc 堆已知答案与严格汇总**

扩展 `build_fixtures()` 生成 `-O0` 编译的堆样本，使用 16 字节对齐的专用 `.bss` target。`check_heap` 分别验证：

- 正常分配/释放返回不同且对齐的 chunk；
- 当前 glibc 2.43 下，释放后的 encoded next 等于 `(storage_position >> 12) ^ next`；
- 通过已证明的 UAF 写覆盖 encoded next 后，两次 malloc 第二次精确返回 target 并出现 `HEAP_OK`；
- 独立进程的 double-free 以 SIGABRT 结束且 stderr 含实际 `double free` 诊断，不能只看任意非零退出。

`supports_safelinking` 仅在版本低于 2.32 时返回 False；此分支产生 SKIP 和总体 `2`，不伪造通过。`main()` 依次汇总 toolchain/stack/loopback/format-string/heap，只有五组全 PASS 才返回 0。

- [ ] **Step 5: 运行完整容器冒烟**

Run: 使用 Task 3 Step 4 的同一条 Docker 命令。

Expected: 五组全部 `PASS`、无 SKIP/FAIL、退出 0；输出记录实际镜像/glibc/pwntools 版本。若当前 2.43 引入新完整性检查，按真实 abort 调整最小样本或 KB 版本说明，不得绕过检查来制造 PASS。

- [ ] **Step 6: 编写 Offline Pwn 文档**

在 `docker/CTFY.md` 说明工具用途、patchelf/pwninit 只作用于副本且禁止下载、真实独立 ELF 的受控执行和同 UID 残余风险，并原样给出 Task 3 的 Docker 冒烟命令。注明本地样本不验证真实目标、旧 glibc 或 solve rate；若 Dockerfile 未改，明确复用 `sapper-ctfy-web-test:latest`。

- [ ] **Step 7: 运行契约、完整回归和静态检查**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'pwn' -v`
Run: `python3 -m unittest discover -s tests -p 'test_*.py' -v`
Run: `bash -n docker/entrypoint-ctfy.sh`
Run: `python3 -m compileall -q lib tests`
Run: `git diff --check`
Run: `git status --short`

Expected: 所有测试 `OK`，静态命令退出 0；状态只包含本计划文件、设计/handoff 和预期 Pwn 改动，无二进制/core/临时证据，无提交或推送。

- [ ] **Step 8: 如实记录验证边界**

最终报告列出实际测试数、镜像 ID、glibc/pwntools 版本、五组冒烟结果、Dockerfile 是否变化，以及未做真实题目 A/B 和未实测其他 glibc 版本。任何未执行或失败项目必须明确写出，不得将局部成功描述为完整交付。
