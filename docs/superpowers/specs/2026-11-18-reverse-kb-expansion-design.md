# CTFY Reverse 知识库与本地验证能力扩充设计

## 意图与既定方向

在 Pwn 扩充完成后，继续增强同一 CTFY 单题 reason → explore 闭环的 Reverse 能力。当前 `kb057` 只有静态优先总览，提示词也只有一行工具清单，缺少可独立检索的静态证据、动态校验和壳/混淆/自研 VM 工作流。

用户已明确要求：不再次询问方案，按推荐的完整交付单元自动实施，顺序为 Pwn → Reverse → Crypto。本设计因此沿用已验证的 B 方案：KB、提示词、离线工具文档、契约测试和真实已知答案验证一起交付；不创建提交或推送。

## 方案比较

### A：只扩 KB

新增细分条目但保留一行提示词，改动最小；仍可能因模型不主动检索而漏读，也没有真实工具与样本验证。

### B：完整 Reverse 交付单元（选定）

新增三个条目并修订总入口，增加首轮提示词路由、`docker/CTFY.md` 文档和 `reverse_smoke.py`。复用现有 binutils、radare2、gdb、strace/ltrace、UPX、YARA 和 Python，不引入重型平台。

### C：B 加自动 KB 注入

可进一步降低漏读，但会改变 runner/提示词组装，超出本轮知识扩充范围。

## 目标

1. 静态分析先建立文件 hash、格式、真实文件偏移、节区、符号、重定位、函数边界、交叉引用和反汇编证据，不把 strings 猜测当结论。
2. 动态分析先定位比较、解码或密钥生成函数，再用有界输入输出、strace/ltrace 或 gdb 验证，避免全程序单步。
3. 壳和混淆只在副本上处理，比较节区、导入、熵与行为；自研 VM 先恢复 opcode、栈/寄存器语义和分派器，再写独立解码器。
4. 新条目可被常见中英混合查询检索，提示词能按静态/动态/VM 证据路由。
5. 所有新增命令与镜像实际能力一致，并以非 root `agent`、`--network none` 完成已知答案验证。
6. 保留现有提交、图、生命周期、作用域、提示注入和其他类别语义。

## 非目标与反目标

- 不加入 Ghidra、angr、Frida、QEMU/multi-arch、Android、Sage 或其他重型平台，不改自动 KB 注入或检索评分。
- 不直接执行完整不可信项目、安装器或脚本入口；不为动态分析提供无边界命令。
- 不把 flag 形状字符串、扫描器命中、单次寄存器状态或壳名称当作答案。
- 不默认全程序单步、全量 YARA 规则扫描或无界熵/暴力路径搜索。
- 不用 toy VM 名称替代 opcode 语义证据，不声称已知答案样本证明真实题 solve rate。
- 不重写 Pwn/Web/Crypto/Misc 条目或修复无关代码。

## 约束与动态执行边界

- 真实题目独立 ELF 只有在静态审查后、确认无需安装器/额外权限/外部依赖时才允许本地动态运行；使用独立副本和输入、非 root、`env -i`、CPU/地址空间/文件大小限制、timeout 以及 `strace -f` 的 `socket`/`socketcall` EPERM 注入。
- 同 UID 不是强沙箱，仍可能访问同 UID 文件或进程；无法满足上述条件时记录 blocker 并停止动态执行。
- 已实测 live GDB 与 `strace -f` 因 `ptrace` 冲突不能同时使用；ltrace 同样依赖 ptrace，不能借工具切换规避禁网。普通 solver 中真实题目 ELF 的“受控直接执行”特指保留完整 strace 注入的直接运行，不是无包装执行。真实 ELF 的 live GDB/ltrace 只能在外部已验证的 `--network none`、无凭证、非 root、一次性容器且保留其他资源限制时进行；不得假定 solver 容器内能再启动 Docker。该环境不可用或无法验证时记录 blocker，或转为 Agent 审查后自写最小样本上的 gdb/ltrace。自写可信 smoke fixture 可在 `--network none` 容器中直接使用 gdb/ltrace。
- 远程行为验证只针对当前任务授权 endpoint，默认每个 intent 最多 20 次请求、并发 1、5 分钟；本地每个假设也必须有有限输入、工具轮次和 wall-clock，重试需要新证据。
- 所有动态样本和补丁只作用于工作目录副本，原始附件 hash 不变；UPX 不解到原路径，不运行时下载。
- KB 使用中文说明和前置英文关键词，标签匹配合法格式；新条目使用真实 UTC 时间和可核验 source。
- 默认完整测试不依赖 Docker/gcc/gdb/radare2；独立 smoke 文件名不匹配 `test_*.py`，由文档命令显式运行。
- 不提交二进制、core dump、真实 flag 或凭证。

