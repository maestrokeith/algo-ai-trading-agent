import { useEffect, useMemo, useRef, useState } from 'react'
import { Panel } from '@/components/layout/Panel'
import { apiClient } from '@/lib/api'

type Status = {
  status: string
  mode: string
  paper_only: boolean
  live_execution: boolean
  autonomy: string
  capabilities: string[]
}

type RankingRow = {
  symbol: string
  score: number
  trades: number
  win_rate: number | null
  return_pct: number | null
  max_drawdown: number | null
  profit_factor: number | null
  expectancy: number | null
}

type CommandResponse = {
  mission_id: string
  intent: string
  mode: string
  live_execution: boolean
  summary: string
  steps: string[]
  result: any
}

type Mission = {
  id: string
  at: string
  command: string
  status: 'running' | 'complete' | 'blocked' | 'error'
  response?: CommandResponse
  error?: string
}

const QUICK_COMMANDS = [
  'scan and rank gold silver eurusd gbpusd',
  'backtest XAUUSD',
  'optimize XAUUSD',
  'backtest EURUSD',
  'system status',
]

const shell = {
  border: '1px solid var(--border)',
  background: 'linear-gradient(180deg, rgba(255,255,255,0.018), rgba(255,255,255,0))',
}

export function CommandCenter() {
  const [status, setStatus] = useState<Status | null>(null)
  const [command, setCommand] = useState('scan and rank gold silver eurusd gbpusd')
  const [symbol, setSymbol] = useState('XAUUSD')
  const [missions, setMissions] = useState<Mission[]>([])
  const [latest, setLatest] = useState<CommandResponse | null>(null)
  const [autoMode, setAutoMode] = useState(false)
  const [autoInterval, setAutoInterval] = useState(300)
  const [countdown, setCountdown] = useState(300)
  const [busy, setBusy] = useState(false)
  const nextAuto = useRef<number | null>(null)

  useEffect(() => {
    apiClient.get<Status>('/api/command/status')
      .then((r) => setStatus(r.data))
      .catch(() => setStatus(null))
  }, [])

  useEffect(() => {
    if (!autoMode) {
      nextAuto.current = null
      setCountdown(autoInterval)
      return
    }
    const schedule = Date.now() + autoInterval * 1000
    nextAuto.current = schedule
    const id = window.setInterval(() => {
      if (!nextAuto.current) return
      const left = Math.max(0, Math.ceil((nextAuto.current - Date.now()) / 1000))
      setCountdown(left)
      if (left === 0 && !busy) {
        nextAuto.current = Date.now() + autoInterval * 1000
        void runAutonomousCycle('AUTONOMOUS RESEARCH CYCLE')
      }
    }, 1000)
    return () => window.clearInterval(id)
  }, [autoMode, autoInterval, busy])

  const ranking = useMemo<RankingRow[]>(() => {
    if (!latest?.result) return []
    if (Array.isArray(latest.result.symbols)) return latest.result.symbols
    if (Array.isArray(latest.result?.result?.symbols)) return latest.result.result.symbols
    return []
  }, [latest])

  const tradePnls = useMemo<number[]>(() => {
    const trades = latest?.result?.trades
    if (!Array.isArray(trades)) return []
    return trades.map((t: any) => Number(t.pnl ?? 0)).filter(Number.isFinite)
  }, [latest])

  const equitySeries = useMemo(() => {
    let equity = 10000
    const values = [equity]
    for (const pnl of tradePnls) {
      equity += pnl
      values.push(equity)
    }
    return values
  }, [tradePnls])

  const primaryMetrics = latest?.result?.metrics ?? latest?.result?.best?.metrics ?? null
  const safetyOkay = status?.paper_only === true && status?.live_execution === false

  async function execute(text = command) {
    if (!text.trim() || busy) return
    setBusy(true)
    const id = `ui_${Date.now()}`
    const mission: Mission = { id, at: new Date().toISOString(), command: text, status: 'running' }
    setMissions((prev) => [mission, ...prev].slice(0, 30))
    try {
      const response = await apiClient.post<CommandResponse>('/api/command/execute', {
        command: text,
        symbol,
        bars: 3500,
        seed: 7,
      })
      const data = response.data
      setLatest(data)
      setMissions((prev) => prev.map((m) => m.id === id ? {
        ...m,
        status: data.intent === 'blocked' ? 'blocked' : 'complete',
        response: data,
      } : m))
    } catch (err: any) {
      const message = err?.response?.data?.detail ?? err?.message ?? 'Command failed'
      setMissions((prev) => prev.map((m) => m.id === id ? { ...m, status: 'error', error: message } : m))
    } finally {
      setBusy(false)
    }
  }

  async function runAutonomousCycle(label = 'AUTONOMOUS RESEARCH CYCLE') {
    if (busy) return
    setBusy(true)
    const id = `auto_${Date.now()}`
    const mission: Mission = { id, at: new Date().toISOString(), command: label, status: 'running' }
    setMissions((prev) => [mission, ...prev].slice(0, 30))
    try {
      const response = await apiClient.post('/api/command/autonomous-cycle', {
        symbols: ['XAUUSD', 'XAGUSD', 'EURUSD', 'GBPUSD'],
        bars: 3500,
        seed: Math.floor(Date.now() / 60000) % 100000,
      })
      const wrapped: CommandResponse = {
        mission_id: response.data.cycle_id,
        intent: 'autonomous_cycle',
        mode: 'paper_research',
        live_execution: false,
        summary: 'Autonomous paper-research cycle completed across the active universe.',
        steps: ['Generate deterministic research environments', 'Run multi-factor engine', 'Stress paper risk', 'Rank robustness'],
        result: response.data,
      }
      setLatest(wrapped)
      setMissions((prev) => prev.map((m) => m.id === id ? { ...m, status: 'complete', response: wrapped } : m))
    } catch (err: any) {
      const message = err?.response?.data?.detail ?? err?.message ?? 'Autonomous cycle failed'
      setMissions((prev) => prev.map((m) => m.id === id ? { ...m, status: 'error', error: message } : m))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ paddingTop: 16, display: 'grid', gap: 14 }}>
      <Panel title="Mission Control" tag="AUTONOMOUS PAPER RESEARCH" accented>
        <div style={{ padding: 16, display: 'grid', gridTemplateColumns: '1.35fr 0.65fr', gap: 16 }}>
          <div>
            <div style={{ fontFamily: 'var(--font-display)', fontSize: 42, letterSpacing: '0.04em', color: 'var(--amber)' }}>
              COMMAND THE ENGINE
            </div>
            <div style={{ marginTop: 6, maxWidth: 760, fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-muted)', lineHeight: 1.7 }}>
              Agentic research orchestration, multi-market paper backtests, bounded parameter sweeps, risk diagnostics,
              Monte-Carlo validation and autonomous ranking from one control surface. No live-order capability exists in this command path.
            </div>
          </div>
          <div style={{ ...shell, padding: 13 }}>
            <StatusLine label="ENGINE" value={status?.status?.toUpperCase() ?? 'CONNECTING'} good={status?.status === 'ready'} />
            <StatusLine label="MODE" value="PAPER RESEARCH" good />
            <StatusLine label="AUTONOMY" value={autoMode ? 'RUNNING' : 'STANDBY'} good={autoMode} />
            <StatusLine label="LIVE ORDERS" value="HARD DISABLED" good />
            <StatusLine label="SAFETY" value={safetyOkay ? 'VERIFIED' : 'CHECKING'} good={safetyOkay} />
          </div>
        </div>
      </Panel>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(360px, 0.85fr) minmax(0, 1.55fr)', gap: 14 }}>
        <div style={{ display: 'grid', gap: 14, alignContent: 'start' }}>
          <Panel title="Command Console" tag={busy ? 'EXECUTING' : 'READY'} accented>
            <div style={{ padding: 14, display: 'grid', gap: 10 }}>
              <textarea
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                rows={4}
                placeholder="Example: optimize XAUUSD"
                style={{ width: '100%', boxSizing: 'border-box', resize: 'vertical', background: 'var(--bg)', color: 'var(--text-dim)', border: '1px solid var(--border)', padding: 10, fontFamily: 'var(--font-mono)', fontSize: 11, lineHeight: 1.5 }}
              />
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 110px', gap: 8 }}>
                <select value={symbol} onChange={(e) => setSymbol(e.target.value)} style={inputStyle}>
                  {['XAUUSD', 'XAGUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD'].map((s) => <option key={s}>{s}</option>)}
                </select>
                <button disabled={busy} onClick={() => execute()} style={actionButton}>{busy ? 'BUSY…' : 'EXECUTE'}</button>
              </div>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {QUICK_COMMANDS.map((q) => (
                  <button key={q} disabled={busy} onClick={() => { setCommand(q); void execute(q) }} style={chipButton}>{q.toUpperCase()}</button>
                ))}
              </div>
            </div>
          </Panel>

          <Panel title="Autonomous Lab" tag={autoMode ? `NEXT ${countdown}s` : 'OFF'} accented>
            <div style={{ padding: 14, display: 'grid', gap: 10 }}>
              <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-muted)', lineHeight: 1.55 }}>
                When enabled, the browser schedules recurring paper-research scans across gold, silver and major FX instruments. The loop creates research reports only.
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 90px', gap: 8 }}>
                <select value={autoInterval} onChange={(e) => { const v = Number(e.target.value); setAutoInterval(v); setCountdown(v) }} style={inputStyle}>
                  <option value={120}>2 MIN</option>
                  <option value={300}>5 MIN</option>
                  <option value={600}>10 MIN</option>
                  <option value={1800}>30 MIN</option>
                </select>
                <button onClick={() => setAutoMode((v) => !v)} style={{ ...actionButton, borderColor: autoMode ? 'var(--green)' : 'var(--amber)', color: autoMode ? 'var(--green)' : 'var(--amber)' }}>
                  {autoMode ? 'STOP' : 'START'}
                </button>
              </div>
              <button disabled={busy} onClick={() => runAutonomousCycle()} style={secondaryButton}>RUN CYCLE NOW</button>
            </div>
          </Panel>

          <Panel title="Agent Trace" tag={latest?.mission_id ?? 'WAITING'}>
            <div style={{ padding: 14, display: 'grid', gap: 8 }}>
              {(latest?.steps ?? ['Awaiting mission…']).map((step, index) => (
                <div key={`${step}-${index}`} style={{ display: 'grid', gridTemplateColumns: '28px 1fr', gap: 8, alignItems: 'start' }}>
                  <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--amber)', fontSize: 9 }}>{String(index + 1).padStart(2, '0')}</span>
                  <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-dim)', fontSize: 10, lineHeight: 1.45 }}>{step}</span>
                </div>
              ))}
              {latest?.summary && <div style={{ marginTop: 5, borderTop: '1px solid var(--border)', paddingTop: 9, fontFamily: 'var(--font-mono)', color: 'var(--green)', fontSize: 10 }}>{latest.summary}</div>}
            </div>
          </Panel>
        </div>

        <div style={{ display: 'grid', gap: 14, alignContent: 'start' }}>
          <Panel title="Research Telemetry" tag={latest?.intent?.toUpperCase() ?? 'NO MISSION'} accented>
            <div style={{ padding: 14, display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0,1fr))', gap: 8 }}>
              <Metric label="Trades" value={fmt(primaryMetrics?.trades, 0)} />
              <Metric label="Win Rate" value={pct(primaryMetrics?.win_rate)} />
              <Metric label="Profit Factor" value={fmt(primaryMetrics?.profit_factor, 2)} />
              <Metric label="Return" value={pct(primaryMetrics?.return_pct)} />
              <Metric label="Max Drawdown" value={pct(primaryMetrics?.max_drawdown)} />
              <Metric label="Expectancy" value={money(primaryMetrics?.expectancy)} />
              <Metric label="Net P/L" value={money(primaryMetrics?.net_profit)} />
              <Metric label="Loss Run" value={fmt(primaryMetrics?.max_consecutive_losses, 0)} />
            </div>
          </Panel>

          <div style={{ display: 'grid', gridTemplateColumns: '1.3fr 0.7fr', gap: 14 }}>
            <Panel title="Equity / Mission Curve" tag={`${equitySeries.length} POINTS`}>
              <div style={{ padding: 12 }}><LineGraph values={equitySeries.length > 1 ? equitySeries : [10000, 10010, 10005, 10022, 10018, 10031]} /></div>
            </Panel>
            <Panel title="Risk Radar" tag="PAPER">
              <div style={{ padding: 12 }}>
                <RadarGraph metrics={primaryMetrics} />
              </div>
            </Panel>
          </div>

          <Panel title="Cross-Instrument Ranking" tag={ranking.length ? `${ranking.length} SYMBOLS` : 'AWAITING SCAN'} accented>
            <div style={{ padding: 12, overflowX: 'auto' }}>
              {ranking.length ? (
                <table style={{ width: '100%', borderCollapse: 'collapse', fontFamily: 'var(--font-mono)', fontSize: 9 }}>
                  <thead>
                    <tr>{['#', 'SYMBOL', 'SCORE', 'TRADES', 'WIN', 'RETURN', 'MAX DD', 'PF', 'EXPECTANCY'].map((h) => <th key={h} style={th}>{h}</th>)}</tr>
                  </thead>
                  <tbody>
                    {ranking.map((row, index) => (
                      <tr key={row.symbol} style={{ borderTop: '1px solid var(--border)' }}>
                        <td style={td}>{index + 1}</td>
                        <td style={{ ...td, color: index === 0 ? 'var(--amber)' : 'var(--text-dim)', fontWeight: 600 }}>{row.symbol}</td>
                        <td style={td}>{fmt(row.score, 4)}</td>
                        <td style={td}>{row.trades}</td>
                        <td style={td}>{pct(row.win_rate)}</td>
                        <td style={td}>{pct(row.return_pct)}</td>
                        <td style={td}>{pct(row.max_drawdown)}</td>
                        <td style={td}>{fmt(row.profit_factor, 2)}</td>
                        <td style={td}>{money(row.expectancy)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : <Empty text="Run SCAN/RANK or an autonomous cycle to populate the research universe." />}
            </div>
          </Panel>

          <Panel title="Mission Ledger" tag={`${missions.length} EVENTS`}>
            <div style={{ maxHeight: 300, overflowY: 'auto' }}>
              {missions.length ? missions.map((mission) => (
                <div key={mission.id} style={{ padding: '9px 12px', borderBottom: '1px solid var(--border)', display: 'grid', gridTemplateColumns: '95px 1fr 80px', gap: 10 }}>
                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: 8, color: 'var(--text-muted)' }}>{new Date(mission.at).toLocaleTimeString()}</span>
                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-dim)' }}>{mission.command}</span>
                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: 8, textAlign: 'right', color: mission.status === 'error' || mission.status === 'blocked' ? 'var(--red)' : mission.status === 'complete' ? 'var(--green)' : 'var(--amber)' }}>{mission.status.toUpperCase()}</span>
                </div>
              )) : <Empty text="Mission ledger is empty." />}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  )
}

