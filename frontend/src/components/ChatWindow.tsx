import { useState, useCallback, useEffect } from 'react'
import MessageList from './MessageList'
import Composer from './Composer'
import { streamChat, MODEL_LS_KEY, CONTEXT_TURNS_LS_KEY, fetchBackendModel } from '../api'
import { resolveSessionId } from '../session'
import type { StoredChat, DisplayMessage } from '../types'

function getModel(): string {
  try {
    const saved = localStorage.getItem(MODEL_LS_KEY)
    if (saved && saved.trim()) return saved.trim()
  } catch { /* ignore */ }
  return ''
}

function getContextTurns(): number {
  try {
    const saved = localStorage.getItem(CONTEXT_TURNS_LS_KEY)
    if (saved) {
      const n = parseInt(saved, 10)
      if (!isNaN(n) && n > 0) return n
    }
  } catch { /* ignore */ }
  return 0 // 0 = 不截斷
}

function handleBack() {
  if (window.history.length > 1) {
    window.history.back()
  } else {
    window.location.href = '/Our-love/'
  }
}

interface Props {
  onOpenSidebar: () => void
  onOpenPanel: () => void
  activeChat: StoredChat | null
  onUpdateChat: (chatId: string, messages: DisplayMessage[], sessionId?: string) => void
}

export default function ChatWindow({ onOpenSidebar, onOpenPanel, activeChat, onUpdateChat }: Props) {
  const [input, setInput] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [displayModel, setDisplayModel] = useState<string>(() => {
    const saved = getModel()
    return saved ? saved.split('/').pop() ?? saved : ''
  })

  // 若 localStorage 沒有 model，嘗試從後端撈
  useEffect(() => {
    if (displayModel) return
    fetchBackendModel().then((m) => {
      if (m) setDisplayModel(m.split('/').pop() ?? m)
    }).catch(() => { /* ignore */ })
  }, [displayModel])

  const sendMessage = useCallback(async () => {
    if (!activeChat || !input.trim() || isStreaming) return

    const text = input.trim()
    setInput('')
    setIsStreaming(true)

    // 判斷 session 是否過期，過期就換新的
    const { sessionId, renewed } = resolveSessionId(activeChat)

    // 使用者訊息
    const userMsg: DisplayMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: text,
      createdAt: Date.now(),
    }

    // Assistant 佔位（streaming 中）
    const assistantMsg: DisplayMessage = {
      id: crypto.randomUUID(),
      role: 'assistant',
      content: '',
      createdAt: Date.now(),
      isStreaming: true,
    }

    // newMessages 是這次 send 的完整快照，整個 streaming 過程都用這個當 base
    const newMessages: DisplayMessage[] = [...activeChat.messages, userMsg, assistantMsg]
    onUpdateChat(activeChat.id, newMessages, renewed ? sessionId : undefined)

    // API payload：只包含已完成的訊息 + 這次的 user 訊息
    const prevCompleted = activeChat.messages
      .filter((m) => !m.isError && !m.isStreaming)
      .map((m) => ({ role: m.role as 'user' | 'assistant', content: m.content }))

    const contextTurns = getContextTurns()
    const trimmedPrev = contextTurns > 0
      ? prevCompleted.slice(-(contextTurns * 2 - 1)) // 保留最近 N 輪（各含 user+assistant），留 1 位給當前 user
      : prevCompleted

    const payload = [
      ...trimmedPrev,
      { role: 'user' as const, content: text },
    ]

    let accContent = ''
    let hadError = false

    try {
      for await (const event of streamChat({
        model: getModel(),
        messages: payload,
        session_id: sessionId,
      })) {
        if (event.type === 'delta') {
          accContent += event.text
          const updated = newMessages.map((m) =>
            m.id === assistantMsg.id ? { ...m, content: accContent } : m
          )
          onUpdateChat(activeChat.id, updated)
        } else if (event.type === 'error') {
          hadError = true
          const updated = newMessages.map((m) =>
            m.id === assistantMsg.id
              ? { ...m, content: event.message, isStreaming: false, isError: true }
              : m
          )
          onUpdateChat(activeChat.id, updated)
          break
        } else if (event.type === 'done') {
          break
        }
      }
    } finally {
      if (!hadError) {
        const final = newMessages.map((m) =>
          m.id === assistantMsg.id
            ? { ...m, content: accContent || '（無回應）', isStreaming: false }
            : m
        )
        onUpdateChat(activeChat.id, final)
      }
      setIsStreaming(false)
    }
  }, [activeChat, input, isStreaming, onUpdateChat])

  return (
    <main className="chat-window">
      <header className="chat-header">
        <button
          className="chat-header-btn"
          onClick={handleBack}
          aria-label="返回"
          title="返回"
        >
          ‹
        </button>
        <button
          className="chat-header-btn mobile-only"
          onClick={onOpenSidebar}
          aria-label="聊天列表"
        >
          ☰
        </button>
        <div className="chat-header-title">
          <span>{activeChat?.title ?? 'M'}</span>
          {displayModel && (
            <span className="chat-header-model">{displayModel}</span>
          )}
        </div>
        <button
          className="chat-header-btn mobile-only"
          onClick={onOpenPanel}
          aria-label="資訊"
        >
          ⋯
        </button>
      </header>

      <div className="chat-messages">
        <MessageList messages={activeChat?.messages ?? []} />
      </div>

      <div className="chat-composer">
        <Composer
          value={input}
          onChange={setInput}
          onSend={sendMessage}
          disabled={isStreaming || !activeChat}
        />
      </div>
    </main>
  )
}
