import { useTrades } from '@/hooks/useTrades'
import { Panel } from '@/components/layout/Panel'
import type { Trade } from '@/types'

const EXIT_STYLES: Record<Trade['exitReason'], { label: string; color: string; bg: string; border: string }> = {
  'profit-target': { label: 'PROFIT TARGET', color: 'var(--green)', bg: 'var(--green-dim)', border: 'rgba(0,212,139,0.25)' },
  'stop-loss':     { label: 'STOP LOSS',     color: 'var(--red)',   bg: 'var(--red-dim)',   border: 'rgba(255,59,92,0.25)'  },
  'trail-stop':    { label: 'TRAIL STOP',    color: 'var(--blue)',  bg: 'rgba(59,139,255,0.07)', border: 'rgba(59,139,255,0.25)' },
  'time-exit':     { label: 'TIME EXIT',     color: 'var(--amber)', bg: 'var(--amber-dim)', border: 'rgba(255,179,0,0.25)'  },
}

function ExitReasonBadge({ reason }: { reason: Trade['exitReason'] }) {
  const s = EXIT_STYLES[reason]
  return (
    <span style={{
      fontFamily: 'var(--font-mono)',
      fontSize: 9,
      letterSpacing: '0.1em',
      textTransform: 'uppercase',
      padding: '2px 7px',
      border: `1px solid ${s.border}`,
      background: s.bg,
      color: s.color,
      whiteSpace: 'nowrap',
    }}>
      {s.label}
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
  fontSize: 11,
  padding: '8px 14px',
  color: 'var(--text-primary)',
  whiteSpace: 'nowrap',
  textAlign: 'right',
  borderBottom: '1px solid var(--border)',
}

export function TradesPanel() {
  const { trades } = useTrades()

  return (
    <Panel
      title="RECENT TRADES"
      tag={`TODAY · ${trades.length} FILLS`}
      accented
      style={{ gridColumn: '1 / 3', gridRow: 3 }}
    >
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {['TIME', 'SYMBOL', 'SIDE', 'SHARES', 'ENTRY', 'EXIT', 'P&L', 'RETURN', 'EXIT REASON', 'BARS'].map((h, i) => (
                <th key={h} style={{ ...TH, textAlign: i <= 1 ? 'left' : 'right' }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {trades.map((t) => (
              <tr
                key={`${t.time}-${t.symbol}`}
                style={{ cursor: 'default', transition: 'background 0.15s' }}
                onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--amber-dim)')}
                onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
              >
                <td style={{ ...TD, textAlign: 'left', color: 'var(--text-dim)' }}>{t.time}</td>
                <td style={{ ...TD, textAlign: 'left', color: 'var(--amber)', fontWeight: 500 }}>{t.symbol}</td>
                <td style={TD}>
                  <span style={{
                    display: 'inline-block',
                    fontFamily: 'var(--font-mono)',
                    fontSize: 9,
                    letterSpacing: '0.14em',
                    padding: '2px 6px',
                    border: `1px solid ${t.side === 'long' ? 'rgba(0,212,139,0.35)' : 'rgba(255,59,92,0.35)'}`,
                    background: t.side === 'long' ? 'var(--green-dim)' : 'var(--red-dim)',
                    color: t.side === 'long' ? 'var(--green)' : 'var(--red)',
                    textTransform: 'uppercase',
                  }}>
                    {t.side}
                  </span>
                </td>
                <td style={{ ...TD, color: 'var(--text-dim)' }}>{t.shares}</td>
                <td style={{ ...TD, color: 'var(--text-dim)' }}>${t.entryPrice.toFixed(2)}</td>
                <td style={TD}>${t.exitPrice.toFixed(2)}</td>
                <td style={{ ...TD, color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>
                  {t.pnl >= 0 ? '+' : '-'}${Math.abs(t.pnl).toFixed(2)}
                </td>
                <td style={{ ...TD, color: t.returnPct >= 0 ? 'var(--green)' : 'var(--red)' }}>
                  {t.returnPct >= 0 ? '+' : ''}{t.returnPct.toFixed(2)}%
                </td>
                <td style={TD}><ExitReasonBadge reason={t.exitReason} /></td>
                <td style={{ ...TD, color: 'var(--text-dim)' }}>{t.barsHeld}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}
