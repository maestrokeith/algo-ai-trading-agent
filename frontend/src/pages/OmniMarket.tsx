import { useEffect, useMemo, useState, type CSSProperties } from 'react'
import { Panel } from '@/components/layout/Panel'
import { apiClient } from '@/lib/api'

type OmniStatus = {
  status: string
  paper_only: boolean
  live_execution: boolean
  markets: Record<string, string[]>
  modules: string[]
}

type AgentVote = { name: string; vote: string; score: number }
type MarketRow = {
  asset_class: string
  symbol: string
  price: number
  probability_up: number
  confidence: number
  score: number
  regime: string
  risk_score: number
  trend_strength: number
  liquidity_quality: number
  agents?: AgentVote[]
}
type ScanResponse = { rows: MarketRow[]; leader: MarketRow | null }
type OptionsResponse = {
  model: {
    call_value: number; put_value: number; call_delta: number; put_delta: number
    gamma: number; vega_per_vol_point: number; call_theta_per_day: number
    put_theta_per_day: number; expected_move: number
  }
  payoff: { underlying_price: number; long_call_pnl: number; long_put_pnl: number }[]
}

const box: CSSProperties = { border: '1px solid var(--border)', background: 'rgba(255,255,255,0.015)', padding: 11 }
const btn: CSSProperties = { border: '1px solid var(--amber)', background: 'var(--amber-dim)', color: 'var(--amber)', padding: '8px 10px', fontFamily: 'var(--font-mono)', fontSize: 9, cursor: 'pointer' }
const input: CSSProperties = { width: '100%', boxSizing: 'border-box', border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text-dim)', padding: '8px 9px', fontFamily: 'var(--font-mono)', fontSize: 9 }
const th: CSSProperties = { textAlign: 'left', padding: '7px 6px', color: 'var(--text-muted)', fontWeight: 500, whiteSpace: 'nowrap' }
const td: CSSProperties = { padding: '8px 6px', color: 'var(--text-dim)', whiteSpace: 'nowrap' }

