<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue"
import DOMPurify from "dompurify"
import { marked } from "marked"
import { ApiError, api } from "./api"

marked.setOptions({ breaks: true, gfm: true })

const EVENT_NAMES = [
  "run.started",
  "turn.started",
  "turn.completed",
  "reasoning.started",
  "reasoning.delta",
  "reasoning.done",
  "assistant.delta",
  "assistant.done",
  "tool_call.delta",
  "tool_call.ready",
  "tool.started",
  "tool.completed",
  "tool.failed",
  "run.completed",
  "run.failed",
]

const quickPrompts = [
  { label: "计算器", text: "@calculator 请计算 (128 + 72) * 3 / 5" },
  { label: "主板搜索", text: "@search 帮我找支持 Wi-Fi 6E 的 B650 Mini-ITX 主板" },
  { label: "天气", text: "@weather 查询上海当前天气和未来两天预报" },
  { label: "连续工具", text: "先计算 144 的平方根，再搜索一款 B650 主板并总结结果。" },
]

const sessions = ref([])
const selectedSessionId = ref("")
const messages = ref([])
const events = ref([])
const traces = ref([])
const activeRun = ref(null)
const draft = ref("")
const newSessionTitle = ref("")
const health = ref({ state: "checking", label: "检查中" })
const loadingSessions = ref(false)
const loadingMessages = ref(false)
const creatingSession = ref(false)
const sending = ref(false)
const error = ref(null)
const diagnosticsTab = ref("events")
const sidebarOpen = ref(false)
const diagnosticsOpen = ref(false)
const messageViewport = ref(null)
const scrollPinned = ref(true)
const showJumpToLatest = ref(false)
let eventSource = null

const selectedSession = computed(() =>
  sessions.value.find((session) => session.id === selectedSessionId.value),
)
const sessionGroups = computed(() => {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const buckets = { today: [], yesterday: [], week: [], month: [], older: [] }
  for (const session of sessions.value) {
    const updated = new Date(session.updated_at)
    const day = new Date(updated.getFullYear(), updated.getMonth(), updated.getDate())
    const difference = Math.floor((today - day) / 86_400_000)
    const key = difference <= 0 ? "today" : difference === 1 ? "yesterday" : difference < 7 ? "week" : difference < 30 ? "month" : "older"
    buckets[key].push(session)
  }
  return [
    ["today", "今天"],
    ["yesterday", "昨天"],
    ["week", "近 7 天"],
    ["month", "近 30 天"],
    ["older", "更早"],
  ]
    .filter(([key]) => buckets[key].length)
    .map(([key, label]) => ({ key, label, items: buckets[key] }))
})
const runIsActive = computed(() => activeRun.value?.status === "running")
const assistantStream = computed(() =>
  events.value
    .filter((event) => event.type === "assistant.delta")
    .map((event) => event.data.delta || "")
    .join(""),
)
const reasoningStream = computed(() =>
  events.value
    .filter((event) => event.type === "reasoning.delta")
    .map((event) => event.data.delta || "")
    .join(""),
)
const eventStats = computed(() => ({
  turns: events.value.filter((event) => event.type === "turn.started").length,
  tools: events.value.filter((event) => event.type === "tool.started").length,
  total: events.value.length,
}))

function formatTime(value) {
  if (!value) return "—"
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value))
}

function shortId(value) {
  if (!value) return "—"
  return value.length > 15 ? `${value.slice(0, 7)}…${value.slice(-5)}` : value
}

function messageText(message) {
  if (message.kind === "message") return message.content?.text || ""
  if (message.kind === "tool_call") return `${message.content?.name || "tool"}(${message.content?.arguments || ""})`
  if (message.kind === "tool_result") return JSON.stringify(message.content?.result ?? message.content, null, 2)
  return JSON.stringify(message.content, null, 2)
}

function renderMarkdown(value) {
  return DOMPurify.sanitize(marked.parse(String(value || "")))
}

function toolName(message) {
  if (message.content?.name) return message.content.name
  const callId = message.content?.call_id
  if (!callId) return "tool"
  const matchingCall = messages.value.find(
    (item) => item.kind === "tool_call" && item.content?.call_id === callId,
  )
  return matchingCall?.content?.name || "tool"
}

function toolSucceeded(message) {
  if (message.kind !== "tool_result") return null
  return message.content?.result?.ok !== false
}

