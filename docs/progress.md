# Minimal Agent 开发进度

- 计划版本：1.1
- 当前步骤：步骤 11——全链路验收与运行文档
- 当前状态：真实模型验收完成，等待用户最终 Review
- 最后更新：2026-10-24（Asia/Shanghai）

## 执行规则

1. 每次只执行一个冻结计划步骤。
2. 当前步骤完成后暂停，等待用户 Review 和明确确认。
3. Review 修复仍属于当前步骤，修复完成后再次等待确认。
4. 未经确认不开始下一步，不调整步骤顺序、范围或验收条件。
5. 任何计划调整先记录原因、影响和迁移成本，状态标记为“待确认”。

## 当前步骤目标

补齐结构化推理与 `<think>` 双路流、Responses/Chat Completions 适配、逐工具测试契约、
供应商无关真实模型验收、构建产物与运行文档。本步骤完成后进入最终 Review，不再开始
新的实施步骤。

## 已完成任务

- [x] 编写完整设计方案 `docs/design.md`。
- [x] 冻结 11 个实施步骤及每步验收条件。
- [x] 创建 `pyproject.toml` 及运行/开发依赖。
- [x] 创建 `.env.example` 和 `.gitignore`。
- [x] 创建 FastAPI 应用工厂和 `/health`。
- [x] 创建最小健康检查测试。
- [x] 创建项目 README。
- [x] 使用隔离虚拟环境安装项目及开发依赖。
- [x] 通过 pytest、Ruff lint 和 Ruff format 检查。
- [x] 创建 SQLite 连接和短事务管理，启用 WAL、外键、busy timeout。
- [x] 创建版本化且可重复执行的 `schema.sql`。
- [x] 实现 Session、Message、Run、Trace、StreamEvent Repository。
- [x] 实现每 Session 独立消息序号和 Run 内独立流事件序号。
- [x] 实现每 Session 最多一个 running Run 的数据库约束。
- [x] 实现摘要覆盖范围单调推进和越界保护。
- [x] 实现 Run 完成、失败、循环计数和启动恢复所需状态转换。
- [x] 验证跨 Session 引用保护、事务回滚和并发消息序号。
- [x] 将 `schema.sql` 纳入 wheel package data 并检查构建产物。
- [x] 实现无全局状态的 ToolDefinition 和 ToolRegistry。
- [x] 实现 OpenAI Responses API 扁平 Function Schema 导出。
- [x] 注册时校验严格 Schema：禁止额外属性，全部属性必须 required。
- [x] 实现 Pydantic 参数验证和 JSON 参数解析。
- [x] 实现同步/异步 handler 调度、执行超时和统一 ToolResult/ToolError。
- [x] 对未知工具、非法参数、超时、预期异常和未知异常返回稳定错误码。
- [x] 实现基于 AST 白名单与 Decimal 的 calculator。
- [x] calculator 支持基础算术、整数幂、括号、abs、sqrt 和 round。
- [x] calculator 限制表达式长度、节点数、嵌套、幂和中间结果规模。
- [x] calculator 拒绝导入、属性访问、推导式、未知函数和非白名单语法。
- [x] 添加带官方来源和核验日期的本地主板 JSON 数据集。
- [x] 数据集覆盖 ASUS、MSI、Gigabyte、ASRock 共 16 款主板。
- [x] 添加数据维护约定，并提供可配置的 `MAINBOARD_DATA_PATH`。
- [x] 实现数据集 Schema、一次性加载校验、重复 ID 拒绝和确定性搜索排序。
- [x] 实现大小写、Unicode、标点归一化以及多字段 AND 分词匹配。
- [x] 型号和别名精确匹配优先，分数相同时按品牌、型号和 ID 稳定排序。
- [x] 实现 search 工具定义并接入现有 ToolRegistry 协议。
- [x] 覆盖型号、别名、品牌、规格、limit、空结果和坏数据测试。
- [x] 实现 Open-Meteo 地理编码与天气查询客户端。
- [x] 请求当前天气和最多 7 天预报，并提供摄氏/华氏温标选项。
- [x] 使用严格 Pydantic 模型校验地理编码、当前天气、单位和等长每日数组。
- [x] 将 WMO 天气代码映射为稳定的英文描述。
- [x] 实现 weather 工具定义并接入现有 ToolRegistry 协议。
- [x] 实现显式连接、读取、写入和连接池超时的共享 HTTP 客户端工厂。
- [x] 对无地点、429、超时、网络故障、4xx/5xx 和畸形响应返回结构化错误。
- [x] 使用 MockTransport 覆盖正常响应、无地点、429、超时、5xx 和畸形响应。
- [x] 实现内部 LLM 请求、流事件、终态判断和稳定错误协议。
- [x] 实现脚本化 Fake LLM，并记录每次请求用于后续 AgentLoop 断言。
- [x] 实现基于 OpenAI SDK 的 Responses API 流适配器及事件白名单。
- [x] 映射正文、公开 reasoning summary、拒绝、工具参数和响应终态。
- [x] 丢弃 DeepSeek 原始 `reasoning_text` 内容，仅保留 reasoning 阶段生命周期事件。
- [x] 实现超时、429、连接和 5xx 的最多 3 次有限重试及退避事件。
- [x] 流已产生语义事件后禁止自动重试，避免向下游发送重复增量。
- [x] 支持 DeepSeek/OpenAI 的 base URL、模型和 API Key 环境变量别名。
- [x] SDK 内建重试关闭，避免与适配器重试叠加导致次数失控。
- [x] 添加默认跳过、显式开启才产生费用的 DeepSeek live smoke test。
- [x] 覆盖正文、公开推理摘要、工具参数、完成、拒绝、中断和重试测试。
- [x] 编写并加载 `system.md` 与摘要提示词。
- [x] 实现精确 `@工具名` 解析和工具范围收窄。
- [x] 实现 Session 隔离的 ContextBuilder 与消息格式转换。
- [x] 实现可替换 TokenCounter、80% 阈值检测和上下文超限错误。
- [x] 实现已结束旧轮次选择、最近 5 轮保护和摘要覆盖单调推进。
- [x] 覆盖 Session 隔离、记忆、压缩、重复摘要保护和不可压缩超限测试。
- [x] 定义并验证标准化 SSE 对外事件。
- [x] 实现有界队列、单写入器、8 KiB/100 ms/屏障刷新。
- [x] 实现仅合并相邻同类型同 item 的 delta。
- [x] 实现事件提交后通知与多订阅者等待。
- [x] 实现 SQLite 重放、`Last-Event-ID`/`after_id` 和心跳。
- [x] 实现终态关闭和客户端断开不影响生产者测试。
- [x] 实现 LLM 流事件累积与“工具调用或最终答案”决策。
- [x] 实现工具调用、参数错误回传和多步 ReAct 循环。
- [x] 实现最大步骤、总运行时限、Context 和 LLM 异常终止。
- [x] 实现消息、Run 终态和 SSE 终态事件的原子提交。
- [x] 为关键生命周期、工具结果和全部异常记录 Trace。
- [x] 实现 RunSupervisor 强引用、清理、启动恢复和限时关闭。
- [x] 覆盖普通回答、连续工具、错误回传、终态原子性、恢复和 Trace 测试。
- [x] 实现 Session 创建、列表、详情和消息历史路由。
- [x] 实现原子化“用户消息 + Run”创建和 `202` 后台执行入口。
- [x] 实现 Run 状态、Trace 和既有 SSE 路由的完整应用接入。
- [x] 实现统一 JSON 错误 envelope 及稳定的 404/409/422/503 错误码。
- [x] 实现显式应用服务图、Agent 配置降级、启动恢复和关闭生命周期。
- [x] 覆盖同 Session 冲突、不同 Session 并发、生命周期和 OpenAPI 契约测试。
- [x] 增加 HTTP→AgentLoop→calculator/search→SQLite→SSE 多工具端到端测试。
- [x] 增加跨轮 Session 记忆/隔离和未知 LLM 异常脱敏全链路测试。
- [x] 增加默认跳过的 DeepSeek HTTP→SSE 真实全链路冒烟测试。
- [x] 将 schema、提示词和主板数据纳入发行产物并实现源码/安装路径解析。
- [x] 在全新虚拟环境、非源码工作目录验证 wheel 安装和资源加载。
- [x] 补齐源码发行清单、部署、单 worker、备份恢复、维护和故障排查文档。
- [x] 将设计与进度基线升级为 1.1，并记录修订原因、影响和兼容策略。
- [x] 扩展通用 LLM 请求与推理事件，区分 raw、summary、activity 及具体来源。
- [x] Responses 适配器透传 `reasoning_text`、reasoning summary，并保存 reasoning item
  供工具调用后的下一轮续传。
