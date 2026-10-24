const API_BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "")

export class ApiError extends Error {
  constructor(message, { code = "REQUEST_FAILED", status = 0, details = {} } = {}) {
    super(message)
    this.name = "ApiError"
    this.code = code
    this.status = status
    this.details = details
  }
}

async function request(path, options = {}) {
  let response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      headers: options.body ? { "Content-Type": "application/json", ...options.headers } : options.headers,
      ...options,
    })
  } catch (error) {
    throw new ApiError("无法连接后端服务，请确认 FastAPI 已在 8000 端口启动。", {
      code: "NETWORK_ERROR",
      details: { cause: error instanceof Error ? error.message : String(error) },
    })
  }

  const contentType = response.headers.get("content-type") || ""
  const body = contentType.includes("application/json") ? await response.json() : await response.text()
  if (!response.ok) {
    const payload = body?.error
    throw new ApiError(payload?.message || `请求失败（HTTP ${response.status}）`, {
      code: payload?.code || "HTTP_ERROR",
      status: response.status,
      details: payload?.details || {},
    })
  }
  return body
}

export const api = {
  health: () => request("/health"),
  listSessions: () => request("/v1/sessions?limit=200"),
  createSession: (title) =>
    request("/v1/sessions", {
      method: "POST",
      body: JSON.stringify(title?.trim() ? { title: title.trim() } : {}),
    }),
  listMessages: (sessionId) => request(`/v1/sessions/${sessionId}/messages?limit=1000`),
  createMessage: (sessionId, text) =>
    request(`/v1/sessions/${sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  getRun: (runId) => request(`/v1/runs/${runId}`),
  getTrace: (runId) => request(`/v1/runs/${runId}/trace`),
  eventUrl: (runId) => `${API_BASE}/v1/runs/${runId}/events`,
}
