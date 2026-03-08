import { useState } from 'react'
import { API_BASE_LS_KEY, MODEL_LS_KEY, CONTEXT_TURNS_LS_KEY, checkHealth, fetchBackendModel } from '../api'
import type { HealthStatus, StoredChat } from '../types'

type Tab = 'chat' | 'memory' | 'loops' | 'system'

interface Props {
  activeChat: StoredChat | null
  onClose?: () => void
}

const TABS: { key: Tab; label: string }[] = [
  { key: 'chat',   label: 'Chat'   },
  { key: 'memory', label: 'Memory' },
  { key: 'loops',  label: 'Loops'  },
  { key: 'system', label: 'System' },
]

function normalizeApiBaseUrl(raw: string): string {
  const trimmed = raw.trim().replace(/\/+$/, '')
  if (!trimmed) return ''
  if (/^https?:\/\//i.test(trimmed)) return trimmed
  return `https://${trimmed}`
}

function loadApiUrl(): string {
  try {
    return normalizeApiBaseUrl(localStorage.getItem(API_BASE_LS_KEY) ?? '')
  } catch {
    return ''
  }
}

function loadModel(): string {
  try { return localStorage.getItem(MODEL_LS_KEY) ?? '' } catch { return '' }
}

function loadContextTurns(): number {
  try {
    const saved = localStorage.getItem(CONTEXT_TURNS_LS_KEY)
    if (saved) {
      const n = parseInt(saved, 10)
      if (!isNaN(n) && n >= 0) return n
    }
  } catch { /* ignore */ }
  return 0
}

type HealthState =
  | { status: 'idle' }
  | { status: 'checking' }
  | { status: 'ok'; data: HealthStatus }
  | { status: 'error'; message: string }

export default function SidePanel({ activeChat, onClose }: Props) {
  const [activeTab, setActiveTab] = useState<Tab>('system')
  const [apiUrl, setApiUrl]       = useState(loadApiUrl)
  const [model, setModel]         = useState(loadModel)
  const [contextTurns, setContextTurns] = useState(loadContextTurns)
  const [health, setHealth]       = useState<HealthState>({ status: 'idle' })
  const [backendModel, setBackendModel] = useState('')

  function handleModelChange(val: string) {
    setModel(val)
    try { localStorage.setItem(MODEL_LS_KEY, val.trim()) } catch { /* ignore */ }
  }

  function handleContextTurnsChange(val: number) {
    setContextTurns(val)
    try { localStorage.setItem(CONTEXT_TURNS_LS_KEY, String(val)) } catch { /* ignore */ }
  }

  function handleUrlChange(val: string) {
    setApiUrl(val)
    setHealth({ status: 'idle' })
    try { localStorage.setItem(API_BASE_LS_KEY, val.trim()) } catch { /* ignore */ }
  }

  function persistNormalizedApiUrl(raw: string) {
    const normalized = normalizeApiBaseUrl(raw)
    setApiUrl(normalized)
    try { localStorage.setItem(API_BASE_LS_KEY, normalized) } catch { /* ignore */ }
    return normalized
  }

  async function handleHealthCheck() {
    const normalized = persistNormalizedApiUrl(apiUrl)
    if (!normalized) {
      setHealth({ status: 'error', message: '請先填後端 URL' })
      return
    }

    setHealth({ status: 'checking' })
    try {
      const data = await checkHealth()
      setHealth({ status: 'ok', data })
      const bm = await fetchBackendModel()
      if (bm) setBackendModel(bm)
    } catch (e) {
      const rawMessage = e instanceof Error ? e.message : '連線失敗'
      const message = /load failed|failed to fetch|networkerror/i.test(rawMessage)
        ? '連線失敗。常見是 CORS：請到 Railway 把目前前端網域加入允許來源。'
        : rawMessage
      setHealth({ status: 'error', message })
    }
  }

  // Chat tab：session 資訊
  const sessionAge = activeChat
    ? Math.floor((Date.now() - activeChat.lastActiveAt) / 60_000)
    : null

  return (
    <aside className="side-panel">
      <div className="panel-header">
        <div className="panel-tabs">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={`panel-tab${activeTab === t.key ? ' panel-tab--active' : ''}`}
              onClick={() => setActiveTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <button className="panel-close" onClick={onClose} aria-label="關閉">✕</button>
      </div>

      <div className="panel-body">

        {/* ── Chat tab ── */}
        {activeTab === 'chat' && (
          <div style={{ display: 'grid', gap: 12 }}>
            {!activeChat ? (
              <p className="sp-hint">沒有選中的對話。</p>
            ) : (
              <>
                <InfoRow label="對話標題" value={activeChat.title} />
                <InfoRow label="Session ID" value={activeChat.sessionId.slice(0, 16) + '…'} mono />
                <InfoRow
                  label="Session 閒置"
                  value={sessionAge === null ? '—' : sessionAge < 1 ? '剛剛' : `${sessionAge} 分鐘前`}
                />
                <InfoRow
                  label="訊息數"
                  value={`${activeChat.messages.filter((m) => !m.isStreaming).length} 則`}
                />
              </>
            )}
          </div>
        )}

        {/* ── Memory tab ── */}
        {activeTab === 'memory' && (
          <div className="panel-placeholder"><span>記憶瀏覽（後端 API 待接）</span></div>
        )}

        {/* ── Loops tab ── */}
        {activeTab === 'loops' && (
          <div className="panel-placeholder"><span>Open Loops（後端 API 待接）</span></div>
        )}

        {/* ── System tab ── */}
        {activeTab === 'system' && (
          <div style={{ display: 'grid', gap: 20 }}>

            {/* URL 設定 */}
            <div>
              <label className="sp-label">後端 URL</label>
              <input
                className="sp-input"
                type="url"
                placeholder="https://xxxx.up.railway.app"
                value={apiUrl}
                onChange={(e) => handleUrlChange(e.target.value)}
                onBlur={(e) => persistNormalizedApiUrl(e.target.value)}
              />
              <p className="sp-hint">填 Railway 根網址，不含 /v1。少貼 https:// 會自動補上。</p>
            </div>

            {/* Model */}
            <div>
              <label className="sp-label">Model</label>
              <input
                className="sp-input"
                type="text"
                placeholder={backendModel || 'Railway 預設'}
                value={model}
                onChange={(e) => handleModelChange(e.target.value)}
              />
              <p className="sp-hint">
                留空自動用 Railway 的設定{backendModel ? `（${backendModel}）` : '，先測試連線可顯示'}。有填就用填的。
              </p>
            </div>

            {/* 上下文截斷 */}
            <div>
              <label className="sp-label">
                上下文輪數&nbsp;
                <span style={{ color: 'var(--text-primary)', fontVariantNumeric: 'tabular-nums' }}>
                  {contextTurns === 0 ? '不限' : `${contextTurns} 輪`}
                </span>
              </label>
              <input
                type="range"
                min={0}
                max={999}
                step={1}
                value={contextTurns}
                onChange={(e) => handleContextTurnsChange(Number(e.target.value))}
                style={{ width: '100%', accentColor: 'var(--accent)' }}
              />
              <p className="sp-hint">
                0 = 不截斷（送全部歷史）。設 N 則每次只送最近 N 輪對話給 M。
                超出範圍的對話靠記憶系統補充。
              </p>
            </div>

            {/* Health check */}
            <div style={{ display: 'grid', gap: 8 }}>
              <button
                className="sp-btn"
                onClick={handleHealthCheck}
                disabled={health.status === 'checking'}
              >
                {health.status === 'checking' ? '測試中…' : '測試連線'}
              </button>

              {health.status === 'ok' && (
                <div className="sp-health sp-health--ok">
                  <span>✓ 連線正常</span>
                  {health.data.gateway && <InfoRow label="Gateway" value={health.data.gateway} />}
                  <InfoRow
                    label="記憶"
                    value={health.data.memory_enabled ? `開啟（${health.data.memory_count ?? 0} 條）` : '關閉'}
                  />
                </div>
              )}

              {health.status === 'error' && (
                <div className="sp-health sp-health--err">
                  <span>✗ {health.message}</span>
                </div>
              )}
            </div>

            {/* 測試頁 */}
            <div>
              <a
                href="/Our-love/docs/labs/ai-memory-chat.html"
                target="_blank"
                rel="noopener noreferrer"
                className="sp-btn"
                style={{ display: 'block', textAlign: 'center', textDecoration: 'none' }}
              >
                開啟測試頁 ↗
              </a>
              <p className="sp-hint">簡易測試介面，可直接貼訊息測試後端。</p>
            </div>

          </div>
        )}

      </div>

      <style>{SP_STYLES}</style>
    </aside>
  )
}

function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div style={{ display: 'grid', gap: 2 }}>
      <span className="sp-label" style={{ margin: 0 }}>{label}</span>
      <span style={{ fontSize: 13, color: 'var(--text-primary)', fontFamily: mono ? 'monospace' : undefined, wordBreak: 'break-all' }}>
        {value}
      </span>
    </div>
  )
}

const SP_STYLES = `
.sp-label {
  display: block;
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 6px;
}
.sp-input {
  width: 100%;
  padding: 8px 10px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border);
  background: var(--bg-elevated);
  color: var(--text-primary);
  font-size: 14px;
}
.sp-input:focus {
  outline: none;
  border-color: var(--accent-dim);
}
.sp-hint {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.5;
}
.sp-btn {
  padding: 8px 14px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border);
  background: var(--bg-elevated);
  color: var(--text-primary);
  font-size: 13px;
  font-weight: 500;
  transition: background 0.12s;
}
.sp-btn:hover:not(:disabled) { background: var(--bg-hover); }
.sp-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.sp-health {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  font-size: 13px;
  display: grid;
  gap: 8px;
}
.sp-health--ok  { background: rgba(100,200,130,0.1); border: 1px solid rgba(100,200,130,0.3); color: #7ecf9e; }
.sp-health--err { background: rgba(217,112,112,0.1); border: 1px solid rgba(217,112,112,0.25); color: #e89090; }
`
