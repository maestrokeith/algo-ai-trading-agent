import { usePositions } from '@/hooks/usePositions'
import { Panel } from '@/components/layout/Panel'
import type { Position } from '@/types'

function SideBadge({ side }: { side: Position['side'] }) {
  const isLong = side === 'long'
  return (
    <span style={{
      display: 'inline-block',
      fontFamily: 'var(--font-mono)',
      fontSize: 9,
      letterSpacing: '0.14em',
      padding: '2px 6px',
      border: `1px solid ${isLong ? 'rgba(0,212,139,0.35)' : 'rgba(255,59,92,0.35)'}`,
      background: isLong ? 'var(--green-dim)' : 'var(--red-dim)',
      color: isLong ? 'var(--green)' : 'var(--red)',
      textTransform: 'uppercase',
    }}>
      {side}
    </span>
  )
}

function BarsPip({ count }: { count: number }) {
  const heights = [6, 9, 12, 15, 18]
  return (
    <span style={{ display: 'inline-flex', gap: 2, alignItems: 'flex-end', verticalAlign: 'middle' }}>
      {heights.slice(0, Math.min(count, 5)).map((h, i) => (
        <span key={i} style={{ display: 'block', width: 3, height: h, background: 'var(--text-muted)', borderRadius: 1 }} />
      ))}
    </span>
  )
}

const TH: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontSize: 9,
  fontWeight: 500,
  letterSpacing: '0.2em',
  textTransform: 'uppercase',
  color: 'var(--text-muted)',
  textAlign: 'right',
  padding: '9px 14px',
  borderBottom: '1px solid var(--border)',
  background: 'var(--bg-panel)',
  whiteSpace: 'nowrap',
}

const TD: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontSize: 12,
  padding: '10px 14px',
  color: 'var(--text-primary)',
  whiteSpace: 'nowrap',
  textAlign: 'right',
  borderBottom: '1px solid var(--border)',
}

export function PositionsPanel() {
  const { positions } = usePositions()

  return (
    <Panel
      title="ACTIVE POSITIONS"
      tag={`${positions.length} OPEN`}
      accented
      style={{ gridColumn: 2, gridRow: '1 / 3', display: 'flex', flexDirection: 'column' }}
    >
      <div style={{ flex: 1, overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {['SYMBOL', 'SIDE', 'SHARES', 'ENTRY', 'CURRENT', 'UNRLZD P&L', '% RETURN', 'BARS', 'ATR%'].map((h, i) => (
                <th key={h} style={{ ...TH, textAlign: i === 0 ? 'left' : 'right' }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {positions.map((pos) => (
              <tr
                key={pos.symbol}
                style={{ cursor: 'default', transition: 'background 0.15s' }}
                onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--amber-dim)')}
                onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
              >
                <td style={{ ...TD, textAlign: 'left', color: 'var(--amber)', fontWeight: 500, letterSpacing: '0.05em' }}>
                  {pos.symbol}
                </td>
                <td style={TD}><SideBadge side={pos.side} /></td>
                <td style={{ ...TD, color: 'var(--text-dim)' }}>{pos.shares}</td>
                <td style={{ ...TD, color: 'var(--text-dim)' }}>${pos.entryPrice.toFixed(2)}</td>
                <td style={TD}>${pos.currentPrice.toFixed(2)}</td>
                <td style={{ ...TD, color: pos.unrealizedPnl >= 0 ? 'var(--green)' : 'var(--red)' }}>
                  {pos.unrealizedPnl >= 0 ? '+' : '-'}${Math.abs(pos.unrealizedPnl).toFixed(2)}
                </td>
                <td style={{ ...TD, color: pos.returnPct >= 0 ? 'var(--green)' : 'var(--red)' }}>
                  {pos.returnPct >= 0 ? '+' : ''}{pos.returnPct.toFixed(2)}%
                </td>
                <td style={TD}>
                  <BarsPip count={pos.barsHeld} />
                  <span style={{ marginLeft: 5, color: 'var(--text-dim)' }}>{pos.barsHeld}</span>
                </td>
                <td style={{ ...TD, color: 'var(--text-dim)' }}>{pos.atrPct.toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Footer stats */}
      <div style={{ padding: '9px 14px', borderTop: '1px solid var(--border)', display: 'flex', gap: 22 }}>
        {[
          ['EXPOSURE', '$58,442', 'var(--amber)'],
          ['LARGEST', 'NVDA · 33%', 'var(--text-dim)'],
          ['AVG BARS', '2.0', 'var(--text-dim)'],
        ].map(([label, value, color]) => (
          <div key={label} style={{ display: 'flex', gap: 7, fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-muted)', letterSpacing: '0.08em' }}>
            <span>{label}</span>
            <strong style={{ color, fontWeight: 500 }}>{value}</strong>
          </div>
        ))}
      </div>
    </Panel>
  )
}
