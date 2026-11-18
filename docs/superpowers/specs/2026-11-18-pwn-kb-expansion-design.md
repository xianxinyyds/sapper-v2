# CTFY Pwn 知识库与本地验证能力扩充设计

## 意图

在 `worktree-ctfy-v3-solver` 的现有单题 reason → explore 闭环上，补齐 Pwn 高频题型的可执行工作流。当前 `kb042` 仅提供二进制与协议验证总览，提示词只要求检查附件、保护和工具，尚未覆盖稳定的本地到远程复现、栈/格式化字符串分流，以及按 glibc 版本约束的现代堆利用。

本设计复用镜像已有的 gdb、checksec、pwntools、ROPgadget、ropper、pwninit、patchelf、gcc/make、strace/ltrace、radare2、one_gadget 和 seccomp-tools，不以安装更多工具代替解题方法。重点是把“真实附件 → 本地已知答案 → 有界远程复核 → 可复核负结果”变成 Agent 能检索、能执行、能停止的交付单元。

## 已确认的需求决策

- 本阶段先做 Pwn，不同时扩 Crypto、Reverse 或 Web。
- 采用完整交付单元：KB、CTFY 提示词、镜像工具核对、`docker/CTFY.md`、契约测试和真实已知答案验证一起交付。
- 新增三个细分条目 `kb065`–`kb067`，并修订 `kb042` 作为 Pwn 总入口。
- 复用现有轻量工具；只有真实冒烟发现工具缺口时才修改 Dockerfile。
- 本批不改全局 KB 自动注入或检索评分；通过 Pwn 首轮提示词路由提高新条目的实际读取率。
- 用户已确认：正常解题允许受控运行真实题目提供的独立 ELF。仅限静态审查后的 stdin/stdout ELF，在可写副本目录以非 root、清空环境、禁网、超时和资源限制运行，使用随题 libc/loader；不运行安装器、完整附件项目、未知依赖或要求额外特权的二进制。
- 同 UID 进程隔离不是强沙箱：即使清空环境并拒绝新 socket，题目 ELF 仍可能访问同 UID 可读文件或干扰同 UID 进程。执行前必须记录这一残余风险，发现异常文件、进程、权限或网络行为即停止，不声称已有完整隔离。
- 不创建提交或推送，所有改动留在指定 worktree。

## 目标

1. Agent 在计算偏移或构造 ROP 前，先固定附件 hash、架构、保护、libc/loader 和协议证据。
2. 每条利用链先在本地已知答案样本或题目附件的受控副本上验证，再对当前任务授权 endpoint 做有界复核。
3. 栈与格式化字符串条目能根据已证明的读、写、控制流原语选择路径，不凭远程猜测偏移。
4. 现代堆条目以真实 libc 和已证明的堆操作为前置条件，明确 SafeLinking、key、double-free、对齐和 pointer guard 等版本约束。
5. 新条目能通过常见中英混合查询检索，提示词能引导首轮 Pwn 规划和执行读取对应条目。
6. 新增命令与镜像实际工具一致，并以非 root `agent` 用户、无公共网络环境完成真实冒烟。
7. 保留现有答案提交、实例生命周期、事实图、作用域和提示注入防御语义。

## 非目标

- 不实现自动 KB 注入、向量检索、分类器或新的 Pwn solver 进程。
- 不加入 angr、Sage、Ghidra、QEMU/multi-arch、Frida、Android 或其他重型平台。
- 不罗列未经本地或源码证据支持的 House-of-* 利用名称，不提供通用“一把梭”远程 exploit。
- 不构造跨 glibc 版本都适用的堆链，不宣称未实测的旧版本行为已经验证。
- 不连接真实比赛目标做开发冒烟，不修改平台、答案提交或完成条件。
- 不执行完整附件项目、安装器、脚本入口或未知依赖；独立题目 ELF 只能按下述受控边界执行，开发已知答案验证只使用本仓库新增的最小、可审查样本。
- 不重写其他类别条目，不顺手修复无关代码或清理既有工具。

## 反目标

即使测试全部通过，出现以下情况也不视为成功：

