import { useEffect, useMemo, useState } from 'react'
import { Panel } from '@/components/layout/Panel'

type RankingRow = {
  symbol: string
  score: number
  trades: number
  win_rate: number | null
  return_pct: number | null
  max_drawdown: number | null
  profit_factor: number | null
  expectancy: number | null
  net_profit?: number | null
}

type HistoryRow = {
  generated_at: string
  leader: string | null
  leader_score: number | null
  average_score: number
  total_trades: number
  worst_drawdown: number
}

type CouncilRow = { agent: string; status: string; message: string }

type CloudState = {
  status: string
  mode: string
  paper_only: boolean
  live_execution: boolean
  data_source?: string
  generated_at: string | null
  cycle_id: string | null
  ranking: RankingRow[]
  history: HistoryRow[]
  agent_council: CouncilRow[]
  leader?: RankingRow | null
  summary?: {
    symbols_evaluated: number
    total_simulated_trades: number
    average_score: number
    worst_drawdown: number
  }
}

const STATE_URL = 'https://raw.githubusercontent.com/maestrokeith/algo-ai-trading-agent/research-state/autonomy/latest.json'

export function Autonomy() {
  const [state, setState] = useState<CloudState | null>(null)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)

  async function refresh() {
    setRefreshing(true)
    try {
      const response = await fetch(`${STATE_URL}?t=${Date.now()}`, { cache: 'no-store' })
      if (!response.ok) throw new Error(`cloud state ${response.status}`)
      const data = await response.json() as CloudState
      setState(data)
      setError('')
    } catch (err: any) {
      setError(err?.message ?? 'Cloud research feed unavailable')
    } finally {
      setRefreshing(false)
    }
  }

  useEffect(() => {
    void refresh()
    const id = window.setInterval(() => void refresh(), 60_000)
    return () => window.clearInterval(id)
  }, [])

  const history = state?.history ?? []
  const scoreSeries = useMemo(() => history.map((row) => Number(row.average_score ?? 0)), [history])
  const safety = state?.paper_only === true && state?.live_execution === false
  const generated = state?.generated_at ? new Date(state.generated_at) : null
  const ageMinutes = generated ? Math.max(0, Math.floor((Date.now() - generated.getTime()) / 60_000)) : null

  return (
    <div style={{ paddingTop: 16, display: 'grid', gap: 14 }}>
      <Panel title="Cloud Autonomy Observatory" tag="SCHEDULED PAPER RESEARCH" accented>
        <div style={{ padding: 16, display: 'grid', gridTemplateColumns: '1.3fr 0.7fr', gap: 16 }}>
          <div>
            <div style={{ fontFamily: 'var(--font-display)', fontSize: 38, color: 'var(--amber)', letterSpacing: '0.05em' }}>
              ALWAYS-ON RESEARCH BRAIN
            </div>
            <div style={{ marginTop: 6, maxWidth: 800, fontFamily: 'var(--font-mono)', fontSize: 10, lineHeight: 1.65, color: 'var(--text-muted)' }}>
              GitHub Actions runs an independent cloud paper-research cycle twice per hour and publishes the latest research state to a dedicated branch. This page observes that state even when the browser-driven Mission Control loop is off. Scheduled jobs can start late during platform congestion.
            </div>
          </div>
          <div style={box}>
            <Status label="CLOUD FEED" value={state?.status?.toUpperCase() ?? (error ? 'OFFLINE' : 'CONNECTING')} good={state?.status === 'ready'} />
            <Status label="CADENCE" value="2× / HOUR" good />
            <Status label="PAPER ONLY" value={safety ? 'VERIFIED' : 'CHECKING'} good={safety} />
            <Status label="LIVE ORDERS" value="DISABLED" good />
            <Status label="LAST CYCLE" value={ageMinutes == null ? '—' : `${ageMinutes} MIN AGO`} good={ageMinutes != null && ageMinutes < 90} />
            <button onClick={() => void refresh()} disabled={refreshing} style={buttonStyle}>{refreshing ? 'REFRESHING…' : 'REFRESH CLOUD FEED'}</button>
          </div>
        </div>
      </Panel>

      {error && (
        <div style={{ border: '1px solid var(--red)', padding: 10, fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--red)' }}>
          CLOUD FEED: {error}. Mission Control and local paper research remain available.
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1.15fr 0.85fr', gap: 14 }}>
        <Panel title="Autonomy Pulse" tag={`${history.length} CYCLES`} accented>
          <div style={{ padding: 14 }}>
            <HistoryGraph values={scoreSeries.length ? scoreSeries : [0, 0, 0, 0, 0]} />
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0,1fr))', gap: 8, marginTop: 8 }}>
              <Metric label="Cycles" value={String(history.length)} />
              <Metric label="Symbols" value={String(state?.summary?.symbols_evaluated ?? state?.ranking?.length ?? 0)} />
              <Metric label="Sim Trades" value={String(state?.summary?.total_simulated_trades ?? 0)} />
              <Metric label="Worst DD" value={pct(state?.summary?.worst_drawdown)} />
            </div>
          </div>
        </Panel>

        <Panel title="Current Research Leader" tag={state?.cycle_id ?? 'WAITING'} accented>
          <div style={{ padding: 14, minHeight: 235, display: 'grid', alignContent: 'center', gap: 9 }}>
            <div style={{ fontFamily: 'var(--font-display)', fontSize: 42, color: 'var(--amber)' }}>{state?.leader?.symbol ?? '—'}</div>
            <Status label="ROBUSTNESS SCORE" value={num(state?.leader?.score, 4)} />
            <Status label="SIMULATED TRADES" value={num(state?.leader?.trades, 0)} />
            <Status label="WIN RATE" value={pct(state?.leader?.win_rate)} />
            <Status label="RETURN" value={pct(state?.leader?.return_pct)} />
            <Status label="MAX DRAWDOWN" value={pct(state?.leader?.max_drawdown)} />
            <div style={{ marginTop: 4, fontFamily: 'var(--font-mono)', color: 'var(--text-muted)', fontSize: 8 }}>
              Synthetic research ranking only — not a forecast or trading instruction.
            </div>
          </div>
        </Panel>
      </div>

      <Panel title="Seven-Market Research Matrix" tag={`${state?.ranking?.length ?? 0} INSTRUMENTS`} accented>
        <div style={{ padding: 14, overflowX: 'auto' }}>
          {(state?.ranking?.length ?? 0) > 0 ? (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontFamily: 'var(--font-mono)', fontSize: 9 }}>
              <thead><tr>{['#', 'SYMBOL', 'SCORE', 'TRADES', 'WIN', 'RETURN', 'DD', 'PF', 'EXPECTANCY', 'NET P/L'].map((h) => <th key={h} style={th}>{h}</th>)}</tr></thead>
              <tbody>{state!.ranking.map((row, i) => (
                <tr key={row.symbol} style={{ borderTop: '1px solid var(--border)' }}>
                  <td style={td}>{i + 1}</td>
                  <td style={{ ...td, color: i === 0 ? 'var(--amber)' : 'var(--text-dim)', fontWeight: 700 }}>{row.symbol}</td>
                  <td style={td}>{num(row.score, 4)}</td>
                  <td style={td}>{row.trades}</td>
                  <td style={td}>{pct(row.win_rate)}</td>
                  <td style={td}>{pct(row.return_pct)}</td>
                  <td style={td}>{pct(row.max_drawdown)}</td>
                  <td style={td}>{num(row.profit_factor, 2)}</td>
                  <td style={td}>{money(row.expectancy)}</td>
                  <td style={td}>{money(row.net_profit)}</td>
                </tr>
              ))}</tbody>
            </table>
          ) : <Empty text="Cloud research has not published a completed cycle yet." />}
        </div>
      </Panel>

      <Panel title="Agent Council" tag="DETERMINISTIC RESEARCH AGENTS" accented>
        <div style={{ padding: 14, display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 8 }}>
          {(state?.agent_council ?? []).length ? state!.agent_council.map((agent) => (
            <div key={agent.agent} style={box}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-dim)' }}>{agent.agent.toUpperCase()}</span>
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 8, color: agent.status === 'CAUTION' ? 'var(--amber)' : 'var(--green)' }}>{agent.status}</span>
              </div>
              <div style={{ marginTop: 8, fontFamily: 'var(--font-mono)', fontSize: 9, lineHeight: 1.55, color: 'var(--text-muted)' }}>{agent.message}</div>
            </div>
          )) : <Empty text="The cloud Agent Council will appear after the first scheduled cycle." />}
        </div>
      </Panel>

      <Panel title="Cloud Cycle Ledger" tag="ROLLING 24H APPROX.">
        <div style={{ maxHeight: 330, overflowY: 'auto' }}>
          {history.length ? [...history].reverse().map((row) => (
            <div key={row.generated_at} style={{ padding: '9px 12px', display: 'grid', gridTemplateColumns: '170px 90px 1fr 110px 110px', gap: 10, borderBottom: '1px solid var(--border)', fontFamily: 'var(--font-mono)', fontSize: 8 }}>
              <span style={{ color: 'var(--text-muted)' }}>{new Date(row.generated_at).toLocaleString()}</span>
              <span style={{ color: 'var(--amber)' }}>{row.leader ?? '—'}</span>
              <span style={{ color: 'var(--text-dim)' }}>AVG SCORE {num(row.average_score, 4)}</span>
              <span style={{ color: 'var(--text-dim)' }}>{row.total_trades} TRADES</span>
              <span style={{ color: 'var(--text-dim)' }}>DD {pct(row.worst_drawdown)}</span>
            </div>
          )) : <Empty text="No cloud-cycle history yet." />}
        </div>
      </Panel>
    </div>
  )
}

