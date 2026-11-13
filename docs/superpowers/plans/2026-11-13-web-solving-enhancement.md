# CTFY Web Solving Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 CTFY 单题 Agent 增加可验证的 Web 黑盒、完整源码和源码片段解题能力，同时修复附件真实路径不同步的问题。

**Architecture:** 保留现有 reason → explore 闭环。附件下载后幂等更新事实图；黑盒通过会话、框架和协议差分工作流处理，白盒通过静态源码审计、自写最小 PoC、可信工具和定向在线验证处理。工具、KB 和提示词分别交付，最后在同一镜像中做端到端接入验证。

**Tech Stack:** Python 标准库、`unittest`、Docker/Kali rolling、`nghttp2-client`、PHP CLI、PHPGGC、`httpx[http2]`、`h2`、`websockets`、`flask-unsign`、JSON KB、Markdown prompts。

**Spec:** `docs/superpowers/specs/2026-11-13-web-solving-enhancement-design.md`

## Global Constraints

- 比赛运行时无公共互联网；不得在运行容器中安装包。只有平台 API、模型网关和当前题目标按现有规则访问。
- 主动探测只针对当前任务明确授权的 endpoint，并保留请求数、并发和时间上限。
- 第一批不直接运行完整附件项目、其依赖、安装脚本或未知二进制，也不加载不可信项目模块进行本地复现。
- 本地动态验证仅限 Agent 审查源码后自写的最小 PoC 和镜像内可信工具；分析产物留在 `/ctfy/work`。
- 第一批不加入 Chromium/Playwright、Semgrep/CodeQL、全量 nuclei 模板或重型 OCR/ML 运行时。
- 新平台代码使用 Python 标准库，测试沿用 `unittest`，保持现有图锁、原子写、typed facts 和提示注入防御。
- 新 KB 编号固定为 `kb060`–`kb064`；关键英文关键词放入 title、description 和 tags，适配现有词法检索。
- 不修改答案提交、verdict、完成条件、结果导出、旧 arena、commander 或 tmux。
- 各任务的提交步骤只在用户明确要求创建 Git 提交时执行；否则保留工作区改动并报告，不自动提交。

## Review Focus

1. **旧 task 快照再次同步：** `path=None` 不得覆盖已经确认的附件路径；由 Task 1 的运行级测试覆盖。
2. **本地与多个远程附件混合：** 更新一个附件不得误改其他附件的路径和下载状态；由 Task 1 的混合附件测试覆盖。
3. **不可信完整项目包含启动/安装诱导：** 提示词必须要求静态审计、自写最小 PoC，禁止直接启动项目；由 Task 4 的提示词契约测试覆盖，真实模型服从性留待 A/B 评估。
4. **非 root 离线运行：** PHPGGC 入口、HTTP/2 extra 和 Flask Session 工具不能只存在于构建日志；由 Task 2 和 Task 4 的容器冒烟覆盖。
5. **KB 查询含标点和中英混合：** `HTTP/2 proxy`、`Werkzeug routing`、`web source audit` 等必须命中预期条目；由 Task 3 的检索契约测试覆盖。

---

### Task 1: 附件下载后的幂等事实同步

**Files:**
- Modify: `lib/ctfy.py:231-248`
- Modify: `lib/ctfy_run.py:190-223`
- Modify: `lib/sapper.py:251-288,623-643,1910-1965`
- Test: `tests/test_ctfy.py:550-568`
- Test: `tests/test_ctfy_run.py:432-565`

**Interfaces:**
- Consumes: 现有 `ensure_attachments(task, client, work_dir) -> None`、`sync_ctfy_facts(store, task, challenge=None) -> None`。
- Produces: `Store.update_attachment_fact(fid: str, path: str) -> dict`；`ensure_attachments()` 成功后在内存 task 中写入真实路径；`sync_ctfy_facts()` 保留旧空路径不回填语义。

- [ ] **Step 1: 写远程附件路径更新的失败测试**

在 `CtfyClientTests.test_downloads_only_null_path_attachment` 中，除现有文件内容和请求断言外，增加：