## KB 设计

### 修订 `kb057`：Reverse 总入口

保留现有静态优先和工具概览以及原 `created_at`，新增：

- 静态画像、真实偏移、xrefs/调用图 → `kb068`。
- 动态行为、比较/解码/密钥生成、反调试差异 → `kb069`。
- UPX/壳、混淆、自研 VM → `kb070`。

### 新增 `kb068`：Reverse 静态证据与校验逻辑定位

关键词：`static-analysis`、`elf`、`offsets`、`xrefs`、`objdump`、`radare2`、`strings`。

1. 前置与预算：清点附件、hash、权限、格式和可见源码；先静态后动态，声明有限工具轮次/时间及远程预算。
2. 最低成本证据链：`file` → `strings -a -t x` → `readelf -h/-S/-s/-r` → `nm -an` → `objdump -d`；需要 xrefs 时用 radare2，记录 VA、文件偏移和节区映射，不能混淆。
3. 校验逻辑：从输入、比较、常量和成功/失败分支建立调用图；区分函数边界证据、字符串猜测和未证实假设。
4. 对照与负结果：保存正常/异常输入对应的静态分支、精确反汇编和偏移；未覆盖函数/间接跳转/ stripped 区域明确标未知。
5. closed-vector：前提证伪、达到预算或两轮静态复核无新证据即停止，不直接转无界动态单步。

### 新增 `kb069`：Reverse 动态行为与校验函数验证

关键词：`dynamic-analysis`、`gdb`、`strace`、`ltrace`、`anti-debug`、`self-modifying`、`validation`。

1. 前置：先从 kb068 或源码定位比较、解码、密钥生成和可疑反调试函数；记录真实运行时与输入协议。
2. 最低成本对照：正常、错误、边界和空输入，保存退出码、stdout/stderr、syscall/library call 和分支差异；一次只改一个变量。
3. 工具分流：strace 用于受控直接执行与 syscall 证据。ltrace/gdb batch 仅在自写可信样本，或外部已验证的无网、无凭证、非 root、一次性容器内使用；保持其他资源限制并设置证据驱动断点、寄存器和内存观察。不得为 ltrace/gdb 移除禁网控制；ptrace 冲突或外部环境不可用时走 strace 受控直接执行、静态分析或自写最小样本，否则记录 blocker。
4. 反调试/自修改：先证明 tracer/timing/页面写入差异，再选择补丁或插桩；只 patch 副本并保留前后 hash，不假定检测到调试器。
5. 负结果与停止：记录输入、断点/函数、地址、trace 和预算；未出现目标函数、trace 不可达或两组对照无新增证据即关闭。

### 新增 `kb070`：Reverse 壳、混淆与自研 VM

关键词：`packing`、`upx`、`obfuscation`、`custom-vm`、`opcode`、`dispatcher`、`flattening`、`z3`。

