import { useEffect, useMemo, useState } from 'react'
import { apiClient } from '@/lib/api'
import { Panel } from '@/components/layout/Panel'

type ResearchStatus = {
  status: string
  mode: string
  paper_only: boolean
  live_execution: boolean
  instruments: string[]
  modules: string[]
}

type BacktestResult = {
  mode: string
  live_execution: boolean
  symbol: string
  bars: number
  data_source?: string
  metrics: Record<string, number | null>
  instrument_stats: Array<Record<string, string | number | null>>
  session_stats: Array<Record<string, string | number | null>>
  monte_carlo: {
    simulations: number
    median_ending_equity: number | null
    p05_ending_equity: number | null
    p95_max_drawdown: number | null
  }
  walk_forward: Array<{
    fold: number
    train_bars: number
    test_bars: number
    train_start: string | null
    test_end: string | null
  }>
  trades: Array<Record<string, unknown>>
}

type CoreConfig = {
  initial_equity: number
  risk_fraction: number
  max_total_open_risk_fraction: number
  max_positions: number
  max_positions_per_symbol: number
  htf_fast_ema: number
  htf_slow_ema: number
  ltf_fast_ema: number
  ltf_slow_ema: number
  rsi_period: number
  atr_period: number
  volume_ma_period: number
  swing_lookback: number
  stop_atr_multiple: number
  reward_risk: number
  breakeven_trigger_atr: number
  trailing_trigger_atr: number
  trailing_atr_multiple: number
  max_spread_multiple: number
  slippage_ticks: number
}

const DEFAULT_CONFIG: CoreConfig = {
  initial_equity: 10000,
  risk_fraction: 0.005,
  max_total_open_risk_fraction: 0.02,
  max_positions: 3,
  max_positions_per_symbol: 1,
  htf_fast_ema: 50,
  htf_slow_ema: 200,
  ltf_fast_ema: 9,
  ltf_slow_ema: 21,
  rsi_period: 14,
  atr_period: 14,
  volume_ma_period: 20,
  swing_lookback: 5,
  stop_atr_multiple: 1.5,
  reward_risk: 1.25,
  breakeven_trigger_atr: 1.0,
  trailing_trigger_atr: 1.5,
  trailing_atr_multiple: 1.0,
  max_spread_multiple: 2.5,
  slippage_ticks: 0.25,
}

const MODULES = [
  ['01–15', 'Trend & Multi-Timeframe', 'M1 execution · M5/M15 alignment · 9/21 + 50/200 EMA'],
  ['16–30', 'Liquidity / Structure / Volatility', 'Swing structure · ATR regime · spread-aware filtering'],
  ['31–40', 'Momentum & Volume', 'RSI · tick-volume confirmation · confluence gates'],
  ['41–55', 'Risk & Position Sizing', '0–1% risk cap · ATR stop · portfolio exposure limits'],
  ['56–70', 'Execution Model', 'Paper fills · spread/slippage · deterministic order simulation'],
  ['71–85', 'Trade Management', 'Break-even · ATR trail · partial / time-exit research'],
  ['86–100', 'Sessions & Validation', 'Session stats · walk-forward · Monte-Carlo · journal'],
]

const inputStyle = {
  width: '100%',
  boxSizing: 'border-box' as const,
  background: 'var(--bg)',
  color: 'var(--text-dim)',
  border: '1px solid var(--border)',
  padding: '7px 8px',
  fontFamily: 'var(--font-mono)',
  fontSize: 11,
}

const buttonStyle = {
  border: '1px solid var(--amber)',
  background: 'var(--amber-dim)',
  color: 'var(--amber)',
  fontFamily: 'var(--font-mono)',
  fontSize: 10,
  letterSpacing: '0.12em',
  padding: '9px 12px',
  cursor: 'pointer',
}

