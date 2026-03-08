// ============================================================
// api.ts — 所有後端呼叫集中在這裡
// UI 層不直接 fetch，也不解析 SSE
// ============================================================

import type { ChatMessage, HealthStatus, StreamEvent } from './types'

export const API_BASE_LS_KEY    = 'myloverM-api-base-url'
export const MODEL_LS_KEY       = 'myloverM-model'
export const CONTEXT_TURNS_LS_KEY = 'myloverM-context-turns'

function getBaseUrl(): string {
  try {
    const saved = localStorage.getItem(API_BASE_LS_KEY)
    if (saved && saved.trim()) return saved.trim().replace(/\/$/, '')
  } catch { /* 非瀏覽器環境 */ }
  const env = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? ''
  return env.replace(/\/$/, '')
}

function apiUrl(path: string): string {
  const base = getBaseUrl()
  return base ? `${base}${path}` : path
}

// ────────────────────────────────────────────────────────────
// Health check
// ────────────────────────────────────────────────────────────

export async function checkHealth(): Promise<HealthStatus> {
  const res = await fetch(apiUrl('/'))
  if (!res.ok) throw new Error(`Health check failed: ${res.status}`)
  return res.json() as Promise<HealthStatus>
}

// 取得後端設定的 model 名稱
export async function fetchBackendModel(): Promise<string> {
  const res = await fetch(apiUrl('/v1/models'))
  if (!res.ok) return ''
  const data = await res.json() as { data?: Array<{ id?: string }> }
  return data?.data?.[0]?.id ?? ''
}

// ────────────────────────────────────────────────────────────
// Chat（非 streaming，跟測試頁一樣）
// ────────────────────────────────────────────────────────────

export async function* streamChat(input: {
  model?: string   // 空字串或不傳 → 讓後端用 DEFAULT_MODEL
  messages: ChatMessage[]
  session_id: string
}): AsyncGenerator<StreamEvent, void, unknown> {

  const body: Record<string, unknown> = {
    messages: input.messages,
    session_id: input.session_id,
    stream: false,
  }
  // 只有明確指定 model 才帶進去，讓後端用自己的 DEFAULT_MODEL
  if (input.model && input.model.trim()) {
    body.model = input.model.trim()
  }

  let res: Response
  try {
    res = await fetch(apiUrl('/v1/chat/completions'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch {
    yield { type: 'error', message: '無法連線到後端，請確認服務是否啟動。' }
    return
  }

  if (!res.ok) {
    let detail = ''
    try {
      const data = await res.json() as { error?: unknown; detail?: unknown }
      const raw = data?.error ?? data?.detail ?? ''
      detail = typeof raw === 'string' ? raw : JSON.stringify(raw)
    } catch { /* ignore */ }
    yield { type: 'error', message: `後端回傳錯誤 ${res.status}${detail ? `：${detail}` : ''}` }
    return
  }

  let data: { choices?: Array<{ message?: { content?: string } }> }
  try {
    data = await res.json() as typeof data
  } catch {
    yield { type: 'error', message: '回應解析失敗。' }
    return
  }

  const content = data?.choices?.[0]?.message?.content ?? ''
  if (content) {
    yield { type: 'delta', text: content }
  }
  yield { type: 'done' }
}