function LineGraph({ values }: { values: number[] }) {
  const width = 720
  const height = 190
  const pad = 16
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = Math.max(max - min, 1)
  const points = values.map((v, i) => {
    const x = pad + (i / Math.max(values.length - 1, 1)) * (width - pad * 2)
    const y = height - pad - ((v - min) / range) * (height - pad * 2)
    return `${x},${y}`
  }).join(' ')
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width="100%" height="190" role="img" aria-label="equity curve">
      {[0.25, 0.5, 0.75].map((p) => <line key={p} x1={pad} x2={width - pad} y1={p * height} y2={p * height} stroke="var(--border)" strokeWidth="1" />)}
      <polyline points={points} fill="none" stroke="var(--amber)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
      <text x={pad} y={14} fill="var(--text-muted)" fontSize="9">HIGH {max.toFixed(2)}</text>
      <text x={pad} y={height - 3} fill="var(--text-muted)" fontSize="9">LOW {min.toFixed(2)}</text>
    </svg>
  )
}

function RadarGraph({ metrics }: { metrics: any }) {
  const width = 280
  const height = 190
  const cx = 140
  const cy = 95
  const r = 72
  const labels = ['WIN', 'PF', 'RETURN', 'DD', 'EXPECT']
  const raw = [
    clamp(Number(metrics?.win_rate ?? 0) * 1.6),
    clamp(Number(metrics?.profit_factor ?? 0) / 2.5),
    clamp(0.5 + Number(metrics?.return_pct ?? 0) * 4),
    clamp(1 - Number(metrics?.max_drawdown ?? 0) * 4),
    clamp(0.5 + Math.tanh(Number(metrics?.expectancy ?? 0) / 30) * 0.5),
  ]
  const vertices = labels.map((_, i) => polar(cx, cy, r, i, labels.length))
  const scorePoints = raw.map((score, i) => {
    const [x, y] = polar(cx, cy, r * score, i, labels.length)
    return `${x},${y}`
  }).join(' ')
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width="100%" height="190" role="img" aria-label="risk radar">
      {[0.33, 0.66, 1].map((scale) => (
        <polygon key={scale} points={labels.map((_, i) => polar(cx, cy, r * scale, i, labels.length).join(',')).join(' ')} fill="none" stroke="var(--border)" />
      ))}
      {vertices.map(([x, y], i) => <line key={i} x1={cx} y1={cy} x2={x} y2={y} stroke="var(--border)" />)}
      <polygon points={scorePoints} fill="var(--amber-dim)" stroke="var(--amber)" strokeWidth="1.5" />
      {vertices.map(([x, y], i) => <text key={labels[i]} x={x} y={y} fill="var(--text-muted)" fontSize="8" textAnchor="middle">{labels[i]}</text>)}
    </svg>
  )
}