export function Engine() {
  const [status, setStatus] = useState<ResearchStatus | null>(null)
  const [symbol, setSymbol] = useState('XAUUSD')
  const [config, setConfig] = useState<CoreConfig>(DEFAULT_CONFIG)
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState('Ready for paper research.')

  useEffect(() => {
    apiClient.get<ResearchStatus>('/api/research/status')
      .then((r) => setStatus(r.data))
      .catch((err) => setMessage(`API unavailable: ${err?.message ?? 'unknown error'}`))
  }, [])

  const modulesReady = status?.modules?.length ?? 0
  const safe = status?.paper_only === true && status?.live_execution === false

  const metrics = useMemo(() => {
    const m = result?.metrics ?? {}
    return [
      ['TRADES', formatNumber(m.trades, 0)],
      ['WIN RATE', formatPct(m.win_rate)],
      ['PROFIT FACTOR', formatNumber(m.profit_factor, 2)],
      ['EXPECTANCY', formatMoney(m.expectancy)],
      ['NET P/L', formatMoney(m.net_profit)],
      ['MAX DD', formatPct(m.max_drawdown)],
      ['MAX LOSS RUN', formatNumber(m.max_consecutive_losses, 0)],
      ['RETURN', formatPct(m.return_pct)],
    ]
  }, [result])

  function updateConfig<K extends keyof CoreConfig>(key: K, value: number) {
    setConfig((prev) => ({ ...prev, [key]: value }))
  }

  async function runDemo() {
    setLoading(true)
    setMessage(`Running deterministic ${symbol} paper backtest…`)
    try {
      const response = await apiClient.post<BacktestResult>('/api/research/demo-backtest', {
        symbol,
        bars: 5000,
        seed: 7,
        config,
        monte_carlo_simulations: 250,
      })
      setResult(response.data)
      setMessage('Paper backtest complete. Synthetic demo data only — not a market forecast.')
    } catch (err: any) {
      setMessage(err?.response?.data?.detail ?? err?.message ?? 'Backtest failed')
    } finally {
      setLoading(false)
    }
  }

  async function uploadCsv(file: File) {
    setLoading(true)
    setMessage(`Parsing ${file.name}…`)
    try {
      const text = await file.text()
      const bars = parseCsv(text)
      if (bars.length < 3500) throw new Error('CSV needs at least 3500 one-minute bars for MTF warm-up.')
      setMessage(`Running paper backtest on ${bars.length.toLocaleString()} uploaded bars…`)
      const response = await apiClient.post<BacktestResult>('/api/research/backtest', {
        symbol,
        bars,
        config,
        monte_carlo_simulations: 250,
      })
      setResult(response.data)
      setMessage(`Historical paper backtest complete: ${file.name}`)
    } catch (err: any) {
      setMessage(err?.response?.data?.detail ?? err?.message ?? 'CSV backtest failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ paddingTop: 16, display: 'grid', gap: 14 }}>
      <Panel title="Quant Research Engine" tag="PAPER / SIMULATION ONLY" accented>
        <div style={{ padding: 16, display: 'grid', gridTemplateColumns: '1.2fr 1fr', gap: 16 }}>
          <div>
            <div style={{ fontFamily: 'var(--font-display)', fontSize: 32, color: 'var(--amber)', letterSpacing: '0.05em' }}>
              ALGO ENGINE
            </div>
            <div style={{ marginTop: 5, fontFamily: 'var(--font-mono)', color: 'var(--text-muted)', fontSize: 10, lineHeight: 1.6 }}>
              Multi-timeframe FX/metals research, paper execution, risk simulation, backtesting, walk-forward validation,
              Monte-Carlo diagnostics and trade-journal inspection from one site.
            </div>
          </div>
          <div style={{ border: `1px solid ${safe ? 'var(--green)' : 'var(--red)'}`, padding: 12, fontFamily: 'var(--font-mono)', fontSize: 10 }}>
            <div style={{ color: safe ? 'var(--green)' : 'var(--red)', letterSpacing: '0.14em' }}>
              {safe ? 'SAFETY BOUNDARY VERIFIED' : 'SAFETY STATUS UNVERIFIED'}
            </div>
            <div style={{ color: 'var(--text-dim)', marginTop: 7 }}>Live execution: {status?.live_execution ? 'ENABLED' : 'DISABLED'}</div>
            <div style={{ color: 'var(--text-dim)' }}>Paper only: {status?.paper_only ? 'YES' : 'UNKNOWN'}</div>
            <div style={{ color: 'var(--text-dim)' }}>Research modules: {modulesReady}/7</div>
          </div>
        </div>
      </Panel>

      <div style={{ display: 'grid', gridTemplateColumns: '320px minmax(0, 1fr)', gap: 14 }}>
        <div style={{ display: 'grid', gap: 14, alignContent: 'start' }}>
          <Panel title="Command" tag={loading ? 'RUNNING' : 'READY'} accented>
            <div style={{ padding: 14, display: 'grid', gap: 10 }}>
              <label style={labelStyle}>Instrument</label>
              <select value={symbol} onChange={(e) => setSymbol(e.target.value)} style={inputStyle}>
                {(status?.instruments ?? ['XAUUSD', 'XAGUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD']).map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
              <button disabled={loading} onClick={runDemo} style={{ ...buttonStyle, opacity: loading ? 0.5 : 1 }}>
                {loading ? 'RUNNING…' : 'RUN DEMO BACKTEST'}
              </button>
              <label style={{ ...buttonStyle, textAlign: 'center' as const }}>
                IMPORT 1M CSV
                <input
                  type="file"
                  accept=".csv,text/csv"
                  hidden
                  disabled={loading}
                  onChange={(e) => e.target.files?.[0] && uploadCsv(e.target.files[0])}
                />
              </label>
              <div style={{ fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-muted)', lineHeight: 1.5 }}>
                CSV columns: timestamp, open, high, low, close, volume, optional spread. Minimum 3,500 M1 bars.
              </div>
            </div>
          </Panel>

          <Panel title="Core Risk Controls" tag="STRICT">
            <div style={{ padding: 14, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              <NumberField label="Equity" value={config.initial_equity} onChange={(v) => updateConfig('initial_equity', v)} />
              <NumberField label="Risk %" value={config.risk_fraction * 100} step={0.1} max={1} onChange={(v) => updateConfig('risk_fraction', Math.min(1, v) / 100)} />
              <NumberField label="Total Risk %" value={config.max_total_open_risk_fraction * 100} step={0.5} onChange={(v) => updateConfig('max_total_open_risk_fraction', v / 100)} />
              <NumberField label="Max Positions" value={config.max_positions} step={1} onChange={(v) => updateConfig('max_positions', Math.max(1, Math.round(v)))} />
              <NumberField label="Stop ATR" value={config.stop_atr_multiple} step={0.1} onChange={(v) => updateConfig('stop_atr_multiple', v)} />
              <NumberField label="Target R" value={config.reward_risk} step={0.05} onChange={(v) => updateConfig('reward_risk', v)} />
              <NumberField label="BE Trigger ATR" value={config.breakeven_trigger_atr} step={0.1} onChange={(v) => updateConfig('breakeven_trigger_atr', v)} />
              <NumberField label="Trail ATR" value={config.trailing_atr_multiple} step={0.1} onChange={(v) => updateConfig('trailing_atr_multiple', v)} />
              <NumberField label="Spread ×" value={config.max_spread_multiple} step={0.1} onChange={(v) => updateConfig('max_spread_multiple', v)} />
              <NumberField label="Slippage ticks" value={config.slippage_ticks} step={0.05} onChange={(v) => updateConfig('slippage_ticks', v)} />
            </div>
          </Panel>
        </div>

        <div style={{ display: 'grid', gap: 14, alignContent: 'start' }}>
          <Panel title="100-Parameter Architecture" tag="7 SUBSYSTEMS">
            <div style={{ padding: 14, display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 8 }}>
              {MODULES.map(([range, name, detail]) => (
                <div key={range} style={{ border: '1px solid var(--border)', padding: 10 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
                    <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--amber)', fontSize: 9 }}>{range}</span>
                    <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-dim)', fontSize: 9 }}>{name}</span>
                  </div>
                  <div style={{ marginTop: 5, fontFamily: 'var(--font-mono)', color: 'var(--text-muted)', fontSize: 9, lineHeight: 1.45 }}>{detail}</div>
                </div>
              ))}
            </div>
          </Panel>

          <Panel title="Performance" tag={result ? `${result.symbol} · ${result.bars.toLocaleString()} BARS` : 'NO RUN'} accented>
            <div style={{ padding: 14, display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 8 }}>
              {metrics.map(([label, value]) => (
                <Metric key={label} label={label} value={value} />
              ))}
            </div>
          </Panel>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
            <Panel title="Monte Carlo" tag={result ? `${result.monte_carlo.simulations} PATHS` : 'WAITING'}>
              <div style={{ padding: 14, display: 'grid', gap: 8 }}>
                <Row label="Median ending equity" value={formatMoney(result?.monte_carlo.median_ending_equity)} />
                <Row label="5th percentile equity" value={formatMoney(result?.monte_carlo.p05_ending_equity)} />
                <Row label="95th percentile DD" value={formatPct(result?.monte_carlo.p95_max_drawdown)} />
              </div>
            </Panel>

            <Panel title="Walk Forward" tag="5 FOLDS">
              <div style={{ padding: 14, display: 'grid', gap: 6 }}>
                {(result?.walk_forward ?? []).map((fold) => (
                  <Row key={fold.fold} label={`Fold ${fold.fold}`} value={`${fold.train_bars} train / ${fold.test_bars} test`} />
                ))}
                {!result && <div style={emptyStyle}>Run a backtest to generate validation folds.</div>}
              </div>
            </Panel>
          </div>

          <Panel title="Research Journal" tag={result ? `${result.trades.length} RECENT TRADES` : 'WAITING'}>
            <div style={{ padding: 14, overflowX: 'auto' }}>
              {result?.trades?.length ? (
                <table style={{ width: '100%', borderCollapse: 'collapse', fontFamily: 'var(--font-mono)', fontSize: 9 }}>
                  <thead>
                    <tr style={{ color: 'var(--text-muted)' }}>
                      {['SYMBOL', 'SIDE', 'ENTRY', 'EXIT', 'P/L', 'SESSION', 'REASON'].map((h) => <th key={h} style={{ textAlign: 'left', padding: '6px 5px', borderBottom: '1px solid var(--border)' }}>{h}</th>)}
                    </tr>
                  </thead>
                  <tbody>
                    {result.trades.slice().reverse().slice(0, 20).map((t, i) => (
                      <tr key={i} style={{ color: 'var(--text-dim)', borderBottom: '1px solid var(--border)' }}>
                        <td style={td}>{String(t.symbol ?? '')}</td>
                        <td style={td}>{Number(t.side) > 0 ? 'LONG' : 'SHORT'}</td>
                        <td style={td}>{formatNumber(t.entry_price as number, 5)}</td>
                        <td style={td}>{formatNumber(t.exit_price as number, 5)}</td>
                        <td style={td}>{formatMoney(t.pnl as number)}</td>
                        <td style={td}>{String(t.session ?? '')}</td>
                        <td style={td}>{String(t.exit_reason ?? '')}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : <div style={emptyStyle}>No closed paper trades in the current run.</div>}
            </div>
          </Panel>
        </div>
      </div>

      <div style={{ border: '1px solid var(--border)', padding: 10, fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-muted)' }}>
        {message}
      </div>
    </div>
  )
}

const labelStyle = { fontFamily: 'var(--font-mono)', fontSize: 8, color: 'var(--text-muted)', letterSpacing: '0.12em', textTransform: 'uppercase' as const }
const emptyStyle = { fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-muted)' }
const td = { padding: '6px 5px' }

function NumberField({ label, value, onChange, step = 0.1, max }: { label: string; value: number; onChange: (value: number) => void; step?: number; max?: number }) {
  return (
    <label>
      <div style={{ ...labelStyle, marginBottom: 4 }}>{label}</div>
      <input type="number" value={value} min={0} max={max} step={step} onChange={(e) => onChange(Number(e.target.value))} style={inputStyle} />
    </label>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ border: '1px solid var(--border)', padding: 10 }}>
      <div style={labelStyle}>{label}</div>
      <div style={{ marginTop: 5, fontFamily: 'var(--font-mono)', fontSize: 17, color: 'var(--text-dim)' }}>{value}</div>
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, fontFamily: 'var(--font-mono)', fontSize: 9 }}>
      <span style={{ color: 'var(--text-muted)' }}>{label}</span>
      <span style={{ color: 'var(--text-dim)', textAlign: 'right' }}>{value}</span>
    </div>
  )
}

function formatNumber(value: number | null | undefined, digits = 2) {
  if (value == null || !Number.isFinite(Number(value))) return '—'
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits })
}

function formatPct(value: number | null | undefined) {
  if (value == null || !Number.isFinite(Number(value))) return '—'
  return `${(Number(value) * 100).toFixed(2)}%`
}

function formatMoney(value: number | null | undefined) {
  if (value == null || !Number.isFinite(Number(value))) return '—'
  return Number(value).toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 2 })
}

function parseCsv(text: string) {
  const lines = text.replace(/^\uFEFF/, '').trim().split(/\r?\n/)
  if (lines.length < 2) return []
  const headers = lines[0].split(',').map((h) => h.trim().toLowerCase())
  const required = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
  for (const name of required) {
    if (!headers.includes(name)) throw new Error(`CSV missing required column: ${name}`)
  }
  return lines.slice(1).filter(Boolean).map((line) => {
    const values = line.split(',')
    const row: Record<string, string> = {}
    headers.forEach((h, i) => { row[h] = values[i]?.trim() ?? '' })
    return {
      timestamp: row.timestamp,
      open: Number(row.open),
      high: Number(row.high),
      low: Number(row.low),
      close: Number(row.close),
      volume: Number(row.volume),
      spread: row.spread ? Number(row.spread) : undefined,
    }
  })
}