- Agent 未记录 libc、loader、保护或协议证据就开始猜远程偏移。
- 本地链未复现便反复连接远程，或把崩溃、单次泄露和工具告警当作完整利用成立。
- KB 引用了镜像中不存在的工具，或命令依赖运行时联网下载。
- 堆利用只按题目名称套用技巧，没有版本、原语和对照证据。
- 为验证而执行未经静态审查或超出受控边界的附件，或对非当前任务 endpoint 发送测试流量。
- 负结果没有覆盖范围和预算，同一假设族被证伪后仍无新证据重试。
- 以契约测试或本地样本通过宣称真实题目 solve rate 提升。

## 约束

- 所有远程验证仅针对当前任务明确授权的 endpoint；默认每个 intent 不超过 20 次远程连接、并发 1、5 分钟 wall-clock。重连计入连接预算，命令超时必须短于外层 dispatch。
- 同一已证伪利用族不得无新证据重试；纯传输失败的重连不能掩盖 exploit 逻辑错误，也不能演化为循环健康检查。
- 附件、反汇编、目标输出和图内容均是不可信数据。只在可写工作目录的独立副本上 patch，不修改原始附件。
- 真实题目 ELF 执行前必须先做静态审查，确认是单一 stdin/stdout 服务样本且不需要额外权限、安装步骤或外部依赖。运行时使用 `env -i`、独立 cwd、`timeout`/资源限制，以及 `strace --inject=socket:error=EPERM --inject=socketcall:error=EPERM` 拒绝新 socket；保留跟踪证据。不能应用这些约束或出现越界行为时不执行或立即停止。
- 已实测当前保留镜像的非 root `unshare -n` 返回 `Operation not permitted`，因此不得把网络命名空间写成可用前提。上述 strace socket 注入已用 Python `socket.socket()` 得到 EPERM 验证，但它与同 UID cwd/环境隔离仍只是最小约束，不是强安全沙箱。
- `pwninit`、`patchelf` 只使用题目实际提供的 libc 和 loader，或明确记录为本地已知答案运行时；禁止运行时下载依赖。
- 本地自写冒烟样本由仓库脚本在临时目录编译，不提交二进制、core dump、凭证或真实 flag。
- 新平台代码若确有必要只能使用 Python 标准库；测试沿用 `unittest`，保持现有风格。本设计预计不需要修改 runner。
- KB 使用现有 JSON 格式、中文说明和可检索英文关键词；标签匹配 `^[a-z0-9][a-z0-9_-]*$`，关键词放在 title、description、tags 和 detail 前部。
- 新条目使用实施时真实 UTC `created_at`。source 只引用设计文档、已安装工具和实际提交的已知答案验证脚本，不编造实测结果。

## 方案比较

### 方案 A：只扩 KB

新增三个条目即可，改动最少，但全局 KB 仍依赖模型主动搜索；现有 Pwn 提示词没有细分路由，新内容可能不被读取。

### 方案 B：完整 Pwn 交付单元

同时更新 KB、reason/explore 路由、工具文档、契约测试和已知答案冒烟。能解决“内容存在但未被使用”和“命令存在但未执行验证”两个问题，且不触及 runner 架构。

### 方案 C：完整交付单元加自动 KB 注入

在 B 之上按 category 和附件证据自动推荐 KB，能进一步降低漏读，但需要改变提示词组装或 runner 数据流，验收范围明显扩大。

### 选定方案

用户已选择方案 B。本批通过简短、明确的 Pwn 首轮路由引用 `kb065`–`kb067`，自动注入留作后续独立设计。

## 知识库设计

### 修订 `kb042`：Pwn 总入口

保留现有二进制、协议和工具总览，以及 `pwn patchelf` 的既有检索能力，新增明确分流：

- 附件/运行时不一致、协议复现、本地到远程迁移 → `kb065`。
- 栈控制流、ROP、格式化字符串 → `kb066`。
- UAF、double free、bin/tcache 和 SafeLinking → `kb067`。

总入口只负责共同前置检查与路由，详细方法放在新条目，避免重复大段文本。原有 `created_at` 保持不变。

### 新增 `kb065`：Pwn 本地到远程利用复现与证据工作流

建议 title 前部包含 `Pwn local remote exploit`，description/tags 覆盖 `pwntools`、`patchelf`、`libc`、`loader`、`protocol`、`reconnect`。

内容分为四段：

