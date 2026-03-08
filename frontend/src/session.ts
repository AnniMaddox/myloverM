// ============================================================
// session.ts — session_id 生命週期 + 聊天列表 localStorage
// 所有 session_id 的生成、切換、判斷都在這裡
// ============================================================

import type { StoredChat, DisplayMessage } from './types'

const SESSION_TIMEOUT_MS = 30 * 60 * 1000  // 30 分鐘
const STORAGE_KEY = 'myloverM_chats'
const MAX_CHATS = 50  // 最多保留幾段聊天（避免 localStorage 爆掉）

// ────────────────────────────────────────────────────────────
// Session ID
// ────────────────────────────────────────────────────────────

export function createSessionId(): string {
  return crypto.randomUUID()
}

export function isSessionExpired(
  lastActiveAt: number,
  timeoutMs: number = SESSION_TIMEOUT_MS,
): boolean {
  return Date.now() - lastActiveAt > timeoutMs
}

// ────────────────────────────────────────────────────────────
// 聊天列表 localStorage
// ────────────────────────────────────────────────────────────

export function loadChats(): StoredChat[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as StoredChat[]
    if (!Array.isArray(parsed)) return []
    return parsed
  } catch {
    return []
  }
}

/**
 * 聊天列表約定：newest-first。
 * 新增聊天用 unshift（加到最前面），更新後重新排序也是最新在前。
 * 所以 slice(0, MAX_CHATS) = 保留最新 50 筆，這是正確的。
 * App.tsx 接手時請維持這個約定。
 */
export function saveChats(chats: StoredChat[]): void {
  try {
    const trimmed = chats.slice(0, MAX_CHATS)
    localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed))
  } catch {
    // localStorage 滿了就算了，不要 crash
  }
}

// ────────────────────────────────────────────────────────────
// 操作
// ────────────────────────────────────────────────────────────

export function createNewChat(): StoredChat {
  const now = Date.now()
  return {
    id: crypto.randomUUID(),
    title: '新對話',
    sessionId: createSessionId(),
    messages: [],
    createdAt: now,
    updatedAt: now,
    lastActiveAt: now,
  }
}

/** 根據第一條 user 訊息自動生成標題（截斷到 30 字） */
export function deriveChatTitle(messages: DisplayMessage[]): string {
  const first = messages.find((m) => m.role === 'user')
  if (!first) return '新對話'
  const text = first.content.trim().replace(/\n/g, ' ')
  return text.length > 30 ? text.slice(0, 30) + '…' : text
}

/**
 * 送出訊息前呼叫。
 * 如果 session 已過期，自動產生新的 session_id。
 * 回傳「這次要用的 session_id」和「是否換了新的」。
 */
export function resolveSessionId(chat: StoredChat): {
  sessionId: string
  renewed: boolean
} {
  if (isSessionExpired(chat.lastActiveAt)) {
    return { sessionId: createSessionId(), renewed: true }
  }
  return { sessionId: chat.sessionId, renewed: false }
}

/** 更新 chat 的 lastActiveAt 和 updatedAt（每次送出訊息後呼叫） */
export function touchChat(
  chats: StoredChat[],
  chatId: string,
  newSessionId?: string,
): StoredChat[] {
  const now = Date.now()
  return chats.map((c) => {
    if (c.id !== chatId) return c
    return {
      ...c,
      sessionId: newSessionId ?? c.sessionId,
      updatedAt: now,
      lastActiveAt: now,
    }
  })
}

/** 把新訊息存入指定的 chat，並更新標題 */
export function updateChatMessages(
  chats: StoredChat[],
  chatId: string,
  messages: DisplayMessage[],
): StoredChat[] {
  return chats.map((c) => {
    if (c.id !== chatId) return c
    return {
      ...c,
      messages,
      title: deriveChatTitle(messages),
      updatedAt: Date.now(),
    }
  })
}
