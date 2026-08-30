import { useGateLog } from '@/hooks/useGateLog'
import { Panel } from '@/components/layout/Panel'

export function GateLogPanel() {
  const { entries } = useGateLog()

  return (
    <Panel title="GATE SKIP LOG" tag="LAST PASS" style={{ gridColumn: 3, gridRow: 3, display: 'flex', flexDirection: 'column' }}>
      <div style={{ flex: 1, overflowY: 'auto' }}>
        {entries.map((entry, i) => (
          <div
            key={entry.id}
            style={{
              padding: '6px 13px',
              borderBottom: i < entries.length - 1 ? '1px solid var(--border)' : 'none',
              display: 'flex',
              gap: 9,
              alignItems: 'baseline',
              animation: i === 0 ? 'fade-in-down 0.4s ease' : 'none',
            }}
          >
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-muted)', flexShrink: 0 }}>
              {entry.time}
            </span>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--amber)', fontWeight: 500, flexShrink: 0, minWidth: 34 }}>
              {entry.symbol}
            </span>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-dim)', lineHeight: 1.4 }}>
              {entry.message}
            </span>
          </div>
        ))}
      </div>
    </Panel>
  )
}