```python
attachments = task["attachments"]
self.assertEqual(attachments[0]["path"], "/ctfy/work/attachments/crackme")
self.assertEqual(attachments[1]["path"], str(work / "attachments" / "dump.bin"))
```

新增 `test_mixed_attachments_update_only_downloaded_paths`：一个已有本地路径、两个 `path=None` 的远程附件，下载后分别断言三个名称对应的唯一正确路径，并确认只发出两次下载请求。

- [ ] **Step 2: 写运行级附件事实与旧快照回归测试**

在 `CtfyLocalPlatformRunTests` 新增 `test_downloaded_attachment_fact_survives_stale_task_sync`：

- task 包含 `source.zip`、`path=None`；依次 queue challenge 响应和附件字节。
- 在 mocked auto 回调中断言只有一个 attachment fact，其 `data["path"] == str(self.work / "attachments" / "source.zip")`，`data["download_required"] == "false"`，保存 fact id 和总数。
- auto 返回后，用原始 `self.task`（仍为 `path=None`）再次调用 `sync_ctfy_facts()`。
- 断言 fact id、总数、真实路径和 `"false"` 均不变。

另加 `test_attachment_update_rejects_non_attachment_fact`：对普通 `kind="note"` fact 调用新接口应抛出 `FGError`，不得改写原 fact。

- [ ] **Step 3: 运行测试并确认按预期失败**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy.py' -v`
Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'attachment' -v`

Expected: 新断言因远程 task 路径仍为 `None`、图中 `download_required` 仍为 true 或新方法不存在而失败；既有无关测试不失败。

- [ ] **Step 4: 在 `ctfy.ensure_attachments()` 中记录实际下载路径**

保持签名和返回类型 `-> None` 不变。遍历原始 attachment dict，在每次 `download_attachment()` 成功后执行 `attachment["path"] = str(work_dir / "attachments" / name)`；已有非空路径不变，失败路径不提前写入。

- [ ] **Step 5: 增加最小的附件事实更新接口**

在 `Store` 中实现：

```python
def update_attachment_fact(self, fid: str, path: str) -> dict:
```

要求：path 必须是非空字符串；持 `self.locked()`；只接受 `kind="loot"` 且 `data.platform="ctfy"`、`data.record_type="attachment"` 的 fact；保留其他字段，设置 `path` 和 `download_required=False`，经 `normalize_fact_data()` 后用 `atomic_write_json()` 写回。

- [ ] **Step 6: 让 `sync_ctfy_facts()` 幂等更新附件**

将内部 `add_once()` 改为返回已有或新建的 fact dict，其他调用点行为不变。附件循环中，只有输入 `path` 非空且现有路径不同或 `download_required != "false"` 时调用 `update_attachment_fact()`；输入为 `None` 时完全保留已有路径状态。

- [ ] **Step 7: 附件准备后、auto 前再次同步**

在 `lib/ctfy_run.py:201` 的 `ensure_attachments()` 成功后、`auto_args` 构造前调用：

```python
sapper.sync_ctfy_facts(store, task, challenge)
```

不调整现有异常处理、checkpoint、提交或 deadline 顺序。

- [ ] **Step 8: 运行 Task 1 和完整回归测试**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy.py' -v`
Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'attachment' -v`
Run: `python3 -m unittest discover -s tests -v`

Expected: 全部 `OK`，无重复 attachment fact，无临时结果文件残留。

- [ ] **Step 9: 可选提交检查点**

仅在用户明确要求提交时执行：`git add lib/ctfy.py lib/ctfy_run.py lib/sapper.py tests/test_ctfy.py tests/test_ctfy_run.py && git commit -m "fix: sync downloaded CTFY attachment paths"`。

---

### Task 2: 离线 Web 工具与非 root 容器冒烟

**Files:**
- Modify: `docker/Dockerfile:18-58,83-111`
- Create: `docker/phpggc.tar.gz`
- Modify: `docker/CTFY.md` 的 offline tooling 段落
- Test: `tests/test_ctfy_run.py:874-1017`

**Interfaces:**
- Consumes: 现有 Kali APT、PyPI 镜像和固定源归档安装模式。
- Produces: 非 root 命令 `nghttp`、`php`、`phpggc`、`flask-unsign`；Python 模块 `httpx`、`h2`、`websockets`，供 Task 3 KB 和 Task 4 最终镜像验证使用。

- [ ] **Step 1: 写工具声明失败测试**

在 `CtfyContainerContractTests` 新增 `test_web_tools_and_offline_phpggc_are_declared`：

- APT 段包含 `nghttp2-client`、`php-cli`。
- Python 安装段包含 `httpx[http2]`、`h2`、`websockets`、`flask-unsign`。
- Dockerfile 包含 `docker/phpggc.tar.gz`、`/opt/phpggc`、`phpggc -l`，归档文件存在。
- `docker/CTFY.md` 包含 `nghttp`、PHPGGC、`flask-unsign` 和禁止运行时下载的说明。
- Dockerfile 仍不含 `chromium`、`playwright`、`semgrep`、`onnxruntime`。

- [ ] **Step 2: 运行静态契约并确认失败**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'web_tools_and_offline_phpggc' -v`