function HistoryGraph({ values }: { values: number[] }) {
  const width = 800
  const height = 210
  const pad = 18
  const min = Math.min(...values, -0.01)
  const max = Math.max(...values, 0.01)
  const range = Math.max(max - min, 0.0001)
  const points = values.map((v, i) => {
    const x = pad + (i / Math.max(values.length - 1, 1)) * (width - pad * 2)
    const y = height - pad - ((v - min) / range) * (height - pad * 2)
    return `${x},${y}`
  }).join(' ')
  const zeroY = height - pad - ((0 - min) / range) * (height - pad * 2)
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width="100%" height="210" role="img" aria-label="autonomous research score history">
      {[0.25, 0.5, 0.75].map((p) => <line key={p} x1={pad} x2={width - pad} y1={p * height} y2={p * height} stroke="var(--border)" />)}
      <line x1={pad} x2={width - pad} y1={zeroY} y2={zeroY} stroke="var(--text-muted)" strokeDasharray="4 5" />
      <polyline points={points} fill="none" stroke="var(--amber)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
      {points.split(' ').map((point, i) => {
        const [x, y] = point.split(',')
        return <circle key={i} cx={x} cy={y} r="2.5" fill="var(--amber)" />
      })}
      <text x={pad} y={12} fill="var(--text-muted)" fontSize="8">MAX {max.toFixed(4)}</text>
      <text x={pad} y={height - 3} fill="var(--text-muted)" fontSize="8">MIN {min.toFixed(4)}</text>
    </svg>
  )
}