- [x] 新增 OpenAI-compatible Chat Completions 流式适配器，支持 `reasoning_content`、
  正文、拒绝、跨 chunk 工具参数、usage 和终态映射。
- [x] 新增供应商无关 ReasoningRouter 和跨 chunk `<think>` 状态机，支持 auto 去重、
  structured/tagged/both 选择、未闭合标记及正文净化。
- [x] 新增 `reasoning.started/delta/done` 持久化事件字段并接入 AgentLoop；推理不进入
  最终 assistant message，事件写库与 SSE 读取仍保持解耦。
- [x] 使用 `LLM_API_STYLE` 选择 Responses 或 Chat 适配器，补齐通用 reasoning、输出上限
  和 think tag 配置；模型专用 reasoning 参数默认留空，保留旧供应商环境变量别名。
- [x] 为 ToolDefinition 增加 pure/read/write 和 safe/idempotency_key/never 元数据。
- [x] 将三个工具单元测试和 Agent 集成测试按工具拆分，并增加注册表测试清单契约。
- [x] 将 DeepSeek 专用真实测试替换为 `tests/live/test_llm_live.py`，支持当前 `.env` 模型、
  smoke/full profile、调用次数上限和 reasoning 能力预期。
- [x] full live profile 以最多 8 次 LLM 调用覆盖 calculator、search、Mock weather、真实
  摘要压缩和 reasoning；默认测试不访问付费 API。
