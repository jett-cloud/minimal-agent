# Minimal Agent Console

用于本地验收 Minimal Agent 的 Vue 3 测试界面。

## 启动

先在项目根目录启动 FastAPI：

```powershell
.venv\Scripts\python -m uvicorn minimal_agent.main:app --host 127.0.0.1 --port 8000 --workers 1
```

再启动前端：

```powershell
cd frontend
npm install
npm run dev
```

打开 `http://127.0.0.1:5173`。开发服务器会把 `/health` 和 `/v1` 请求代理到本地后端，
因此不需要修改 FastAPI 的 CORS 配置。

## 功能

- 创建和切换 Session；
- 查看持久化消息历史；
- 发送消息并实时展示 assistant 和 reasoning 流；
- 查看工具调用和全部标准化 SSE 事件；
- Run 完成后查看状态与 Trace；
- 提供 calculator、search、weather 的快速测试提示词。

如需连接其他同源部署，可在构建时设置 `VITE_API_BASE`。跨域部署仍需在反向代理或
FastAPI 侧配置 CORS。