function Status({ label, value, good }: { label: string; value: string; good?: boolean }) {
  return <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, padding: '4px 0', fontFamily: 'var(--font-mono)', fontSize: 9 }}><span style={{ color: 'var(--text-muted)' }}>{label}</span><span style={{ color: good ? 'var(--green)' : 'var(--text-dim)' }}>{value}</span></div>
}
function Metric({ label, value }: { label: string; value: string }) {
  return <div style={box}><div style={{ fontFamily: 'var(--font-mono)', fontSize: 8, color: 'var(--text-muted)' }}>{label.toUpperCase()}</div><div style={{ marginTop: 5, fontFamily: 'var(--font-display)', fontSize: 22, color: 'var(--text-dim)' }}>{value}</div></div>
}
function Empty({ text }: { text: string }) { return <div style={{ padding: 16, fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-muted)' }}>{text}</div> }
function num(v: any, digits = 2) { const n = Number(v); return Number.isFinite(n) ? n.toFixed(digits) : '—' }
function pct(v: any) { const n = Number(v); return Number.isFinite(n) ? `${(n * 100).toFixed(2)}%` : '—' }
function money(v: any) { const n = Number(v); return Number.isFinite(n) ? `$${n.toFixed(2)}` : '—' }

const box = { border: '1px solid var(--border)', padding: 10, background: 'linear-gradient(180deg, rgba(255,255,255,0.015), transparent)' }
const buttonStyle = { width: '100%', marginTop: 9, border: '1px solid var(--amber)', background: 'var(--amber-dim)', color: 'var(--amber)', padding: '8px 10px', fontFamily: 'var(--font-mono)', fontSize: 8, cursor: 'pointer' }
const th = { textAlign: 'left' as const, padding: '7px 6px', color: 'var(--text-muted)', fontWeight: 500, whiteSpace: 'nowrap' as const }
const td = { padding: '8px 6px', color: 'var(--text-dim)', whiteSpace: 'nowrap' as const }