- [x] 更新 `.env.example`、system prompt、README、设计和运行手册。
- [x] 按用户最终验收需要新增 Vue 3 测试控制台，覆盖 Session、消息、SSE、推理流、
  工具事件、Run 状态和 Trace 查看。
- [x] 使用 Vite 开发代理连接现有 FastAPI，无需放宽后端 CORS；完成桌面与移动端浏览器
  验收。
- [x] 参考 AgentFlow Studio `tool-plugin` 界面重构为浅色工作台：会话按日期分组、用户/助手
  气泡、工具状态卡片、安全 Markdown 渲染及独立诊断面板。
- [x] 修复长对话消息区未形成独立滚动容器的问题，并增加固定可见滚动条、打开会话自动
  定位最新消息、用户向上阅读时暂停自动跟随和“跳到最新消息”入口。

## 验证记录

- `Python 3.12.9`：通过。
- `frontend\npm run build`：通过（Vite 7.3.7，生产构建成功）。
- Vue 控制台浏览器验收：开发代理健康检查通过、Session 创建成功、桌面与 390 px 移动
  布局通过、浏览器控制台无错误；未触发付费 LLM 调用。
- 长对话浏览器验收：消息区 `clientHeight=412`、`scrollHeight=1479`，可从 `scrollTop=0`
  滚动至最大值 `1067`；重新打开会话立即定位最大值，Markdown 表格正常渲染，浏览器
  控制台无错误。
- `.venv\\Scripts\\python -m pytest -ra`：`129 passed, 2 skipped in 11.17s`（真实验收配置修订后
  最终复验；默认跳过 2 个付费、供应商无关 live profile）。
- `.venv\\Scripts\\python -m ruff check .`：`All checks passed!`。
- `.venv\\Scripts\\python -m ruff format --check .`：`125 files already formatted`。
- `.venv\\Scripts\\python -m build`：成功构建 sdist 和 wheel。
- 发行内容检查：wheel 含 schema、主板数据和两个提示词；sdist 含 `.env.example`、完整
  docs 和测试，但不含真实 `.env`。
- 干净安装检查：全新 Python 3.12 虚拟环境从 wheel 安装成功，在非源码目录可解析全部
  运行资源并导入 FastAPI 应用。
- wheel 构建检查：`minimal_agent/storage/schema.sql` 已包含在产物中。
- SQLite `PRAGMA foreign_key_check`：无违规；Session 级联删除试验无残留记录。
- 生产数据冒烟查询 `B650 Wi-Fi 6E Mini-ITX`：稳定返回 4 条匹配记录。
- 真实 Open-Meteo 冒烟调用 `Beijing, China`：地理编码、当前天气和 2 天预报均通过
  严格响应校验。
- 用户在本地使用 DeepSeek API 完成步骤 6 真实测试并确认无问题。
- 步骤 7 验证了 Session 隔离、精确工具点名、工具调用/结果回放、最近 5 轮保护、
  摘要覆盖并发竞争保护、不可压缩超限和摘要流组装。
- 步骤 8 验证了 8 KiB、定时和屏障刷新，相邻 delta 合并、提交后多订阅者通知、事件
  顺序、断线补发、无重复、心跳不落库、终态关闭及客户端断开后生产者继续执行。
- 步骤 9 验证了普通答案、工具循环、参数错误回传、每步单工具限制、最大步骤、总时限、
  Context 超限、LLM 错误、终态事务回滚、启动恢复、任务清理/关闭和完整 Trace。
