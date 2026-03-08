// ============================================================
// 共用型別定義
// ============================================================

export type MessageRole = 'user' | 'assistant' | 'system'

export interface ChatMessage {
  role: MessageRole
  content: string
}

// 前端顯示用的訊息（含 id 和狀態）
export interface DisplayMessage {
  id: string
  role: MessageRole
  content: string
  createdAt: number
  isStreaming?: boolean
  isError?: boolean
}

// streaming 事件
export type StreamEvent =
  | { type: 'delta'; text: string }
  | { type: 'done' }
  | { type: 'error'; message: string }

// 後端 health 回應
export interface HealthStatus {
  status: string
  gateway?: string
  memory_enabled?: boolean
  memory_count?: number
  system_prompt_loaded?: boolean
  system_prompt_length?: number
  memory_extract_interval?: number
}

// 一段聊天紀錄（存 localStorage）
export interface StoredChat {
  id: string
  title: string
  sessionId: string
  messages: DisplayMessage[]
  updatedAt: number
  createdAt: number
  lastActiveAt: number  // 用來判斷 session 是否過期
}