function polar(cx: number, cy: number, r: number, i: number, total: number): [number, number] {
  const angle = -Math.PI / 2 + (i / total) * Math.PI * 2
  return [cx + Math.cos(angle) * r, cy + Math.sin(angle) * r]
}

function clamp(v: number) { return Math.max(0.05, Math.min(1, Number.isFinite(v) ? v : 0.05)) }

function StatusLine({ label, value, good }: { label: string; value: string; good?: boolean }) {
  return <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, padding: '5px 0', fontFamily: 'var(--font-mono)', fontSize: 9 }}><span style={{ color: 'var(--text-muted)' }}>{label}</span><span style={{ color: good ? 'var(--green)' : 'var(--amber)' }}>{value}</span></div>
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div style={{ ...shell, padding: 10 }}><div style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-muted)', fontSize: 8, letterSpacing: '0.12em' }}>{label.toUpperCase()}</div><div style={{ marginTop: 5, fontFamily: 'var(--font-display)', color: 'var(--text-dim)', fontSize: 22 }}>{value}</div></div>
}

function Empty({ text }: { text: string }) {
  return <div style={{ padding: 18, fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-muted)' }}>{text}</div>
}

function fmt(v: any, digits = 2) {
  const n = Number(v)
  return Number.isFinite(n) ? n.toFixed(digits) : '—'
}
function pct(v: any) {
  const n = Number(v)
  return Number.isFinite(n) ? `${(n * 100).toFixed(2)}%` : '—'
}
function money(v: any) {
  const n = Number(v)
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : '—'
}

const inputStyle = { width: '100%', boxSizing: 'border-box' as const, background: 'var(--bg)', color: 'var(--text-dim)', border: '1px solid var(--border)', padding: '9px 10px', fontFamily: 'var(--font-mono)', fontSize: 10 }
const actionButton = { border: '1px solid var(--amber)', background: 'var(--amber-dim)', color: 'var(--amber)', padding: '9px 12px', fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.12em', cursor: 'pointer' }
const secondaryButton = { ...actionButton, borderColor: 'var(--border)', background: 'transparent', color: 'var(--text-dim)' }
const chipButton = { border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-muted)', padding: '5px 7px', fontFamily: 'var(--font-mono)', fontSize: 7, cursor: 'pointer' }
const th = { textAlign: 'left' as const, color: 'var(--text-muted)', fontWeight: 500, padding: '7px 5px', whiteSpace: 'nowrap' as const }
const td = { color: 'var(--text-dim)', padding: '8px 5px', whiteSpace: 'nowrap' as const }