- 步骤 10 验证了完整 API 正常路径、持久化 SSE、稳定错误 envelope、同 Session 409、
  不同 Session 并发、启动恢复、OpenAPI 路径与响应 Schema。
- 步骤 11 版本 1.1 验证了 Responses/Chat 两种事件映射、raw/summary/reasoning_content/
  `<think>` 捕获、来源去重、reasoning item 续传、逐工具端到端链路、工具测试契约和发行
  内容；两个通用 live profile 保持显式 opt-in，默认门禁不产生 LLM API 费用。
- 2026-10-24 使用本地 `.env` 对 `deepseek-flash`（Responses 风格接口）完成真实验收：
  smoke profile `1 passed, 1 skipped in 6.02s`；full profile 在 1024 token 输出上限下
  `1 passed, 1 skipped in 15.69s`，覆盖 calculator、search、Mock weather、真实摘要压缩、
  持久化 SSE，并强制断言结构化 `reasoning_text/raw` 推理事件。

## 实施中发现并修复的问题

- 首次格式检查发现 3 个 Python 文件缺少 Ruff 期望的文件尾格式；已执行 Ruff format
  修复并重新验证。
- 首次健康测试使用 FastAPI `TestClient` 时，当前 Starlette 版本产生 httpx 弃用警告；
  已改用 `httpx.AsyncClient` 和 `ASGITransport`，测试通过且无警告。
- 步骤 2 首轮门禁通过后发现 wheel 可能遗漏非 Python Schema 文件；已在
  `pyproject.toml` 声明 package data，并通过构建 wheel 验证文件存在。
- 步骤 3 首次静态检查要求 Python 3.12 原生泛型语法；已将 ToolDefinition 改为
  PEP 695 泛型声明并重新通过全部门禁。
- 步骤 4 首轮测试有 1 个失败：测试误将只有一项的精确型号结果首尾互相比较；已改为
  验证精确匹配分数阈值和独立别名优先场景，随后 44 个测试全部通过。
- 步骤 4 首轮格式检查发现新增测试的一处列表可压缩为单行；已由 Ruff format 修复并
  重新通过格式门禁。
- 步骤 5 首轮功能测试 53 项全部通过，格式检查发现新增测试的一处表达式可压缩为单行；
  已由 Ruff format 修复。复核时补齐了显式 HTTP 客户端超时工厂和 5xx 测试，最终
  55 项测试全部通过。
- 步骤 6 首轮功能测试 64 项全部通过，Ruff 发现 1 处导入顺序和 3 个文件格式问题；
  已自动修复。复核时补齐 429、连接、5xx 重试及拒绝完成事件测试，最终 67 项确定性
  测试通过。
- 步骤 7 首轮新增测试发现测试专用空参数工具不符合项目的严格 Function Schema 规则；
  已为测试工具增加必填字段。随后 79 项确定性测试全部通过。
- 步骤 8 首轮测试有 1 个失败：断言使用裸数字检查 SSE 文本，误匹配到 `item_id` 中的
  同一数字；已改为检查完整 `id:` 行，随后 90 项确定性测试全部通过。
- 步骤 9 全量测试首次复验有 1 个失败：总时限用例设置为 20 ms，在完整测试负载下可能
  在进入挂起 LLM 前超时。已将该测试时限放宽至 200 ms，使其稳定验证 LLM 挂起后的
  Run 总时限终止；生产默认 60 秒未调整，随后 102 项确定性测试全部通过。
- 步骤 10 首轮集成测试发现 SSE 读取器存在终态竞态：读取普通事件后若恰好观察到 Run
  已结束，会在读取同事务提交的终态事件前退出。现已在非 running 状态下执行一次最终
  补读，确保 `run.completed`/`run.failed` 不遗漏；随后全部测试通过。
- 步骤 11 构建验收发现开发依赖未声明 `build`，且 wheel 仅明确包含 SQLite schema，
  提示词和主板数据依赖源码目录。已补充构建依赖、data-files、源码/安装资源路径解析和
  MANIFEST，随后 wheel 干净安装与 sdist/wheel 内容检查均通过。
- 版本 1.1 移动 search 单元测试后，测试内相对数据路径多了一层目录；已改为从测试目录
  显式定位 fixture 和项目根数据集，随后全量测试恢复通过。
- 首次干净 wheel 安装超过单次 30 秒等待窗口；安装进程继续完成后，在非源码目录成功
  导入两个 LLM 适配器并解析 schema、提示词和主板数据，临时环境已删除。
