# CTFY Crypto 知识库与本地验证能力扩充设计

## 意图与既定方向

在 Pwn、Reverse 完成后，继续增强同一 CTFY 单题 reason → explore 闭环的 Crypto 能力。当前 `kb041` 只有常见套路总览，缺少可独立检索的 RSA/ECC/格、分组/流密码与协议、PRNG/弱随机工作流。

用户已明确要求不再次询问，按推荐的完整交付单元自动实施。本设计沿用 B 方案：KB、提示词、离线工具文档、契约测试和真实已知答案 oracle 一起交付；不创建提交或推送，完成后结束本轮方向扩充。

## 方案比较

### A：只扩 KB

能快速增加攻击名称，但不能保证首轮读取，也无法证明恢复脚本、查询预算和工具行为正确。

### B：完整 Crypto 交付单元（选定）

新增三个细分条目并修订总入口，增加提示词路由、`docker/CTFY.md` 和 `crypto_smoke.py`。复用现有 pycryptodome、ecdsa、fpylll、sympy/gmpy2/z3、RsaCtfTool、xortool 和 OpenSSL，不引入 Sage 或联网 oracle。

### C：B 加自动 KB 注入

可进一步降低漏读，但会改变 runner/提示词组装，超出本轮范围。

## 目标

1. 先识别密码构造、参数、编码、字节序和 oracle，再选择数学攻击；每个恢复先在本地已知答案上通过。
2. RSA/ECC/格条目覆盖小指数、共模/近素数、Wiener、Boneh-Durfee/Coppersmith 前置、部分私钥泄露、广播、ECC nonce 复用和曲线/阶检查，不把工具输出当私钥证据。
3. 分组/流密码与协议条目覆盖 ECB、CBC、CTR、CFB/OFB、GHASH 滥用、DH 小子群、参数校验、KDF 和协议状态机，明确理论可解与服务预算内可解的区别。
4. PRNG 条目覆盖 MT19937、LCG、xorshift、时间/PID/弱种子、输出宽度/字节序和观测数量，禁止把 `/dev/urandom` 当可预测 MT。
5. 新条目可检索，提示词按构造选择对应工作流，工具只运行明确选择的离线 attack。
6. 以非 root `agent`、`--network none` 验证真实恢复、预测和负对照，保留请求数与停止条件。

## 非目标与反目标

- 不加入 Sage、在线 factordb/sendtofdb、默认 `RsaCtfTool --attack all`、重型格平台或自动 KB 注入。
- 不凭标题套 RSA/CBC/MT 攻击，不把 hashid/xortool/RsaCtfTool 候选或退出码单独当恢复成功。
- 不用无限请求、并行爆破或无界 Sage/z3 搜索代替前置证明；理论可解但预算不足时关闭。
- 不把 nonce 重用的密文 XOR 泄露直接宣称为 GHASH 认证绕过，不把无效曲线点当成已利用。
- 不把 Python `random` 与 OS CSPRNG 混为一谈，不对 `/dev/urandom` 做预测尝试。
- 不重写 Pwn/Reverse/Web/Misc 或修改答案提交/runner。

## 约束与预算

- 先写本地已知密钥/明文/种子 oracle，记录参数、编码、原始字节长度和预期结果，再调用专用工具；恢复后用独立加解密/签名/预测往返验证。
- 远程只访问当前任务授权 endpoint。侦查默认每 intent 最多 20 请求、并发 1、5 分钟；恢复攻击必须先计算并声明逐字节/逐块上限、观测数和 wall-clock，服务预算不足即关闭，不能中途扩大。
- 每个候选必须唯一命中；全部命中、重复异常、连续异常字节、不可区分错误或超时均是实现/前提问题，立即停止并保留负结果。
- RsaCtfTool 只指定一个明确本地 attack，禁止 `all`、factordb/sendtofdb；fpylll/z3 只在构造和规模已证明后使用。
- 新 KB 使用中文说明和前置英文关键词、合法 tags、真实 UTC 和可核验 source；不提交真实 flag、密钥、种子或凭证。
- 默认单元测试不依赖 Docker 或第三方包；`crypto_smoke.py` 不匹配 `test_*.py`，由文档命令显式运行，当前镜像验收不接受 FAIL/SKIP。

## KB 设计

### 修订 `kb041`：Crypto 总入口

保留现有可验证恢复内容和原 `created_at`，新增：RSA/ECC/格 → `kb071`；分组/流密码与协议 → `kb072`；PRNG/弱随机 → `kb073`。

### 新增 `kb071`：RSA、ECC 与格攻击的版本化证据

关键词：`rsa`、`ecc`、`lattice`、`wiener`、`boneh-durfee`、`coppersmith`、`nonce`、`fpylll`。

1. 前置：识别 n/e/dp/dq/密文/签名/曲线/阶/生成元及编码；先本地小参数已知答案，记录预算和停止条件。
2. RSA 分流：小公钥指数与广播、共模、近素数/共享因子、Wiener、Boneh-Durfee/Coppersmith、部分私钥泄露分别列出可证前置，不混用。
3. ECC：nonce 复用恢复先做签名验证；检查异常曲线、阶和生成元，再考虑 invalid-curve、small-subgroup 或有界离散日志。
4. 工具闸门：sympy/gmpy2 用于基础数论，ecdsa 用于已知曲线，fpylll/z3 只用于明确小规模模型；RsaCtfTool 仅单 attack，恢复后独立验证。
5. 负结果：记录参数、候选、工具输出和独立验证；候选不唯一、模型不满足或预算不足即关闭。

### 新增 `kb072`：分组/流密码与协议状态机

