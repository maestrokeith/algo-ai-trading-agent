import { Panel } from '@/components/layout/Panel'

interface Gauge {
  label: string
  current: number | string
  max: number | string
  pct: number
  level: 'safe' | 'warn' | 'danger'
}

const GAUGES: Gauge[] = [
  { label: 'DAILY LOSS LIMIT', current: '$320',  max: '$800',  pct: 40, level: 'safe' },
  { label: 'MAX DRAWDOWN',     current: '3.2%',  max: '8%',    pct: 40, level: 'safe' },
  { label: 'PDT TRADES (5D)', current: '2',     max: '3',     pct: 67, level: 'warn' },
  { label: 'POSITION COUNT',   current: '3',     max: '6',     pct: 50, level: 'safe' },
]

const LEVEL_COLORS: Record<string, string> = {
  safe:   'var(--green)',
  warn:   'var(--amber)',
  danger: 'var(--red)',
}

function RiskGauge({ label, current, max, pct, level }: Gauge) {
  const color = LEVEL_COLORS[level]
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.16em', textTransform: 'uppercase', color: 'var(--text-muted)' }}>
          {label}
        </span>
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12, fontWeight: 500, color }}>
          {current} / {max}
        </span>
      </div>
      <div style={{ height: 3, background: 'var(--border)', position: 'relative', overflow: 'hidden' }}>
        <div style={{
          position: 'absolute',
          top: 0, left: 0,
          height: '100%',
          width: `${pct}%`,
          background: color,
          transition: 'width 0.8s cubic-bezier(0.4,0,0.2,1)',
        }} />
      </div>
    </div>
  )
}

export function RiskCapsPanel() {
  return (
    <Panel title="RISK CAPS" tag="12-GATE" style={{ gridColumn: 3, gridRow: 2 }}>
      <div style={{ padding: '13px 15px', display: 'flex', flexDirection: 'column', gap: 13 }}>
        {GAUGES.map((g) => <RiskGauge key={g.label} {...g} />)}

        {/* Safe mode badge */}
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '7px 11px',
          background: 'var(--amber-dim)',
          border: '1px solid rgba(255,179,0,0.2)',
          marginTop: 2,
        }}>
          <div style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--amber)', flexShrink: 0 }} />
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, letterSpacing: '0.12em', textTransform: 'uppercase', color: 'var(--amber)' }}>
            SAFE MODE: INACTIVE
          </span>
        </div>
      </div>
    </Panel>
  )
}