1. 壳前置：记录原始 hash、节区、导入、熵和 UPX 指纹；UPX 只解到工作副本，比较解包前后行为，不把 UPX 等同于所有壳。
2. 混淆：XOR/RC4/自定义编码先恢复算法、密钥来源和输入输出对照；控制流平坦化先找状态变量和分派块，不做无依据全量路径爆破。
3. 自研 VM：恢复 bytecode 文件偏移、opcode 表、分派器、栈/寄存器语义和每个 handler 的输入输出；先写独立最小解释器并对照真实样本，再讨论补丁或约束求解。
4. z3/sympy 只在 `command -v`/import 可用且小规模路径约束已证实时使用；不运行时安装，不把不可满足/超时当成答案。
5. 对照与停止：保存原始/解包样本、opcode 映射、独立解释器输出和负结果； opcode 语义未证明、行为不一致或达到预算即关闭。

## 提示词与文档

### `templates/ctfy/reason.md`

Reverse 首轮运行 `sapper kb search`：默认先按 `kb068` 建静态证据；已有目标函数/输入协议后按 `kb069` 动态验证；有壳、分派器或 VM 证据时按 `kb070`。禁止无静态目标的全程序单步，并保留预算与 closed-vector。

### `templates/ctfy/explore.md`

要求保存真实偏移/反汇编、输入输出、trace、opcode 映射和独立脚本；真实 ELF 直接动态执行必须保留完整 strace 注入，ltrace/gdb 仅限自写可信样本或外部已验证的无网无凭证一次性容器，无法满足即 blocker；不把 strings、YARA 或单次动态异常当作答案。

### `docker/CTFY.md`

新增 Offline Reverse tooling 小节，列出 binutils、radare2、gdb、strace/ltrace、UPX、YARA、Python/z3（可用时）的用途和边界，给出 `tests/reverse_smoke.py` 的非 root 无网命令，注明旧平台/真实题与 solve rate 未验证。

## 已知答案 smoke 设计

新增 `tests/reverse_smoke.py`，只生成可审查的最小 C/Python 源，在临时目录编译和运行：

1. **static 组**：构造带符号的校验/解码 ELF。核对 file/hash/strings 偏移、readelf/nm/objdump、radare2 xrefs；独立 Python 解码已知常量，并用 VA↔文件偏移证据验证正确/错误输入分支，不能仅靠 strings。
2. **dynamic 组**：同一可信 fixture 使用正确/错误/边界输入验证唯一 `REV_OK`、退出码和 strace/ltrace 差异；在无网容器中使用 gdb batch 在目标函数观察寄存器/内存。真实 ELF 的受控包装另行用 socket 尝试样本验证 EPERM，不在 live GDB 外叠加 strace。
3. **pack-vm 组**：用 UPX 在副本上 pack/unpack，比较原 hash、节区/导入/行为；构造小型 C VM，恢复实际 bytecode 偏移和 opcode/handler 语义，用独立 Python 解释器输出唯一 `VM_OK` 并与 C VM 行为对照。

所有组必须 PASS，任何 FAIL 非零；当前镜像不接受 SKIP。保留临时源码、输入、反汇编、trace 和输出直到进程结束，不提交生成物。

## 文件范围与验证

预计：

- 修订 `kb057`，新增 `kb068`–`kb070`。
- 修改 `templates/ctfy/reason.md`、`explore.md`、`docker/CTFY.md`、`tests/test_ctfy_run.py`。
- 新增 `tests/reverse_smoke.py`、本设计和实施计划。
- Dockerfile 只在真实工具缺口时最小修改；当前核对 radare2 6.0.5、UPX 4.2.4、ltrace 0.7.91、YARA 4.5.8 均可用，预计不变。

验证包括精确检索第一命中、KB 内容/预算/来源、提示词区域契约、完整单元测试、静态检查、非 root 无网三组 smoke、实际镜像和版本记录。未做真实 Reverse 题 A/B，不能宣称解题率提升。

## 留给实现者

- 最小 C 校验和 VM 样本的具体 opcode、常量和编译参数，只要确定性、正负对照和真实偏移要求不变。
- radare2 命令脚本和 VA↔文件偏移校验的简洁实现。
- UPX 版本兼容处理；出现真实失败必须报告并修复，不能删除对照或伪造通过。
- smoke 内部分组辅助函数和证据文件命名，保持单一文件和 YAGNI。