- Vue 首版在长对话完成后无法查看全部消息。原因是 Grid/Flex 中间列缺少完整的
  `min-height: 0`/`overflow` 约束，且会话加载时在占位视图消失前执行了自动滚动。现已将
  workspace、conversation panel 和 message viewport 建立明确的滚动边界，并把定位动作
  移到消息视图渲染之后；实测滚动首尾和重新打开定位均通过。
- 真实 full profile 首轮使用 256 token 输出上限时，calculator 和 search 工具均成功，
  search 后的详细中文答案被正常标记为 `LLM_RESPONSE_INCOMPLETE/max_output_tokens`；这不是
  工具或 SSE 缺陷。已将 full profile 默认上限和示例配置改为 1024，并在相同 8 次调用
  总预算内重跑通过；真实流同时验证了 `reasoning_text/raw` 的 started/delta/done 事件。

## Review 结果

- 步骤 1：2026-10-22 用户确认通过，并要求开始步骤 2。
- 步骤 2：2026-10-22 用户确认通过，并要求开始步骤 3。
- 步骤 3：2026-10-22 用户确认通过，并要求开始步骤 4。
- 步骤 4：2026-10-22 用户确认通过，并要求开始步骤 5。
- 步骤 5：2026-10-22 用户确认通过，并指定步骤 6 使用 DeepSeek API 测试。
- 步骤 6：2026-10-22 用户使用 DeepSeek API 测试通过，并要求开始步骤 7。
- 步骤 7：2026-10-22 用户确认通过，并要求开始步骤 8。
- 步骤 8：2026-10-22 用户确认通过，并要求开始步骤 9。
- 步骤 9：2026-10-22 用户确认通过，并要求开始步骤 10。
- 步骤 10：2026-10-23 用户确认通过，并要求开始步骤 11。
- 步骤 11：2026-10-24 用户提出 Review 修订要求并确认版本 1.1 实施计划；修订已完成，
  等待最终 Review。

## 待解决问题

- 无未解决的实现缺陷。真实 `smoke/full` 测试保持付费 opt-in；本次已使用本地配置完成
  真实调用，凭据未输出、未写入 Trace 或文档。
- 默认 `CONTEXT_WINDOW_TOKENS=0` 是刻意的未配置状态；实际应用装配 AgentLoop 时必须按
  所用模型填写窗口大小，否则 ContextBuilder 会拒绝启动。

## 优化建议 / 待确认

- 2026-10-24 用户确认启用本地 Git；在历史推理回看修复前创建步骤 11 版本 1.1 基线
  提交。仅建立本地版本记录，不连接或推送远程仓库；真实 `.env`、数据库、依赖和构建
  产物继续由 `.gitignore` 排除。
- 当前同步工具通过工作线程执行；超时会立即返回结构化错误，但 Python 无法强制终止
  已运行的线程。calculator 已通过 AST 和资源上限保证有界执行。若未来引入不可信的
  CPU 密集型工具，建议改用独立进程隔离；不影响当前步骤和后续只读工具。
- 当前 TokenCounter 使用无第三方依赖的保守启发式估算，并已设计为可替换协议。若部署
  模型提供稳定的官方 tokenizer，建议替换为精确计数器；不改变 ContextBuilder 接口。

## 计划调整记录

- 2026-10-24，计划版本升级为 1.1。原因：步骤 11 Review 明确要求显示模型实际返回的
  推理、兼容多个模型/API 风格、逐工具测试及通用真实测试。影响：扩展 LLM/SSE 协议、
  配置、测试结构和文档；不迁移数据库、不改变 HTTP 路由。用户已确认执行。
- 写工具幂等持久化延后到首个写工具加入时实现；当前只增加副作用和重试语义契约，避免
  为三个纯计算/只读工具提前创建无用途数据表。
- 2026-10-24，用户在步骤 11 最终 Review 阶段明确要求增加 Vue 前端用于测试。影响：新增
  独立 `frontend/`，只调用既有 HTTP/SSE 契约，不修改数据库、AgentLoop 或后端路由；
  本地开发通过 Vite 代理连接单 worker 后端。

## 实现结构说明

- 设计草案中的流写入职责最终集中在 `streaming/buffer.py` 的 `BufferedEventWriter`，未再
  创建只有转发作用的 `writer.py`；事件协议仍在 `events.py`，重放在 `replay.py`。这是
  文件职责合并，不改变步骤、接口或验收条件。
- `services.py` 承担完整应用服务图和外部客户端生命周期，`dependencies.py` 只保留
  FastAPI 依赖提取，避免路由层自行构造资源。

## 下一步计划

步骤 11 版本 1.1 及真实模型验收已完成并暂停，等待用户 Review。Review 修复仍归属于
步骤 11；未经确认不开始其他工作。

