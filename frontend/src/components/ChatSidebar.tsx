import type { StoredChat } from '../types'

interface Props {
  chats: StoredChat[]
  activeChatId: string
  onClose?: () => void
  onSelectChat: (chatId: string) => void
  onNewChat: () => void
}

function formatRelativeTime(ts: number): string {
  const diff = Date.now() - ts
  const min  = Math.floor(diff / 60_000)
  const hr   = Math.floor(diff / 3_600_000)
  const day  = Math.floor(diff / 86_400_000)
  if (min < 1)   return '剛剛'
  if (min < 60)  return `${min} 分鐘前`
  if (hr  < 24)  return `${hr} 小時前`
  if (day < 7)   return `${day} 天前`
  return new Date(ts).toLocaleDateString('zh-TW', { month: 'numeric', day: 'numeric' })
}

export default function ChatSidebar({ chats, activeChatId, onClose, onSelectChat, onNewChat }: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <span className="sidebar-title">對話</span>
        <button className="sidebar-close" onClick={onClose} aria-label="關閉">✕</button>
      </div>

      <div className="sidebar-new">
        <button className="btn-new-chat" onClick={onNewChat}>＋ 新對話</button>
      </div>

      <div className="sidebar-list">
        {chats.length === 0 ? (
          <div className="sidebar-empty">還沒有對話</div>
        ) : (
          chats.map((chat) => (
            <button
              key={chat.id}
              className={`chat-item${chat.id === activeChatId ? ' chat-item--active' : ''}`}
              onClick={() => onSelectChat(chat.id)}
            >
              <span className="chat-item-title">{chat.title}</span>
              <span className="chat-item-time">{formatRelativeTime(chat.updatedAt)}</span>
            </button>
          ))
        )}
      </div>

      <style>{SIDEBAR_STYLES}</style>
    </aside>
  )
}

const SIDEBAR_STYLES = `
.chat-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
  width: 100%;
  padding: 9px 10px;
  border-radius: var(--radius-md);
  text-align: left;
  transition: background 0.12s;
}
.chat-item:hover {
  background: var(--bg-hover);
}
.chat-item--active {
  background: var(--accent-bg);
}
.chat-item-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.chat-item--active .chat-item-title {
  color: var(--accent);
}
.chat-item-time {
  font-size: 11px;
  color: var(--text-muted);
}
`