关键词：`ecb`、`cbc`、`padding-oracle`、`ctr`、`cfb`、`ofb`、`ghash`、`dh`、`protocol`。

1. 前置：证明模式、IV/nonce、block size、prefix/suffix、padding 和错误信道；保存 hex/raw bytes 对照。
2. ECB secret-suffix：先确认确定性和长度，再逐字节恢复；每轮唯一候选并 checkpoint。CBC bit-flip 与 padding oracle 分开，后者必须有可区分 unpad 错误。
3. CTR/CFB/OFB：先证明 keystream/nonce 复用和状态边界，再做密文 XOR 或状态恢复；GHASH 滥用需分别证明机密性与完整性前提，不混淆。
4. DH/协议：检查 p/q/g、子群、点/参数验证、KDF、会话状态和跨协议角色；小子群/离散日志只在有界规模内求解。
5. 预算与停止：先计算恢复请求公式，区分本地 oracle 与远程可达；错误不可区分、nonce 未复用、参数非法检查已生效或预算不足即关闭。

### 新增 `kb073`：PRNG 与弱随机恢复

关键词：`prng`、`mt19937`、`lcg`、`xorshift`、`seed`、`timestamp`、`pid`、`untemper`。

1. 前置：识别生成器、输出位宽/字节序、观测数量、状态来源和预测目标；先固定已知种子和输出序列。
2. MT19937：624 个 32-bit 输出做 untemper/state 对齐和预测；部分输出/截断需另证可逆约束，不能直接把不足 624 输出当完整状态。
3. LCG/xorshift：从连续输出恢复参数/状态，检查模数、可逆差分、符号位和位宽，回放后续输出验证。
4. 时间/PID/弱种子：只枚举有证据的有限窗口，记录候选数量；窗口无命中即关闭，不扩大为无界爆破。`/dev/urandom`、token、密钥和 CSPRNG 输出不做 MT 预测。
5. 负结果与停止：记录生成器假设、观测、候选、预测对照和预算；预测不一致、生成器不明或观测不足即关闭。

## 提示词与文档

### `templates/ctfy/reason.md`

Crypto 首轮运行 `sapper kb search`：RSA/ECC/格按 `kb071`，分组/流/协议按 `kb072`，PRNG/弱随机按 `kb073`。先构造与本地 oracle，再选择工具；每个 intent 声明请求公式、并发、wall-clock 和 closed-vector，理论可解但服务预算不足时不安排远程攻击。

### `templates/ctfy/explore.md`

保存参数、原始 bytes、oracle、候选唯一性、独立恢复/预测验证和逐字节 checkpoint；禁止默认 all/在线 factordb、把候选当答案或对 `/dev/urandom` 预测；负结果写明覆盖与预算。

### `docker/CTFY.md`

新增 Offline Crypto tooling 小节，列出已安装库和 RsaCtfTool 单 attack 规则，给出 `tests/crypto_smoke.py` 非 root 无网命令，注明数学覆盖范围、真实服务与 solve rate 未验证。

## 已知答案 smoke 设计

新增 `tests/crypto_smoke.py`，只用镜像内已有库和本地生成的小参数：

1. **public-key 组**：RSA 小指数精确开方、共模、共享因子；生成 Wiener 小 d 公钥后用单个 `RsaCtfTool --attack wiener` 恢复并独立解密验证；fpylll 小格已知短向量；ecdsa 同 nonce 双签名恢复私钥并验证。曲线/阶/生成元检查拒绝明确非法输入。
2. **modes-protocol 组**：本地 AES ECB secret-suffix 逐字节恢复与唯一候选；CBC bit-flip 和可区分 padding oracle 恢复；CTR nonce 复用恢复已知明文；小规模 DH 子群/离散日志与非法参数负对照。每个 oracle 记录查询数，超出预算即失败，非确定性/统一错误负对照必须关闭。
3. **prng 组**：624 个 MT19937 输出 untemper 后预测；连续 LCG 输出恢复参数并预测；xorshift 状态回放；有限时间/PID 窗口候选验证及无命中负对照。记录输出位宽、字节序和观测数，不调用 OS CSPRNG 作为可预测对象。

所有组必须 PASS，任何 FAIL 非零；当前镜像不接受 SKIP。保存临时参数、oracle 日志、恢复值和独立验证，不提交生成密钥或真实敏感数据。

## 文件范围与验证

预计：

- 修订 `kb041`，新增 `kb071`–`kb073`。
- 修改 `templates/ctfy/reason.md`、`explore.md`、`docker/CTFY.md`、`tests/test_ctfy_run.py`。
- 新增 `tests/crypto_smoke.py`、本设计和实施计划。
- Dockerfile 只在真实工具缺口时最小修改；当前核对 pycryptodome 4.4.0、ecdsa 0.19.2、fpylll 0.6.4、sympy 1.14.0、OpenSSL 3.6.5、xortool 1.1.0 可用，预计不变。

验证包括精确检索第一命中、KB 内容/预算/来源、提示词区域契约、完整单元测试、静态检查、非 root 无网三组 smoke、实际镜像和版本记录。未做真实 Crypto 题 A/B，不能宣称解题率提升。

## 留给实现者

- 最小 RSA/ECC/DH 参数与测试消息，只要满足攻击前置、可快速验证且不依赖真实敏感数据。
- oracle 的具体函数划分和恢复算法，保持标准库/已安装包、有限查询和独立往返验证。
- RsaCtfTool 输出解析方式，以恢复私钥并完成独立解密为准，不只看退出码。
- smoke 内证据文件命名和分组汇总，保持单一文件、YAGNI 和严格负对照。
