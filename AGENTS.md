# Minimal Agent 项目指南

## 开工必读

- 本项目是不用 Agent 框架实现的最小可用 ReAct Agent，后端为 Python 3.12、FastAPI、
  Pydantic、SQLite、OpenAI Python SDK，前端为 Vue 3 + Vite。
- 开始修改前先阅读 `docs/design.md`、`docs/progress.md` 和本文件；以实际代码和测试为准。
- 每次代码、配置、接口、数据结构、测试或运行方式发生修改，都必须同步更新本文件的
  “当前状态/最近变更”，并按需更新 README、设计文档、运行手册和进度文档。
- 每次只处理用户确认的当前步骤；完成后停止，等待 Review，不擅自开始下一步。

## 目录与职责

- `src/minimal_agent/agent/`：Context 构建、摘要压缩、ReAct Loop、RunSupervisor。
- `src/minimal_agent/llm/`：通用 LLM 协议、Responses/Chat Completions 流式适配和推理解析。
- `src/minimal_agent/tools/`：工具协议，以及 calculator、search、weather 和订单 CRUD 工具。
- `src/minimal_agent/storage/`：SQLite schema、Repository、订单幂等写操作和短事务。
- `src/minimal_agent/streaming/`：事件模型、8 KiB/100 ms 缓冲、持久化、SSE 重放。
- `src/minimal_agent/api/`：Session、Run、Order、Trace、reasoning 与 SSE HTTP 接口。
- `frontend/`：用于本地验收 Session、消息、推理、工具、事件和 Trace 的 Vue 控制台。
- `tests/`：unit、integration、contract、live；live 默认跳过，只有显式开启才产生费用。

## 核心数据流与约束

1. `POST /v1/sessions/{id}/messages` 原子创建用户消息与 running Run。
2. Agent 仅加载当前 Session 的摘要、消息和工具结果，最多 8 步；不设置 Run 总时限、
   普通模型输出 token 上限或当前工具的统一执行时限。
3. 模型正文、推理和工具参数先进入内存缓冲，再批量写入 `stream_events`；SSE 只读数据库。
4. `reasoning.started/delta/done` 是推理的唯一持久化来源，不复制进最终 assistant 消息。
5. `/reasoning` 从事件表重建历史推理；`/event-history` 分页恢复事件，前端按回合将推理
   放到对应工具调用或最终回答之前，并为最近 Run 恢复事件与 Trace。
6. Session 删除会级联删除 messages、runs、traces、stream_events；有 running Run 时返回 409。
7. 同一 Session 同时最多一个 running Run；不同 Session 可以并行。
8. Context 到模型窗口 80% 时只压缩已完成旧轮次，保留最近 5 轮完整内容。
9. 订单创建、更新和删除使用持久化幂等键；更新和删除以版本号做并发冲突检测。
10. 每次模型工具决策先写入 `tool_invocations`；超时只按
    `safe/with_idempotency_key/never` 声明重试一次，重试和恢复必须复用原参数与原键。

## 主要接口

- `POST/GET /v1/sessions`
- `GET/PATCH/DELETE /v1/sessions/{session_id}`
- `GET/POST /v1/sessions/{session_id}/messages`
- `GET /v1/runs/{run_id}`、`/trace`、`/reasoning`、`/event-history`、`/events`
- `POST/GET /v1/orders`、`GET/PATCH/DELETE /v1/orders/{order_id}`
- `GET /v1/runs/{run_id}/events` 是可断线续传的 SSE，支持 `Last-Event-ID` 和 `after_id`。

## 修改规则

- 不引入 Agent 框架或隐藏的全局注册表；依赖通过显式服务图装配。
- API 输入、工具参数、第三方响应和公共输出都用 Pydantic 验证。
- 网络调用期间不得持有 SQLite 事务；异常必须结构化并写 Trace，禁止无限重试。
- 不输出、记录或提交 API Key；`.env`、数据库、依赖和构建产物必须保持 Git 忽略。
- 新工具必须声明副作用/重试语义，并分别增加单元、Agent 集成和工具契约测试。
- 写工具必须设计持久化幂等键、重复请求、并发冲突、超时结果未知和事务回滚测试。
- 修改 API 后同步更新 OpenAPI 测试、前端调用、README、设计与运行文档。
- 修改前端后执行 `npm run build`；修改后端后执行 pytest、Ruff 和 Python build。
- 本地服务推荐使用 `uvicorn minimal_agent.main:app --reload`，否则代码更新后必须重启后端。

## 常用验证命令

```powershell
.venv\Scripts\python -m pytest -ra
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
.venv\Scripts\python -m build
cd frontend
npm run build
```

## 当前状态 / 最近变更

- 当前实现已完成并通过步骤 13 Review：包含 Session 管理、可恢复 SSE、推理历史、订单
  CRUD、工具幂等重试与进程恢复，以及 Vue 本地验收控制台。
- 2026-10-26 发布前全量验证通过：`174 passed, 2 skipped`；Ruff lint/format、Python
  sdist/wheel 构建和 Vue 生产构建均通过。两个跳过项为需显式开启的付费 live 测试。
- 本地 `.env`、SQLite 数据库、运行日志、依赖目录和构建产物保持 Git 忽略，不进入仓库。
