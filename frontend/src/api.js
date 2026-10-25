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
  updateSession: (sessionId, title) =>
    request(`/v1/sessions/${sessionId}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),
  deleteSession: (sessionId) => request(`/v1/sessions/${sessionId}`, { method: "DELETE" }),
  listOrders: ({ status, limit = 100, offset = 0 } = {}) => {
    const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
    if (status) query.set("status", status)
    return request(`/v1/orders?${query}`)
  },
  getOrder: (orderId) => request(`/v1/orders/${orderId}`),
  createOrder: (payload, idempotencyKey) =>
    request("/v1/orders", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(payload),
    }),
  updateOrder: (orderId, payload, idempotencyKey) =>
    request(`/v1/orders/${orderId}`, {
      method: "PATCH",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(payload),
    }),
  deleteOrder: (orderId, expectedVersion, idempotencyKey) =>
    request(`/v1/orders/${orderId}?expected_version=${expectedVersion}`, {
      method: "DELETE",
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  listMessages: (sessionId) => request(`/v1/sessions/${sessionId}/messages?limit=1000`),
  createMessage: (sessionId, text) =>
    request(`/v1/sessions/${sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  getRun: (runId) => request(`/v1/runs/${runId}`),
  getTrace: (runId) => request(`/v1/runs/${runId}/trace`),
  getReasoning: (runId) => request(`/v1/runs/${runId}/reasoning`),
  getEventHistory: (runId, afterId = 0, limit = 1000) =>
    request(`/v1/runs/${runId}/event-history?after_id=${afterId}&limit=${limit}`),
  eventUrl: (runId, afterId = 0) => {
    const suffix = afterId > 0 ? `?after_id=${afterId}` : ""
    return `${API_BASE}/v1/runs/${runId}/events${suffix}`
  },
}