function toolBadge(message) {
  if (message.kind === "tool_call") return "Requested"
  return toolSucceeded(message) ? "Completed" : "Error"
}

function eventLabel(type) {
  const labels = {
    "run.started": "运行开始",
    "turn.started": "回合开始",
    "turn.completed": "回合完成",
    "reasoning.started": "推理开始",
    "reasoning.delta": "推理增量",
    "reasoning.done": "推理完成",
    "assistant.delta": "回答增量",
    "assistant.done": "回答完成",
    "tool_call.delta": "工具参数",
    "tool_call.ready": "工具就绪",
    "tool.started": "工具开始",
    "tool.completed": "工具完成",
    "tool.failed": "工具失败",
    "run.completed": "运行完成",
    "run.failed": "运行失败",
  }
  return labels[type] || type
}

function eventTone(type) {
  if (type.includes("failed")) return "danger"
  if (type.startsWith("tool.")) return "amber"
  if (type.startsWith("reasoning.")) return "violet"
  if (type.startsWith("assistant.")) return "cyan"
  if (type === "run.completed") return "green"
  return "slate"
}

function setError(caught) {
  if (caught instanceof ApiError) {
    error.value = { title: caught.code, message: caught.message }
  } else {
    error.value = { title: "UNEXPECTED_ERROR", message: caught?.message || String(caught) }
  }
}

function clearError() {
  error.value = null
}

async function checkHealth() {
  try {
    const result = await api.health()
    health.value = { state: "online", label: `${result.service} · ${result.version}` }
  } catch {
    health.value = { state: "offline", label: "后端未连接" }
  }
}

async function loadSessions({ selectFirst = true } = {}) {
  loadingSessions.value = true
  try {
    sessions.value = await api.listSessions()
    if (selectFirst && !selectedSessionId.value && sessions.value.length) {
      await selectSession(sessions.value[0].id)
    }
  } catch (caught) {
    setError(caught)
  } finally {
    loadingSessions.value = false
  }
}

async function createSession() {
  if (creatingSession.value) return
  creatingSession.value = true
  clearError()
  try {
    const session = await api.createSession(newSessionTitle.value)
    newSessionTitle.value = ""
    sessions.value = [session, ...sessions.value.filter((item) => item.id !== session.id)]
    await selectSession(session.id)
  } catch (caught) {
    setError(caught)
  } finally {
    creatingSession.value = false
  }
}

async function selectSession(sessionId) {
  closeStream()
  selectedSessionId.value = sessionId
  messages.value = []
  events.value = []
  traces.value = []
  activeRun.value = null
  scrollPinned.value = true
  showJumpToLatest.value = false
  sidebarOpen.value = false
  loadingMessages.value = true
  clearError()
  try {
    messages.value = await api.listMessages(sessionId)
  } catch (caught) {
    setError(caught)
  } finally {
    loadingMessages.value = false
    await scrollMessages(true)
  }
}

async function submitMessage() {
  const text = draft.value.trim()
  if (!text || !selectedSessionId.value || sending.value || runIsActive.value) return
  sending.value = true
  clearError()
  events.value = []
  traces.value = []
  diagnosticsTab.value = "events"
  try {
    const accepted = await api.createMessage(selectedSessionId.value, text)
    draft.value = ""
    messages.value = await api.listMessages(selectedSessionId.value)
    activeRun.value = {
      id: accepted.run_id,
      status: accepted.status,
      loop_count: 0,
      started_at: new Date().toISOString(),
    }
    connectStream(accepted.run_id)
    await scrollMessages(true)
  } catch (caught) {
    setError(caught)
  } finally {
    sending.value = false
  }
}

function connectStream(runId) {
  closeStream()
  const source = new EventSource(api.eventUrl(runId))
  eventSource = source

  for (const type of EVENT_NAMES) {
    source.addEventListener(type, async (rawEvent) => {
      let data = {}
      try {
        data = JSON.parse(rawEvent.data)
      } catch {
        data = { raw: rawEvent.data }
      }
      events.value.push({ id: rawEvent.lastEventId, type, data, receivedAt: new Date().toISOString() })
      if (type === "assistant.delta" || type === "assistant.done" || type.startsWith("tool.")) {
        await scrollMessages()
      }
      if (type === "run.completed" || type === "run.failed") {
        closeStream()
        await finishRun(runId)
      }
    })
  }

  source.onopen = () => {
    if (health.value.state === "offline" || health.value.state === "warning") {
      health.value = { state: "online", label: "后端已连接" }
    }
  }
  source.onerror = () => {
    if (activeRun.value?.status === "running") {
      health.value = { state: "warning", label: "SSE 正在重连" }
    }
  }
}

