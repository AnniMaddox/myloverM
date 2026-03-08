import { useState, useCallback, useEffect, useRef } from 'react'
import ChatSidebar from './components/ChatSidebar'
import ChatWindow from './components/ChatWindow'
import SidePanel from './components/SidePanel'
import {
  loadChats,
  saveChats,
  createNewChat,
  deriveChatTitle,
} from './session'
import type { StoredChat, DisplayMessage } from './types'

// ── 初始化（只跑一次）──
function initState(): { chats: StoredChat[]; activeChatId: string } {
  const loaded = loadChats()
  if (loaded.length > 0) return { chats: loaded, activeChatId: loaded[0].id }
  const first = createNewChat()
  return { chats: [first], activeChatId: first.id }
}

export default function App() {
  const init = useRef(initState())

  // 聊天列表 state（newest-first）
  const [chats, setChats] = useState<StoredChat[]>(init.current.chats)
  const [activeChatId, setActiveChatId] = useState<string>(init.current.activeChatId)
  // ChatSidebar：切換聊天
  const handleSelectChat = useCallback((chatId: string) => setActiveChatId(chatId), [])

  // ChatSidebar：新對話
  const handleNewChat = useCallback(() => {
    const chat = createNewChat()
    setChats((prev) => [chat, ...prev])
    setActiveChatId(chat.id)
  }, [])

  // 手機版：左右 panel 開關
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [panelOpen, setPanelOpen] = useState(false)

  const closeSidebar = () => setSidebarOpen(false)
  const closePanel   = () => setPanelOpen(false)

  // chats 變動時自動存 localStorage
  useEffect(() => {
    saveChats(chats)
  }, [chats])

  const activeChat = chats.find((c) => c.id === activeChatId) ?? null

  // ChatWindow 回呼：更新訊息（streaming 過程中會頻繁呼叫）
  const handleUpdateChat = useCallback(
    (chatId: string, messages: DisplayMessage[], sessionId?: string) => {
      setChats((prev) => {
        const now = Date.now()
        const target = prev.find((c) => c.id === chatId)
        if (!target) return prev
        const updated: StoredChat = {
          ...target,
          messages,
          title: deriveChatTitle(messages),
          sessionId: sessionId ?? target.sessionId,
          updatedAt: now,
          lastActiveAt: now,  // 每次送訊息都刷新，30 分鐘是「閒置」逾時，不是「對話時長」
        }
        // 把這段聊天移到最前面（newest-first）
        return [updated, ...prev.filter((c) => c.id !== chatId)]
      })
    },
    [],
  )

  return (
    <>
      <style>{APP_STYLES}</style>

      {(sidebarOpen || panelOpen) && (
        <div
          className="overlay-mask"
          onClick={() => { closeSidebar(); closePanel() }}
        />
      )}

      <div className="app-shell">
        <div className={`sidebar-wrap${sidebarOpen ? ' is-open' : ''}`}>
          <ChatSidebar
            chats={chats}
            activeChatId={activeChatId}
            onClose={closeSidebar}
            onSelectChat={(id) => { handleSelectChat(id); closeSidebar() }}
            onNewChat={handleNewChat}
          />
        </div>

        <ChatWindow
          onOpenSidebar={() => setSidebarOpen(true)}
          onOpenPanel={() => setPanelOpen(true)}
          activeChat={activeChat}
          onUpdateChat={handleUpdateChat}
        />

        <div className={`panel-wrap${panelOpen ? ' is-open' : ''}`}>
          <SidePanel activeChat={activeChat} onClose={closePanel} />
        </div>
      </div>
    </>
  )
}

/* ============================================================
   App 版型樣式
   所有顏色走 index.css 的 CSS variables
   ============================================================ */
