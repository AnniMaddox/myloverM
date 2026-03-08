import { useEffect, useRef } from 'react'
import type { DisplayMessage } from '../types'

interface Props {
  messages: DisplayMessage[]
}

function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString('zh-TW', {
    hour: '2-digit',
    minute: '2-digit',
  })
}

export default function MessageList({ messages }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)

  // 每次訊息更新（包含 streaming 中）自動捲到底
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  if (messages.length === 0) {
    return (
      <div className="chat-empty">
        <p className="chat-empty-hint">開始和 M 聊天吧</p>
      </div>
    )
  }

  return (
    <div className="message-list">
      {messages.map((msg) => (
        <div key={msg.id} className={`msg-row msg-row--${msg.role}`}>
          <div
            className={[
              'bubble',
              `bubble--${msg.role}`,
              msg.isError ? 'bubble--error' : '',
            ].join(' ').trim()}
          >
            {msg.content || (msg.isStreaming ? '' : '（無回應）')}
            {msg.isStreaming && !msg.isError && (
              <span className="streaming-cursor" aria-hidden />
            )}
          </div>
          {!msg.isStreaming && (
            <div className="msg-time">{formatTime(msg.createdAt)}</div>
          )}
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