async function finishRun(runId) {
  try {
    const [run, runTraces, runMessages] = await Promise.all([
      api.getRun(runId),
      api.getTrace(runId),
      api.listMessages(selectedSessionId.value),
    ])
    activeRun.value = run
    traces.value = runTraces
    messages.value = runMessages
    await loadSessions({ selectFirst: false })
    await checkHealth()
    await scrollMessages(true)
  } catch (caught) {
    setError(caught)
  }
}

async function refreshDiagnostics() {
  if (!activeRun.value?.id) return
  try {
    const [run, runTraces] = await Promise.all([
      api.getRun(activeRun.value.id),
      api.getTrace(activeRun.value.id),
    ])
    activeRun.value = run
    traces.value = runTraces
  } catch (caught) {
    setError(caught)
  }
}

function usePrompt(text) {
  draft.value = text
}

function handleComposerKeydown(event) {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault()
    submitMessage()
  }
}

function closeStream() {
  eventSource?.close()
  eventSource = null
}

function handleMessageScroll() {
  const viewport = messageViewport.value
  if (!viewport) return
  scrollPinned.value = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight <= 48
  if (scrollPinned.value) showJumpToLatest.value = false
}

async function scrollMessages(force = false) {
  await nextTick()
  const viewport = messageViewport.value
  if (!viewport) return
  if (force || scrollPinned.value) {
    viewport.scrollTop = viewport.scrollHeight
    scrollPinned.value = true
    showJumpToLatest.value = false
  } else {
    showJumpToLatest.value = true
  }
}

function jumpToLatest() {
  scrollMessages(true)
}

onMounted(async () => {
  await Promise.all([checkHealth(), loadSessions()])
})