1. **固定事实**：记录附件路径、SHA256、ELF 架构与 endian、NX/PIE/RELRO/Canary/CET、动态依赖、libc/loader hash、符号和实际输入输出协议。缺失附件或运行时必须标为未知，不得补猜。
2. **建立本地对照**：在副本目录用 patchelf/pwninit 固定解释器和 libc；保存 patch 前后 hash、命令和 `patchelf --print-interpreter` 输出。真实题目 ELF 只有在满足受控执行边界时才运行；否则只做静态分析和自写最小样本复现，并记录 blocker。用正常输入、边界输入和崩溃输入建立对照，保存最小输入、退出状态、gdb 寄存器/栈证据。
3. **迁移到授权远程**：复用同一份 exploit 状态机，仅替换 process/remote transport；显式设置 timeout，处理 banner、分帧、菜单同步和连接关闭。远程偏移、泄露和 libc 必须由证据重新确认，不能把本地值当作远程常量。
4. **证据与停止**：保存 exploit 脚本、输入、崩溃/泄露证据、本地与远程结果矩阵。负结果记录已测保护、偏移来源、运行时、协议状态和连接预算；前提证伪、达到预算或两组对照无新增证据即关闭。

命令仅使用现有 `file`、`sha256sum`、`checksec`、`readelf`、`patchelf`、`pwninit`、gdb、strace/ltrace 和 pwntools。

### 新增 `kb066`：Pwn 栈利用与格式化字符串

建议 description/tags 覆盖 `stack`、`ret2plt`、`ret2libc`、`rop`、`stack-pivot`、`srop`、`format-string`、`fsop`。

内容按原语分流：

1. **共同前置**：先由本地崩溃和反汇编确认返回地址偏移、栈对齐、可用泄露和可写区域；PIE/ASLR、Canary 和 seccomp 都需要独立证据，不能把保护名称直接等同于不可解。
2. **栈路径**：根据已证明条件选择 ret2win、ret2plt/GOT、ret2libc、栈 pivot 或 SROP。ROPgadget、ropper、pwntools ROP 和 objdump 用于交叉核对 gadget；每条链保留寄存器约束、栈布局和本地已知答案输出。
3. **格式化字符串**：先区分字面回显与真实格式解释，再用唯一 marker 定位参数偏移；分别证明地址泄露和写原语。使用 pwntools 生成写 payload 前必须核对架构、offset、目标地址和写宽度，保留普通字符串对照。
4. **目标选择**：GOT overwrite 需要可写 GOT 与合适 RELRO 条件；FSOP 需要真实 libc、足够读写原语、vtable 校验与 pointer guard 分析。条件不足时关闭对应路径，不降级为盲写。
5. **停止条件**：负结果记录已测偏移、gadget、泄露格式、写目标和保护前提；不把 crash 或 `%p` 输出单独当作可利用完成。

### 新增 `kb067`：Pwn 现代堆利用与 glibc 版本分流

建议 description/tags 覆盖 `heap`、`glibc`、`tcache`、`fastbin`、`unsorted-bin`、`uaf`、`double-free`、`safe-unlink`、`safelinking`、`pointer-mangling`。

内容要求：

1. **前置与最小模型**：固定真实 libc 版本/hash，记录 chunk size、对齐、分配/释放顺序、UAF/double-free 可读可写能力和 bin 状态。先用最小本地程序复现分配、释放和 crash，不用 House-of-* 名称代替原语证明。
2. **版本闸门**：tcache/fastbin/unsorted bin 的选择必须结合实际 glibc。明确比较 2.31 与 2.35+ 时涉及的 tcache key、double-free 检测、对齐、SafeLinking 和 pointer guard；具体行为由对应 libc 源码/符号和本地 PoC 复核，不能仅按发行版名称推断。
3. **SafeLinking 与 mangling**：只有版本和堆地址证据满足时才计算 `PROTECT_PTR`/`REVEAL_PTR` 关系；记录 storage position、next pointer、heap key 和解码对照，不把指向 heap 的泄露直接当作已绕过。
4. **利用路径**：tcache poisoning、fastbin/unsorted-bin、UAF、double free 和 safe-unlink 只从已经证明的分配原语出发；每次写后检查对齐、链表完整性和分配返回地址。hook/FSOP 等后续目标另做版本与写原语闸门。
5. **对照与停止**：保存正常分配、单一原语和完整链三组结果；负结果记录版本、chunk/bin、输入序列、断言/abort 文本和尝试预算。版本未知、原语未证实或关键完整性检查未绕过即停止该链。