Expected: 因缺少包声明和 `docker/phpggc.tar.gz` 而失败。

- [ ] **Step 3: 固定并归档 PHPGGC 上游版本**

从 `https://github.com/ambionics/phpggc` 获取一个可验证的 tag/commit，检查根目录实际入口和许可后，将该精确版本归档为 `docker/phpggc.tar.gz`，排除 `.git`、`.venv` 和本地依赖。把版本、commit、来源和归档 SHA256 记入 `docker/CTFY.md`。

涉及 PHPGGC 或 `flask-unsign` CLI 参数时，先按项目全局规则用 `ctx7` 查当前文档，并以固定上游源码/安装后的 `--help` 复核；不要凭记忆修改命令。

- [ ] **Step 4: 修改 Dockerfile**

- APT 列表加入 `nghttp2-client php-cli`。
- Python 安装列表加入 `httpx[http2]`、`h2`、`websockets`、`flask-unsign`，使用与镜像 Python 兼容的版本或范围。
- 按现有源归档模式解压 PHPGGC 到 `/opt/phpggc`，为实际上游入口建立 `/usr/local/bin/phpggc`，删除临时归档，构建时执行 `phpggc -l >/dev/null`。
- 不增加完整 PHP Web 服务栈，不改变最终 `USER agent`。

- [ ] **Step 5: 更新离线工具文档**

在 `docker/CTFY.md` 记录四类用途：HTTP/2 差分、会话/WebSocket 脚本、Flask Session、PHPGGC/最小 PHP 片段。明确完整附件项目不得直接启动，运行容器不得联网补依赖。

