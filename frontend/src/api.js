/** 后端接口封装。SSE 用 fetch + ReadableStream 手动解析,因为要 POST。 */

async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail || body.error || detail
    } catch (e) { /* 忽略解析失败 */ }
    throw new Error(`${res.status} ${detail}`)
  }
  return res.status === 204 ? null : res.json()
}

export const api = {
  health: () => request('/api/health'),
  status: () => request('/api/demo/status'),
  scenarios: () => request('/api/demo/scenarios'),
  workflows: () => request('/api/workflows'),

  listConversations: () => request('/api/conversations'),
  getConversation: (id) => request(`/api/conversations/${id}`),
  deleteConversation: (id) => request(`/api/conversations/${id}`, { method: 'DELETE' }),

  getTask: (id) => request(`/api/tasks/${id}`),
  updateTask: (id, body) => request(`/api/tasks/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),

  listFollowups: (taskId) => request(`/api/followups?task_id=${taskId}`),
  fireFollowup: (id) => request(`/api/followups/${id}/fire`, { method: 'POST' }),
  cancelFollowup: (id) => request(`/api/followups/${id}`, { method: 'DELETE' }),

  getClock: () => request('/api/demo/clock'),
  advanceClock: (hours) => request('/api/demo/clock/advance', { method: 'POST', body: JSON.stringify({ hours }) }),
  resetClock: () => request('/api/demo/clock/reset', { method: 'POST' }),
  resetAll: () => request('/api/demo/reset', { method: 'POST' }),

  searchKnowledge: (q, topK = 5) => request(`/api/tools/knowledge/search?q=${encodeURIComponent(q)}&top_k=${topK}`),
  knowledgeDocs: () => request('/api/tools/knowledge/docs'),
  estimateShipping: (body) => request('/api/tools/shipping/estimate', { method: 'POST', body: JSON.stringify(body) }),
  tracking: (no, company) => request(`/api/tools/tracking?tracking_no=${encodeURIComponent(no)}${company ? `&company=${encodeURIComponent(company)}` : ''}`),

  materialExportUrl: (id) => `/api/materials/${id}/export?fmt=md`,
  taskExportUrl: (id, fmt = 'md') => `/api/tasks/${id}/export?fmt=${fmt}`,
}

/** 发起一轮对话,逐个事件回调。返回一个可中止的句柄。 */
export function streamChat({ conversationId, message, agentType }, onEvent) {
  const controller = new AbortController()
  const done = (async () => {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: conversationId, message, agent_type: agentType }),
      signal: controller.signal,
    })
    if (!res.ok || !res.body) throw new Error(`对话接口出错:${res.status}`)
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done: finished, value } = await reader.read()
      if (finished) break
      buffer += decoder.decode(value, { stream: true })
      const parts = buffer.split('\n\n')
      buffer = parts.pop() ?? ''
      for (const part of parts) {
        const line = part.split('\n').find((l) => l.startsWith('data: '))
        if (!line) continue
        try {
          onEvent(JSON.parse(line.slice(6)))
        } catch (e) {
          console.warn('SSE 解析失败', line, e)
        }
      }
    }
  })()
  return { abort: () => controller.abort(), done }
}

/** 全局事件流:后台主动跟进触发时靠它推过来。 */
export function connectEvents(onEvent) {
  const source = new EventSource('/api/events')
  source.onmessage = (e) => {
    try {
      onEvent(JSON.parse(e.data))
    } catch (err) { /* keepalive 行 */ }
  }
  source.onerror = () => { /* EventSource 自己会重连 */ }
  return source
}