本条目不会声称所有列出的路径都在当前镜像上完成过利用验证；版本差异作为方法约束，实际冒烟范围在验证结果中单独说明。

## 提示词与文档设计

### `templates/ctfy/reason.md`

将 Pwn 一行原则扩展为简短路由：

- 首轮 Pwn 规划核对附件、libc/loader 和 endpoint 协议，运行 `sapper kb search`，按当前假设读取 `kb065`–`kb067`。
- 没有本地已知答案和偏移/原语证据时，优先安排本地复现，不直接安排远程 exploit。
- 本地到远程迁移、栈/格式化字符串、现代堆分别使用对应条目；每个 intent 声明连接数、并发、时间上限和 closed-vector。

### `templates/ctfy/explore.md`

- 执行前读取当前 intent 指定条目与已有证据，避免重复全量检索。
- 保存 exploit/PoC、最小输入、运行时 hash、崩溃或原语证据和本地/远程对照。
- 真实题目 ELF 只能按受控执行边界运行：静态审查、独立副本、`env -i`、资源限制、strace 拒绝新 socket；不把该约束描述为强沙箱，不满足边界时记录 blocker 并停止动态执行。
- 远程重连计入预算；无新证据不得重复已证伪链。
- 详细技术仍留在 KB，提示词只保留路由和硬性证据要求。

### `docker/CTFY.md`

新增简短的 Offline Pwn tooling/verification 小节：

- 列出已安装的 Pwn 工具及各自用途，不增加运行时安装入口。
- 说明 patchelf/pwninit 的本地副本和禁止下载规则，以及真实独立 ELF 的受控执行边界与同 UID 残余风险。
- 给出已知答案冒烟脚本在 Docker 中以 `agent`、`--network none` 运行的命令。
- 明确本地冒烟不验证真实目标，也不证明旧 glibc 或 solve rate。

## Docker 与工具核对

实现时先在保留的 `sapper-ctfy-web-test:latest` 中检查实际命令和版本。预计 Dockerfile 无需变化：

- 若所有引用命令均以 `agent` 离线运行成功，保持 Dockerfile 不变，并在交付中明确“核对后无工具缺口”。
- 若发现真实缺口，只添加能由现有 Kali/PyPI 来源提供的最小轻量依赖，同时更新声明测试和文档；不借此加入非目标中的重型工具。
- 新 KB 和提示词由现有 `COPY kb/`、`COPY templates/` 机制进入后续镜像，不新增第二套安装路径。
- 工具冒烟使用 bind mount 提供当前测试脚本，避免为了未改变的工具链强制进行全量重建；若 Dockerfile 有改动，则必须重新构建后再验证。

## 已知答案冒烟设计

新增 `tests/pwn_smoke.py`。文件名不匹配默认的 `test_*.py` discovery，因此普通标准库测试不要求 Docker、gcc、gdb 或 pwntools；它由文档中的 Docker 命令显式运行。

脚本只使用 Python 标准库和镜像内已有工具，在临时目录生成并编译最小程序，所有断言失败均非零退出：

1. **工具与 ELF 基线**：以 `agent` 执行 checksec、gdb batch、ROPgadget、ropper、patchelf、pwninit、one_gadget 和 seccomp-tools 的真实命令；至少对编译样本运行 ELF/ROP/patchelf 检查，而非只检查包声明。
2. **栈已知答案**：编译无 Canary、无 PIE 的最小 ret2win 样本。普通短输入不产生成功标记，唯一模式崩溃输入得到受控返回地址证据，pwntools ROP 链得到唯一 `STACK_OK` 标记。
3. **格式化字符串已知答案**：编译只处理固定输入的最小样本。普通字符串保持字面输出，`%p` 暴露预期 marker，受控写把专用全局变量改为已知值并产生唯一成功标记；不扫描远程格式串。
4. **当前 glibc 堆已知答案**：先记录实际 `ldd`/libc 版本。使用对齐的专用目标和最小程序验证当前运行时的 SafeLinking 编码关系与 tcache poisoning 返回目标；另以独立进程验证 double-free 检测的已知 abort。只有运行时低于样本要求的 SafeLinking 版本时，才可把这一组标为 SKIP；SKIP 使总体结果为 INCOMPLETE 并非零退出，不能作为本次交付通过。
5. **loopback 传输**：通过 127.0.0.1 将同一栈样本接入 socket，复用相同 payload 获得成功标记，并验证 pwntools timeout/关闭处理和一次有界 reconnect。Docker `--network none` 下只允许 loopback，不访问外部或真实比赛 endpoint。