const APP_STYLES = `

/* ── Shell：三欄 flex ── */
.app-shell {
  display: flex;
  height: 100dvh;
  overflow: hidden;
  background: var(--bg-base);
}

/* ── Overlay 遮罩（手機用） ── */
.overlay-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  z-index: 40;
}

/* ════════════════════════════════════
   左側 Sidebar
════════════════════════════════════ */
.sidebar-wrap {
  flex: 0 0 var(--sidebar-w);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-surface);
  border-right: 1px solid var(--border-subtle);
}

.sidebar {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 16px 12px;
  border-bottom: 1px solid var(--border-subtle);
}

.sidebar-title {
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0.01em;
  color: var(--text-primary);
}

.sidebar-close {
  display: none;
  font-size: 16px;
  color: var(--text-muted);
  padding: 4px;
  border-radius: var(--radius-sm);
  transition: color 0.15s;
}
.sidebar-close:hover { color: var(--text-secondary); }

.sidebar-new {
  padding: 12px 12px 8px;
}

.btn-new-chat {
  width: 100%;
  padding: 9px 14px;
  border-radius: var(--radius-md);
  background: var(--accent-bg);
  color: var(--accent);
  font-size: 14px;
  font-weight: 500;
  text-align: left;
  transition: background 0.15s, color 0.15s;
}
.btn-new-chat:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.sidebar-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 8px 16px;
}

.sidebar-empty {
  padding: 24px 8px;
  font-size: 13px;
  color: var(--text-muted);
  text-align: center;
}

/* ════════════════════════════════════
   中間 ChatWindow
════════════════════════════════════ */
.chat-window {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-base);
  min-width: 0;
}

.chat-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 20px;
  height: 56px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.chat-header-title {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}
.chat-header-title > span:first-child {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
  letter-spacing: 0.02em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chat-header-model {
  font-size: 11px;
  color: var(--text-muted);
  font-weight: 400;
  letter-spacing: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chat-header-btn {
  padding: 6px 8px;
  font-size: 18px;
  color: var(--text-secondary);
  border-radius: var(--radius-sm);
  transition: color 0.15s, background 0.15s;
}
.chat-header-btn:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}

.mobile-only { display: none; }

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 24px 24px 16px;
  display: flex;
  flex-direction: column;
}

.chat-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.chat-empty-hint {
  font-size: 14px;
  color: var(--text-muted);
  letter-spacing: 0.02em;
}

.chat-composer {
  padding: 12px 20px 20px;
  border-top: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.composer-placeholder {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-xl);
  padding: 10px 14px;
  transition: border-color 0.15s;
}
.composer-placeholder:focus-within {
  border-color: var(--accent-dim);
}

.composer-input {
  flex: 1;
  font-size: 15px;
  line-height: 1.5;
  color: var(--text-primary);
  background: none;
  resize: none;
}
.composer-input::placeholder { color: var(--text-placeholder); }

.composer-send {
  flex-shrink: 0;
  padding: 6px 16px;
  border-radius: var(--radius-lg);
  background: var(--accent);
  color: #1a1712;
  font-size: 14px;
  font-weight: 600;
  transition: opacity 0.15s;
}
.composer-send:hover:not(:disabled) { opacity: 0.85; }
.composer-send:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

/* ════════════════════════════════════
   右側 SidePanel
════════════════════════════════════ */
.panel-wrap {
  flex: 0 0 var(--panel-w);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-surface);
  border-left: 1px solid var(--border-subtle);
}

.side-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.panel-header {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 12px 12px 0;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.panel-tabs {
  display: flex;
  flex: 1;
  gap: 2px;
}

.panel-tab {
  flex: 1;
  padding: 8px 4px 10px;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-muted);
  border-bottom: 2px solid transparent;
  transition: color 0.15s, border-color 0.15s;
  white-space: nowrap;
}
.panel-tab:hover { color: var(--text-secondary); }
.panel-tab--active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

.panel-close {
  display: none;
  font-size: 16px;
  color: var(--text-muted);
  padding: 4px 6px;
  border-radius: var(--radius-sm);
  transition: color 0.15s;
  flex-shrink: 0;
}
.panel-close:hover { color: var(--text-secondary); }

.panel-body {
  flex: 1;
  overflow-y: auto;
  padding: 20px 16px;
}

.panel-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 120px;
  color: var(--text-muted);
  font-size: 13px;
  border: 1px dashed var(--border);
  border-radius: var(--radius-md);
}

/* ════════════════════════════════════
   訊息氣泡
════════════════════════════════════ */
.message-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding-bottom: 8px;
}

.msg-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.msg-row--user      { align-items: flex-end; }
.msg-row--assistant { align-items: flex-start; }

.bubble {
  max-width: 72%;
  padding: 10px 14px;
  font-size: 15px;
  line-height: 1.6;
  word-wrap: break-word;
  white-space: pre-wrap;
  border-radius: var(--radius-lg);
}

/* 使用者氣泡：右側，暖棕，右下角收 */
.bubble--user {
  background: var(--bubble-user-bg);
  color: var(--bubble-user-text);
  border-bottom-right-radius: var(--radius-sm);
}

/* M 氣泡：左側，深暖灰，左下角收 */
.bubble--assistant {
  background: var(--bubble-ai-bg);
  color: var(--bubble-ai-text);
  border-bottom-left-radius: var(--radius-sm);
}

.bubble--error {
  background: rgba(217, 112, 112, 0.12);
  color: var(--color-error);
  border: 1px solid rgba(217, 112, 112, 0.25);
}

.msg-time {
  font-size: 11px;
  color: var(--text-muted);
  padding: 0 4px;
}

/* streaming 游標 */
@keyframes blink {
  0%, 100% { opacity: 1; }
  50%       { opacity: 0; }
}
.streaming-cursor {
  display: inline-block;
  width: 2px;
  height: 0.9em;
  background: currentColor;
  margin-left: 3px;
  vertical-align: text-bottom;
  border-radius: 1px;
  animation: blink 0.9s step-end infinite;
}

/* ════════════════════════════════════
   Composer（真實版本）
════════════════════════════════════ */
.composer {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-xl);
  padding: 10px 14px;
  transition: border-color 0.15s;
}
.composer:focus-within { border-color: var(--accent-dim); }

.composer-input {
  flex: 1;
  font-size: 15px;
  line-height: 1.55;
  color: var(--text-primary);
  background: none;
  resize: none;
  overflow: hidden;
  max-height: 160px;
  overflow-y: auto;
}
.composer-input::placeholder { color: var(--text-placeholder); }
.composer-input:disabled { opacity: 0.5; }

.composer-send {
  flex-shrink: 0;
  width: 34px;
  height: 34px;
  border-radius: 50%;
  background: var(--accent);
  color: #1a1712;
  font-size: 17px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: opacity 0.15s, transform 0.1s;
}
.composer-send:hover:not(:disabled) {
  opacity: 0.85;
  transform: scale(1.05);
}
.composer-send:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}

/* ════════════════════════════════════
   手機版 Responsive（< 768px）
════════════════════════════════════ */
@media (max-width: 767px) {

  .mobile-only { display: flex; }

  .sidebar-wrap {
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    width: min(82vw, 320px);
    z-index: 50;
    transform: translateX(-100%);
    transition: transform 0.28s var(--ease-out);
    box-shadow: 4px 0 24px rgba(0,0,0,0.4);
  }
  .sidebar-wrap.is-open { transform: translateX(0); }
  .sidebar-close { display: flex; }

  .panel-wrap {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    width: min(82vw, 320px);
    z-index: 50;
    transform: translateX(100%);
    transition: transform 0.28s var(--ease-out);
    box-shadow: -4px 0 24px rgba(0,0,0,0.4);
  }
  .panel-wrap.is-open { transform: translateX(0); }
  .panel-close { display: flex; }

  .chat-messages { padding: 16px 12px 12px; }
  .chat-composer  { padding: 8px 12px 16px; }
}
`