onBeforeUnmount(closeStream)
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <button class="icon-button mobile-only" type="button" aria-label="打开会话列表" @click="sidebarOpen = true">
        <span></span><span></span><span></span>
      </button>
      <div class="brand-lockup">
        <div class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></div>
        <div>
          <p class="eyebrow">LOCAL TEST CONSOLE</p>
          <h1>Minimal Agent</h1>
        </div>
      </div>
      <div class="topbar-actions">
        <div class="health-pill" :class="health.state">
          <span class="status-dot"></span>
          <span>{{ health.label }}</span>
        </div>
        <button class="icon-button diagnostics-toggle" type="button" aria-label="切换诊断面板" @click="diagnosticsOpen = !diagnosticsOpen">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 19V9m5 10V5m6 14v-7m5 7V3" /></svg>
        </button>
      </div>
    </header>

    <div class="workspace" :class="{ 'diagnostics-collapsed': !diagnosticsOpen }">
      <aside class="session-sidebar" :class="{ open: sidebarOpen }">
        <div class="sidebar-mobile-head">
          <strong>会话</strong>
          <button class="plain-button" type="button" @click="sidebarOpen = false">关闭</button>
        </div>
        <form class="new-session" @submit.prevent="createSession">
          <label for="session-title">新建测试会话</label>
          <div class="input-row">
            <input id="session-title" v-model="newSessionTitle" maxlength="200" placeholder="例如：工具链测试" />
            <button type="submit" :disabled="creatingSession" aria-label="创建会话">+</button>
          </div>
        </form>

        <div class="section-heading">
          <span>历史</span>
          <button type="button" @click="loadSessions({ selectFirst: false })">刷新</button>
        </div>
        <div class="session-list" role="list">
          <div v-if="loadingSessions && !sessions.length" class="quiet-state">正在读取会话…</div>
          <section v-for="group in sessionGroups" :key="group.key" class="session-group">
            <p class="session-group-label">{{ group.label }}</p>
            <button
              v-for="session in group.items"
              :key="session.id"
              class="session-card"
              :class="{ active: session.id === selectedSessionId }"
              type="button"
              @click="selectSession(session.id)"
            >
              <span class="session-title">{{ session.title || "未命名会话" }}</span>
              <span class="session-meta"><time>{{ formatTime(session.updated_at) }}</time></span>
            </button>
          </section>
          <div v-if="!loadingSessions && !sessions.length" class="empty-sidebar">
            <span>01</span>
            <p>还没有会话<br />从上方创建一个开始测试</p>
          </div>
        </div>
        <div class="sidebar-foot">
          <span>SQLite 持久化</span>
          <span>单 Session 单 Run</span>
        </div>
      </aside>
      <button v-if="sidebarOpen" class="sidebar-scrim" type="button" aria-label="关闭会话列表" @click="sidebarOpen = false"></button>

      <main class="conversation-panel">
        <div class="conversation-head">
          <div>
            <p class="eyebrow">SESSION</p>
            <h2>{{ selectedSession?.title || (selectedSession ? "未命名会话" : "选择一个会话") }}</h2>
          </div>
          <div v-if="selectedSession" class="session-badges">
            <span>{{ messages.length }} 条消息</span>
            <span v-if="selectedSession.summary_through_seq">已摘要至 #{{ selectedSession.summary_through_seq }}</span>
          </div>
        </div>

        <div ref="messageViewport" class="message-viewport" tabindex="0" @scroll="handleMessageScroll">
          <div v-if="!selectedSession" class="welcome-state">
            <div class="orb"><span></span></div>
            <p class="eyebrow">READY WHEN YOU ARE</p>
            <h2>创建一个会话，开始验证 Agent</h2>
            <p>你可以观察模型回答、推理流、工具调用、持久化事件以及完整 Trace。</p>
            <button type="button" @click="createSession">创建默认会话</button>
          </div>

          <div v-else-if="loadingMessages" class="loading-state"><span></span>正在加载消息</div>

          <div v-else class="message-stack">
            <div v-if="!messages.length && !runIsActive" class="starter-panel">
              <p class="eyebrow">TEST RECIPES</p>
              <h3>选择一个场景，或自由输入</h3>
              <div class="prompt-grid">
                <button v-for="prompt in quickPrompts" :key="prompt.label" type="button" @click="usePrompt(prompt.text)">
                  <span>{{ prompt.label }}</span>
                  <p>{{ prompt.text }}</p>
                </button>
              </div>
            </div>

            <article v-for="message in messages" :key="message.id" class="message" :class="[message.role, message.kind]">
              <div v-if="message.role === 'assistant' && message.kind === 'message'" class="message-avatar">✦</div>
              <div class="message-body">
                <div v-if="message.kind === 'message'" class="message-meta">
                  <strong>{{ message.role === "user" ? "你" : message.role === "assistant" ? "Minimal Agent" : "工具结果" }}</strong>
                  <span>#{{ message.seq }} · {{ formatTime(message.created_at) }}</span>
                </div>
                <p v-if="message.kind === 'message' && message.role === 'user'" class="message-copy">{{ messageText(message) }}</p>
                <div
                  v-else-if="message.kind === 'message'"
                  class="message-copy markdown-body"
                  v-html="renderMarkdown(messageText(message))"
                ></div>
                <details v-else class="tool-card" :open="toolSucceeded(message) === false">
                  <summary>
                    <span class="tool-icon">⚒</span>
                    <span class="tool-name">{{ toolName(message) }}</span>
                    <span class="tool-badge" :class="{ error: toolSucceeded(message) === false }">{{ toolBadge(message) }}</span>
                  </summary>
                  <pre>{{ messageText(message) }}</pre>
                </details>
              </div>
            </article>

            <article v-if="runIsActive && (reasoningStream || assistantStream)" class="message assistant streaming">
              <div class="message-avatar">AI</div>
              <div class="message-body">
                <div class="message-meta"><strong>Minimal Agent</strong><span class="live-label">LIVE</span></div>
                <details v-if="reasoningStream" class="reasoning-preview">
                  <summary>模型推理</summary>
                  <p>{{ reasoningStream }}</p>
                </details>
                <p v-if="assistantStream">{{ assistantStream }}<span class="cursor"></span></p>
                <div v-else class="thinking"><span></span><span></span><span></span></div>
              </div>
            </article>

            <div v-else-if="runIsActive" class="run-progress">
              <span class="spinner"></span>
              <div><strong>Agent 正在处理</strong><small>等待第一个持久化流事件…</small></div>
            </div>
          </div>
          <button v-if="showJumpToLatest" class="jump-latest" type="button" @click="jumpToLatest">
            跳到最新消息 ↓
          </button>
        </div>

        <div v-if="error" class="error-banner" role="alert">
          <div><strong>{{ error.title }}</strong><span>{{ error.message }}</span></div>
          <button type="button" aria-label="关闭错误" @click="clearError">×</button>
        </div>

        <div class="composer-wrap">
          <div v-if="selectedSession" class="quick-row">
            <button v-for="prompt in quickPrompts.slice(0, 3)" :key="prompt.label" type="button" :disabled="runIsActive" @click="usePrompt(prompt.text)">
              @{{ prompt.label }}
            </button>
          </div>
          <div class="composer" :class="{ disabled: !selectedSession }">
            <textarea
              v-model="draft"
              rows="1"
              :disabled="!selectedSession || runIsActive"
              :placeholder="selectedSession ? (runIsActive ? '当前 Run 执行中…' : '输入消息，Enter 发送，Shift + Enter 换行') : '请先创建或选择会话'"
              @keydown="handleComposerKeydown"
            ></textarea>
            <button class="send-button" type="button" :disabled="!draft.trim() || !selectedSession || sending || runIsActive" @click="submitMessage">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 14-7-4 14-3-5-7-2Z" /><path d="m12 14 7-9" /></svg>
              <span>{{ sending ? "发送中" : "发送" }}</span>
            </button>
          </div>
          <p class="composer-note">消息、工具结果和事件会写入本地 SQLite；真实模型调用可能产生费用。</p>
        </div>
      </main>

      <aside class="diagnostics-panel" :class="{ open: diagnosticsOpen }">
        <div class="diagnostics-head">
          <div><p class="eyebrow">OBSERVABILITY</p><h2>运行诊断</h2></div>
          <button class="plain-button" type="button" :disabled="!activeRun" @click="refreshDiagnostics">刷新</button>
        </div>

        <div v-if="activeRun" class="run-card" :class="activeRun.status">
          <div class="run-card-head"><span class="status-dot"></span><strong>{{ activeRun.status }}</strong><code>{{ shortId(activeRun.id) }}</code></div>
          <div class="run-stats">
            <div><strong>{{ activeRun.loop_count ?? eventStats.turns }}</strong><span>模型回合</span></div>
            <div><strong>{{ eventStats.tools }}</strong><span>工具调用</span></div>
            <div><strong>{{ eventStats.total }}</strong><span>SSE 事件</span></div>
          </div>
          <p v-if="activeRun.error_code" class="run-error"><strong>{{ activeRun.error_code }}</strong>{{ activeRun.error_message }}</p>
        </div>

        <div class="diagnostic-tabs" role="tablist">
          <button type="button" :class="{ active: diagnosticsTab === 'events' }" @click="diagnosticsTab = 'events'">事件 <span>{{ events.length }}</span></button>
          <button type="button" :class="{ active: diagnosticsTab === 'trace' }" @click="diagnosticsTab = 'trace'">Trace <span>{{ traces.length }}</span></button>
        </div>

        <div class="diagnostic-scroll">
          <div v-if="diagnosticsTab === 'events'">
            <div v-if="!events.length" class="diagnostic-empty"><span>⌁</span><p>发送一条消息后，持久化 SSE 事件会显示在这里。</p></div>
            <details v-for="event in events" :key="`${event.id}-${event.type}`" class="event-row">
              <summary>
                <span class="event-index">{{ event.id || "·" }}</span>
                <span class="event-dot" :class="eventTone(event.type)"></span>
                <span class="event-name"><strong>{{ eventLabel(event.type) }}</strong><code>{{ event.type }}</code></span>
                <time>{{ formatTime(event.receivedAt).slice(-8) }}</time>
              </summary>
              <pre>{{ JSON.stringify(event.data, null, 2) }}</pre>
            </details>
          </div>

          <div v-else>
            <div v-if="!traces.length" class="diagnostic-empty"><span>⌁</span><p>Run 完成后会自动载入 Trace，也可以手动刷新。</p></div>
            <details v-for="trace in traces" :key="trace.id" class="event-row trace-row">
              <summary>
                <span class="event-index">{{ trace.step_no }}</span>
                <span class="event-dot slate"></span>
                <span class="event-name"><strong>{{ trace.event_type }}</strong><code>trace #{{ trace.id }}</code></span>
                <time>{{ trace.duration_ms == null ? "—" : `${trace.duration_ms}ms` }}</time>
              </summary>
              <pre>{{ JSON.stringify(trace.payload, null, 2) }}</pre>
            </details>
          </div>
        </div>
      </aside>
    </div>
  </div>
</template>