每个用例设置进程超时，保留临时目录中的源码、输入和命令输出直到进程结束；成功输出包含版本和各项 PASS。除上述版本闸门外，gdb/ptrace、堆行为、工具缺失或 loopback 限制均记为 FAIL 而非 SKIP。任何 FAIL 或 SKIP 都使总体非零退出；失败输出保留最小诊断。脚本不提交生成的二进制或 core dump。

**验收口径（builder 检查后由本设计确定）**：本次指定保留镜像的交付验收不接受 SKIP，所有必需组必须真实 PASS。旧运行时上的单项 SKIP 只用于解释覆盖缺口，不能被脚本或交付报告统计为成功。

## 文件变更范围

预计修改：

- `kb/entries/kb042.json`
- 新增 `kb/entries/kb065.json`、`kb066.json`、`kb067.json`
- `templates/ctfy/reason.md`
- `templates/ctfy/explore.md`
- `docker/CTFY.md`
- `tests/test_ctfy_run.py`
- 新增 `tests/pwn_smoke.py`
- 新增本设计文档

条件性修改：

- `docker/Dockerfile`：仅真实工具缺口需要；若修改，补对应声明测试并重建镜像。

明确不修改 runner、KB 检索算法、答案提交、其他类别 KB 和旧 arena/commander/tmux 路径。

## 验证

### 契约与回归测试

1. 先新增并运行失败的 Pwn 检索/提示词契约，再实现内容使其通过。
2. 检索至少覆盖：
   - `pwn local remote patchelf` → `kb065`
   - `stack ret2libc format string` → `kb066`
   - `heap tcache SafeLinking` → `kb067`
   - 既有 `pwn patchelf` → `kb042` 仍成立
3. 三个新条目均有 `pwn` 标签、合法标签、真实时间、非空 source、前置条件、预算、对照和停止条件；命令不含排除工具或联网安装。
4. 提示词契约包含 `kb065`–`kb067`、本地已知答案、真实 libc/loader、远程预算和停止要求。
5. 完整运行 `python3 -m unittest discover -s tests -p 'test_*.py' -v`、`bash -n docker/entrypoint-ctfy.sh`、`python3 -m compileall -q lib tests` 和 `git diff --check`。

### 镜像已知答案验证

- 以非 root `agent` 和 `--network none` 显式运行 `tests/pwn_smoke.py`。
- 工具命令、ret2win、格式化字符串、当前 glibc 堆样本和 loopback 用例均必须真实 PASS；本次指定镜像出现任何 FAIL 或 SKIP 都表示验收未完成，必须修复或如实报告，不能统计为成功。
- 记录实际镜像 ID、glibc/pwntools 版本和运行命令，不把当前 glibc 结果外推为 2.31/2.35 全版本实测。
- 若 Dockerfile 未改变，说明复用了哪个保留镜像；若改变，则先构建新测试镜像再运行。

### 交付边界

最终报告区分：契约测试通过、工具冒烟通过、哪些版本或路径未实测。没有代表性 Pwn 题集 A/B 时，只能声称“知识、提示路由和本地验证链路已接通”，不能声称真实解题率提升。

## 留给实现者的局部决策

- 三个 KB 条目的最终措辞和命令排版，但必须保留上述检索词、前置条件、预算和停止语义。
- 最小 C 样本的具体源码和编译参数，只要可审查、确定性好且能完成正负对照。
- 冒烟脚本内部函数划分和临时目录管理，不引入第三方测试框架。
- pwninit/patchelf 冒烟使用当前镜像运行时的具体路径，但不得下载额外 libc/loader。
- 若现有镜像暴露工具缺口，先说明缺失命令、影响与最小修复，再决定是否触及 Dockerfile。