export function OmniMarket() {
  const [status, setStatus] = useState<OmniStatus | null>(null)
  const [scan, setScan] = useState<ScanResponse | null>(null)
  const [sniper, setSniper] = useState<any>(null)
  const [memes, setMemes] = useState<any[]>([])
  const [futures, setFutures] = useState<any[]>([])
  const [options, setOptions] = useState<OptionsResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [fxSymbol, setFxSymbol] = useState('XAUUSD')
  const [underlying, setUnderlying] = useState('SPY')
  const [spot, setSpot] = useState(560)
  const [strike, setStrike] = useState(560)
  const [days, setDays] = useState(30)
  const [iv, setIv] = useState(0.22)

  useEffect(() => {
    apiClient.get<OmniStatus>('/api/omni/status').then(r => setStatus(r.data)).catch(() => setStatus(null))
    void runAll()
  }, [])

  async function runAll() {
    if (busy) return
    setBusy(true)
    try {
      const [a, b, c, d] = await Promise.all([
        apiClient.post<ScanResponse>('/api/omni/scan', { markets: ['forex', 'futures', 'crypto', 'memecoin'], seed: 7 }),
        apiClient.post('/api/omni/sniper', { symbol: fxSymbol, seed: 7 }),
        apiClient.post('/api/omni/memecoin-radar', { seed: 7 }),
        apiClient.post('/api/omni/futures-lab', { seed: 7 }),
      ])
      setScan(a.data); setSniper(b.data); setMemes(c.data.rows ?? []); setFutures(d.data.rows ?? [])
    } finally { setBusy(false) }
  }

  async function runSniper() {
    setBusy(true)
    try { setSniper((await apiClient.post('/api/omni/sniper', { symbol: fxSymbol, seed: Date.now() % 100000 })).data) }
    finally { setBusy(false) }
  }

  async function runOptions() {
    setBusy(true)
    try { setOptions((await apiClient.post<OptionsResponse>('/api/omni/options-lab', { underlying, spot, strike, days, iv, rate: 0.04 })).data) }
    finally { setBusy(false) }
  }

  const leaders = useMemo(() => scan?.rows?.slice(0, 12) ?? [], [scan])
  const agents: AgentVote[] = scan?.leader?.agents ?? sniper?.agents ?? []

  return <div style={{ paddingTop: 16, display: 'grid', gap: 14 }}>
    <Panel title="AlgoSphere Omni-Market Intelligence" tag={busy ? 'SCANNING' : 'AUTONOMOUS PAPER RESEARCH'} accented>
      <div style={{ padding: 16, display: 'grid', gridTemplateColumns: '1.45fr .75fr', gap: 14 }}>
        <div>
          <div style={{ fontFamily: 'var(--font-display)', fontSize: 38, color: 'var(--amber)', letterSpacing: '.04em' }}>MARKET GALAXY</div>
          <div style={{ marginTop: 7, maxWidth: 850, fontFamily: 'var(--font-mono)', fontSize: 10, lineHeight: 1.65, color: 'var(--text-muted)' }}>Forex sniper research, futures regimes, crypto and memecoin risk radar, theoretical options analytics, probabilistic forecasts and agent consensus. Synthetic research only; no live-order path exists.</div>
          <div style={{ marginTop: 12, display: 'flex', gap: 6, flexWrap: 'wrap' }}>{Object.entries(status?.markets ?? {}).map(([m, symbols]) => <span key={m} style={{ ...box, padding: '5px 8px', fontFamily: 'var(--font-mono)', fontSize: 8 }}>{m.toUpperCase()} · {symbols.length}</span>)}</div>
        </div>
        <div style={box}>
          <Status label="ENGINE" value={status?.status?.toUpperCase() ?? 'CONNECTING'} />
          <Status label="PAPER ONLY" value={status?.paper_only ? 'VERIFIED' : 'CHECKING'} />
          <Status label="LIVE EXECUTION" value={status?.live_execution ? 'ON' : 'HARD OFF'} />
          <Status label="MODULES" value={String(status?.modules?.length ?? 0)} />
          <button onClick={() => void runAll()} disabled={busy} style={{ ...btn, width: '100%', marginTop: 8 }}>SCAN EVERYTHING</button>
        </div>
      </div>
    </Panel>

    <div style={{ display: 'grid', gridTemplateColumns: '1.45fr .75fr', gap: 14 }}>
      <Panel title="Omni Ranking" tag={`${leaders.length} LEADERS`} accented>
        <div style={{ overflowX: 'auto' }}><table style={{ width: '100%', borderCollapse: 'collapse', fontFamily: 'var(--font-mono)', fontSize: 9 }}>
          <thead><tr>{['#','MARKET','SYMBOL','SCORE','P(UP)','CONF','REGIME','RISK'].map(h => <th key={h} style={th}>{h}</th>)}</tr></thead>
          <tbody>{leaders.map((r, i) => <tr key={`${r.asset_class}-${r.symbol}`} style={{ borderTop: '1px solid var(--border)' }}><td style={td}>{i + 1}</td><td style={td}>{r.asset_class.toUpperCase()}</td><td style={{ ...td, color: i === 0 ? 'var(--amber)' : 'var(--text-dim)' }}>{r.symbol}</td><td style={td}>{r.score.toFixed(1)}</td><td style={td}>{pct(r.probability_up)}</td><td style={td}>{pct(r.confidence)}</td><td style={td}>{r.regime}</td><td style={td}>{r.risk_score.toFixed(1)}</td></tr>)}</tbody>
        </table></div>
      </Panel>
      <Panel title="Agent Council" tag={`${agents.length} AGENTS`}>
        <div style={{ padding: 12, display: 'grid', gap: 7 }}>{agents.length ? agents.map(a => <div key={a.name} style={{ display: 'grid', gridTemplateColumns: '1fr 70px 48px', gap: 8, fontFamily: 'var(--font-mono)', fontSize: 9 }}><span>{a.name}</span><span style={{ color: a.vote === 'approve' ? 'var(--green)' : a.vote === 'reject' ? 'var(--red)' : 'var(--amber)' }}>{a.vote.toUpperCase()}</span><span>{a.score}</span></div>) : <Empty text="Run an omni scan to populate the council." />}</div>
      </Panel>
    </div>

    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
      <Panel title="Forex Sniper Lab" tag={sniper?.research_direction ?? 'WAIT'} accented>
        <div style={{ padding: 13, display: 'grid', gap: 10 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 110px', gap: 8 }}><select value={fxSymbol} onChange={e => setFxSymbol(e.target.value)} style={input}>{['XAUUSD','XAGUSD','EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD'].map(s => <option key={s}>{s}</option>)}</select><button onClick={() => void runSniper()} disabled={busy} style={btn}>ANALYZE</button></div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 8 }}><Metric label="Sniper" value={num(sniper?.sniper_score)} /><Metric label="P(UP)" value={pct(sniper?.probability_up)} /><Metric label="Confidence" value={pct(sniper?.confidence)} /><Metric label="Risk" value={num(sniper?.risk_score)} /></div>
          <Bars values={sniper?.components ?? {}} />
        </div>
      </Panel>
      <Panel title="Probability Engine" tag={scan?.leader?.symbol ?? 'WAITING'} accented>
        <div style={{ padding: 13 }}><Gauge value={scan?.leader?.probability_up ?? 0.5} /><div style={{ marginTop: 10, fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-muted)', lineHeight: 1.55 }}>Probabilities come from deterministic synthetic features and agent confluence. They are not certainty or real-market signals.</div></div>
      </Panel>
    </div>

    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
      <Panel title="Memecoin Radar" tag={`${memes.length} TOKENS`}><div style={{ padding: 10, display: 'grid', gridTemplateColumns: 'repeat(2,minmax(0,1fr))', gap: 8 }}>{memes.map(m => <div key={m.symbol} style={box}><div style={{ fontFamily: 'var(--font-display)', fontSize: 18, color: 'var(--amber)' }}>{m.symbol}</div><Mini label="QUALITY" value={m.quality_score} /><Mini label="MOMENTUM" value={m.momentum_score} /><Mini label="RUG RISK" value={m.rug_risk} /><Mini label="LIQUIDITY" value={m.liquidity_quality * 100} /></div>)}</div></Panel>
      <Panel title="Futures Regime Matrix" tag={`${futures.length} CONTRACTS`}><div style={{ padding: 10, display: 'grid', gap: 6 }}>{futures.map(f => <div key={f.symbol} style={{ ...box, display: 'grid', gridTemplateColumns: '75px 1fr 1fr 55px', gap: 8, fontFamily: 'var(--font-mono)', fontSize: 9 }}><strong style={{ color: 'var(--amber)' }}>{f.symbol}</strong><span>{f.regime}</span><span>{f.session_bias}</span><span>{Number(f.score).toFixed(1)}</span></div>)}</div></Panel>
    </div>

    <Panel title="Options Intelligence Lab" tag="THEORETICAL MODEL" accented>
      <div style={{ padding: 13, display: 'grid', gridTemplateColumns: '.75fr 1.25fr', gap: 14 }}>
        <div style={{ display: 'grid', gap: 7, alignContent: 'start' }}>
          <Field label="UNDERLYING"><input value={underlying} onChange={e => setUnderlying(e.target.value)} style={input} /></Field>
          <Field label="SPOT"><input type="number" value={spot} onChange={e => setSpot(Number(e.target.value))} style={input} /></Field>
          <Field label="STRIKE"><input type="number" value={strike} onChange={e => setStrike(Number(e.target.value))} style={input} /></Field>
          <Field label="DAYS"><input type="number" value={days} onChange={e => setDays(Number(e.target.value))} style={input} /></Field>
          <Field label="IV"><input type="number" step="0.01" value={iv} onChange={e => setIv(Number(e.target.value))} style={input} /></Field>
          <button onClick={() => void runOptions()} disabled={busy} style={btn}>RUN OPTIONS MODEL</button>
        </div>
        <div><div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 8 }}><Metric label="Call" value={num(options?.model.call_value)} /><Metric label="Put" value={num(options?.model.put_value)} /><Metric label="Delta" value={num(options?.model.call_delta)} /><Metric label="Gamma" value={num(options?.model.gamma, 4)} /><Metric label="Vega" value={num(options?.model.vega_per_vol_point)} /><Metric label="Theta" value={num(options?.model.call_theta_per_day)} /><Metric label="Exp Move" value={num(options?.model.expected_move)} /><Metric label="Mode" value="PAPER" /></div><div style={{ marginTop: 10 }}><Payoff data={options?.payoff ?? []} /></div></div>
      </div>
    </Panel>
  </div>
}

function Status({ label, value }: { label: string; value: string }) { return <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', fontFamily: 'var(--font-mono)', fontSize: 9 }}><span style={{ color: 'var(--text-muted)' }}>{label}</span><span style={{ color: 'var(--green)' }}>{value}</span></div> }
function Metric({ label, value }: { label: string; value: string }) { return <div style={box}><div style={{ fontFamily: 'var(--font-mono)', fontSize: 8, color: 'var(--text-muted)' }}>{label}</div><div style={{ marginTop: 4, fontFamily: 'var(--font-display)', fontSize: 18, color: 'var(--text-dim)' }}>{value}</div></div> }
function Mini({ label, value }: { label: string; value: number }) { const v = Math.max(0, Math.min(100, Number(value) || 0)); return <div style={{ marginTop: 6 }}><div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: 'var(--font-mono)', fontSize: 8 }}><span>{label}</span><span>{v.toFixed(1)}</span></div><div style={{ height: 4, background: 'var(--border)', marginTop: 3 }}><div style={{ width: `${v}%`, height: '100%', background: 'var(--amber)' }} /></div></div> }
function Bars({ values }: { values: Record<string, number> }) { return <div style={{ display: 'grid', gap: 5 }}>{Object.entries(values).map(([k,v]) => <Mini key={k} label={k.replaceAll('_',' ').toUpperCase()} value={v} />)}</div> }
function Gauge({ value }: { value: number }) { const v = Math.max(0, Math.min(1, Number(value) || 0)); return <div><div style={{ fontFamily: 'var(--font-display)', fontSize: 48, color: 'var(--amber)' }}>{(v * 100).toFixed(1)}%</div><div style={{ height: 12, marginTop: 12, border: '1px solid var(--border)', position: 'relative' }}><div style={{ width: `${v * 100}%`, height: '100%', background: 'var(--amber-dim)' }} /><div style={{ position: 'absolute', left: '50%', top: 0, bottom: 0, width: 1, background: 'var(--text-muted)' }} /></div></div> }
function Field({ label, children }: { label: string; children: React.ReactNode }) { return <label style={{ display: 'grid', gap: 4, fontFamily: 'var(--font-mono)', fontSize: 8, color: 'var(--text-muted)' }}>{label}{children}</label> }
function Payoff({ data }: { data: { underlying_price: number; long_call_pnl: number; long_put_pnl: number }[] }) { if (!data.length) return <Empty text="Run the theoretical options model to render payoff curves." />; const w=720,h=190,p=16; const xs=data.map(d=>d.underlying_price), ys=data.flatMap(d=>[d.long_call_pnl,d.long_put_pnl]); const minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys); const sx=(x:number)=>p+(x-minX)/(maxX-minX||1)*(w-p*2), sy=(y:number)=>h-p-(y-minY)/(maxY-minY||1)*(h-p*2); const call=data.map(d=>`${sx(d.underlying_price)},${sy(d.long_call_pnl)}`).join(' '), put=data.map(d=>`${sx(d.underlying_price)},${sy(d.long_put_pnl)}`).join(' '); return <svg viewBox={`0 0 ${w} ${h}`} width="100%" height="190"><line x1={p} x2={w-p} y1={sy(0)} y2={sy(0)} stroke="var(--border)"/><polyline points={call} fill="none" stroke="var(--amber)" strokeWidth="2"/><polyline points={put} fill="none" stroke="var(--text-dim)" strokeWidth="2"/><text x={p} y={12} fill="var(--text-muted)" fontSize="9">CALL / PUT EXPIRY P&amp;L</text></svg> }
function Empty({ text }: { text: string }) { return <div style={{ padding: 12, fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-muted)' }}>{text}</div> }
const pct = (v: unknown) => Number.isFinite(Number(v)) ? `${(Number(v) * 100).toFixed(1)}%` : '—'
const num = (v: unknown, d = 2) => Number.isFinite(Number(v)) ? Number(v).toFixed(d) : '—'