- [ ] **Step 6: 运行静态测试并构建镜像**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'web_tools_and_offline_phpggc' -v`
Run: `docker build -f docker/Dockerfile -t sapper-ctfy-web-test .`

Expected: 静态测试通过；Docker 构建成功，PHPGGC 构建冒烟无错误。若 Docker 不可用，必须明确记录未执行，不能写成已验证。

- [ ] **Step 7: 以 agent 用户离线验证工具**

Run:

```bash
docker run --rm --entrypoint /bin/sh sapper-ctfy-web-test -c '
set -eu
test "$(id -un)" = agent
nghttp --version
php -v
phpggc -l >/dev/null
flask-unsign --help >/dev/null
python3 -c "import httpx, h2, websockets; c = httpx.Client(http2=True); c.close()"
'
```

再根据已核实的 `flask-unsign` CLI，用固定测试 secret/cookie 完成签名、解码、重签往返；不向公共网络或真实目标发请求。

Expected: 命令全部退出 0，解码和重签结果与测试值一致。

- [ ] **Step 8: 可选提交检查点**

仅在用户明确要求提交时执行：`git add docker/Dockerfile docker/CTFY.md docker/phpggc.tar.gz tests/test_ctfy_run.py && git commit -m "feat: add offline web analysis tooling"`。

---

### Task 3: Web 知识库扩充与工具一致性

**Files:**
- Create: `kb/entries/kb060.json`、`kb061.json`、`kb062.json`、`kb063.json`、`kb064.json`
- Modify: `kb/entries/kb001.json`、`kb004.json`、`kb005.json`、`kb016.json`、`kb039.json`、`kb043.json`、`kb052.json`
- Test: `tests/test_ctfy_run.py:1020-1078`

**Interfaces:**
- Consumes: Task 2 的 `nghttp`、`phpggc`、`flask-unsign` 和 Python 模块。
- Produces: Task 4 提示词引用的 `kb060`–`kb064`，以及可通过现有 `KnowledgeBase.search()` 命中的 Web 工作流。

- [ ] **Step 1: 写新 KB 检索失败测试**

在 `CtfyKbContractTests` 新增 `test_web_workflow_entries_are_searchable`，用现有 `_load_kb_module()` 和 `KnowledgeBase.default_root()` 断言：

```python
("web session evidence", "kb060")
("Werkzeug routing", "kb061")
("HTTP/2 proxy", "kb062")
("web source audit", "kb063")
("LFI traversal", "kb064")
```

每个查询使用 `limit=20`，并断言五个条目都含 `web` tag。

- [ ] **Step 2: 写旧 KB 工具一致性失败测试**

新增 `test_web_kb_matches_offline_image_and_source_safety`：

- `kb001` 包含 `sourcesContent`。
- `kb004` 的 commands 不含 `tplmap`，detail 含正确的 `{{7*7}}`。
- `kb005` 含 `phpggc`，且明确不启动完整附件项目。
- `kb016`、`kb052` 不含 `nuclei 全模板` 的默认指引。
- `kb039` 含 `签名`、`flask-unsign`，不再把 Flask Session 统称为可加密/可解密保护。
- `kb043` 的 commands 不含 `ddddocr`，detail 不把它写成默认可用依赖。
- `kb062` 含 `nghttp`；`kb063` 同时包含 `完整项目`、`源码片段`、`最小 PoC` 和禁止直接启动项目的语义。

- [ ] **Step 3: 运行 KB 测试并确认失败**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'web_' -v`

Expected: 新条目缺失、旧命令和安全语义断言失败；既有 KB 测试保持可运行。

- [ ] **Step 4: 创建五个新 KB 条目**

严格使用设计中的标题、内容和以下检索锚点：

| ID | Title | 必备英文关键词/tags |
|---|---|---|
| `kb060` | Web 会话、差分证据与停损工作流 | `web`, `session`, `evidence`, `differential` |
| `kb061` | Web 框架路由、调试面与实例生命周期 | `web`, `Werkzeug`, `routing`, `framework` |
| `kb062` | Web 边缘代理与 HTTP 协议差异 | `web`, `HTTP/2`, `proxy`, `nghttp`, `h2` |
| `kb063` | Web 源码审计工作流——完整项目与源码片段 | `web`, `source audit`, `whitebox`, `minimal PoC` |
| `kb064` | Web 文件包含与路径穿越 | `web`, `LFI`, `traversal`, `include` |

内容覆盖设计对应章节，命令只使用 Task 2 或镜像已有工具。`kb063` 明确“完整项目不直接运行、只执行 Agent 自写最小 PoC 和可信工具”；所有条目保留作用域、请求上限、证据和关闭条件。`created_at` 使用实际 UTC 时间，不虚构来源或实测结论。

- [ ] **Step 5: 修订七个旧条目**

按设计“修订现有条目”逐项做最小编辑，不顺手重写其他 KB。PHPGGC 和 `flask-unsign` 命令必须与 Task 2 已核实的 CLI 一致；Source Map 没有 `sourcesContent` 时不得声称恢复完整源码。

- [ ] **Step 6: 验证 JSON、检索和完整测试**

Run: `python3 -m json.tool kb/entries/kb060.json >/dev/null`（对其余新条目同样检查）
Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'web_' -v`
Run: `python3 -m unittest discover -s tests -v`

Expected: JSON 有效，所有查询命中预期 ID，全部测试 `OK`。

- [ ] **Step 7: 可选提交检查点**

仅在用户明确要求提交时执行：`git add kb/entries tests/test_ctfy_run.py && git commit -m "docs: add web blackbox and source audit playbooks"`。

---

### Task 4: Web 双入口提示词与最终接入验证

**Files:**
- Modify: `templates/ctfy/reason.md:18-38,100-118`
- Modify: `templates/ctfy/explore.md:12-45,81-96`
- Test: `tests/test_ctfy_run.py:1081-1188`

**Interfaces:**
- Consumes: Task 1 的真实附件路径、Task 3 的 `kb060`–`kb064`。
- Produces: Web 黑盒/白盒双入口规划与执行约束，以及包含最新代码、工具和 KB 的最终镜像。

- [ ] **Step 1: 写 Web 双入口提示词失败测试**

在 `CtfyPromptIntegrationTests` 新增 `test_web_prompt_routes_source_and_blackbox_without_project_execution`，读取 reason 和 explore，断言组合文本包含：

```python
"kb060", "kb061", "kb062", "kb063"
"完整项目", "源码片段", "最小 PoC"
"sapper kb search", "不得直接启动", "不联网安装"
"closed-vector", "maximum requests or passes"
```

另断言 reason 明确要求有源码先审计再定向在线验证、无源码先建立基线；explore 明确要求保存审计或会话证据，不能只写抽象原则。

- [ ] **Step 2: 运行提示词测试并确认失败**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'web_prompt' -v`

Expected: 因缺少双入口、KB 引用和禁止直接执行完整项目的文本而失败。

- [ ] **Step 3: 修改 reason 的 Web 路由**

将现有单行 Web 路由扩展为简短规则：

- 首轮检查附件事实和实际文件，执行对应 `sapper kb search`，必要时读取 `kb show`。
- 有源码：区分完整项目/源码片段，按 `kb063` 先建立路由、Sink 和未知项，再定向在线验证。
- 无源码：按 `kb060` 建立基线和会话，再按证据选择 `kb061` 或 `kb062`。
- 保留现有最大请求数、并发、wall-clock 和 closed-vector 要求。

详细 payload 不复制进 prompt。

- [ ] **Step 4: 修改 explore 的 Web 执行约束**

增加：保存审计清单或会话/原始响应脚本；不得直接启动完整附件项目或加载其依赖；动态验证只用 Agent 自写最小 PoC 和可信工具；不联网安装；负结果写覆盖范围，达到关闭条件后结束当前 intent。

- [ ] **Step 5: 运行完整测试和 diff 检查**

Run: `python3 -m unittest discover -s tests -p 'test_ctfy_run.py' -k 'web_prompt' -v`
Run: `python3 -m unittest discover -s tests -v`
Run: `git diff --check`

Expected: 全部测试 `OK`，无 whitespace 错误。

- [ ] **Step 6: 重建最终镜像并验证最新 KB/提示词已打入**

Run: `docker build -f docker/Dockerfile -t sapper-ctfy-web-test .`

重复 Task 2 Step 7 的工具冒烟，并在容器内执行：

```bash
sapper kb search "web source audit"
sapper kb search "HTTP/2 proxy"
grep -q 'kb063' /opt/sapper/templates/ctfy/reason.md
```

Expected: 两次检索分别包含 `kb063`、`kb062`，grep 退出 0；镜像仍使用 `agent` 用户。若 Docker 不可用，明确保留为未验证项。

- [ ] **Step 7: 审查最终范围并报告验证边界**

Run: `git status --short` 和 `git diff --stat`。

确认产品改动只涉及设计允许的文件；设计和计划文档可保持未跟踪，直到用户决定是否纳入提交。报告实际执行过的测试、未执行的 Docker/真实题验证，以及“尚未用 A/B 证明 solve rate 提升”的边界。

- [ ] **Step 8: 可选提交检查点**

仅在用户明确要求提交时执行：`git add templates/ctfy/reason.md templates/ctfy/explore.md tests/test_ctfy_run.py && git commit -m "feat: route web blackbox and source audit workflows"`。
