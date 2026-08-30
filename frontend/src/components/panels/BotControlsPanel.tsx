import { useBotStatus } from '@/hooks/useBotStatus'
import { Panel } from '@/components/layout/Panel'
import type { BotStatus } from '@/store/botStore'

const BTN_CONFIG: Record<BotStatus, { label: string; color: string }> = {
  active:  { label: '▶ START',  color: 'var(--green)' },
  paused:  { label: '⏸ PAUSE',  color: 'var(--amber)' },
  stopped: { label: '■ STOP',   color: 'var(--red)' },
}

export function BotControlsPanel() {
  const { status, mode, setStatus, setMode, countdownLabel, loopCount } = useBotStatus()

  return (
    <Panel title="BOT CONTROLS" tag="v2.1" accented style={{ gridColumn: 3, gridRow: 1 }}>
      <div style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 9 }}>

        {/* Action buttons */}
        {(['paused', 'active', 'stopped'] as BotStatus[]).map((action) => {
          const { label, color } = BTN_CONFIG[action]
          const isActive = status === action
          return (
            <button
              key={action}
              onClick={() => setStatus(action)}
              style={{
                width: '100%',
                padding: '10px 14px',
                fontFamily: 'var(--font-display)',
                fontSize: 17,
                letterSpacing: '0.14em',
                textTransform: 'uppercase',
                border: `1px solid ${color}4d`,
                background: isActive ? `${color}1a` : 'transparent',
                color,
                cursor: 'pointer',
                transition: 'all 0.15s ease',
                boxShadow: isActive ? `0 0 14px ${color}22` : 'none',
              }}
              onMouseEnter={(e) => { e.currentTarget.style.background = `${color}1a` }}
              onMouseLeave={(e) => { if (!isActive) e.currentTarget.style.background = 'transparent' }}
            >
              {label}
            </button>
          )
        })}

        {/* Paper / Live toggle */}
        <div style={{ display: 'flex', border: '1px solid var(--border)', marginTop: 4 }}>
          {(['paper', 'live'] as const).map((m, i) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              style={{
                flex: 1,
                padding: '7px 0',
                fontFamily: 'var(--font-mono)',
                fontSize: 10,
                letterSpacing: '0.14em',
                textTransform: 'uppercase',
                cursor: 'pointer',
                border: 'none',
                borderRight: i === 0 ? '1px solid var(--border)' : 'none',
                background: mode === m ? 'var(--amber-mid)' : 'transparent',
                color: mode === m ? 'var(--amber)' : 'var(--text-muted)',
                transition: 'all 0.15s',
              }}
            >
              {m.toUpperCase()}
            </button>
          ))}
        </div>

        {/* Strategy */}
        <div style={{ padding: 12, background: 'var(--bg-panel-alt)', border: '1px solid var(--border)', marginTop: 2 }}>
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.18em', textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: 5 }}>
            ACTIVE STRATEGY
          </div>
          <div style={{ fontFamily: 'var(--font-display)', fontSize: 20, color: 'var(--amber)', letterSpacing: '0.06em' }}>
            BALANCED
          </div>
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-dim)', marginTop: 2 }}>
            Trend-following · 12-gate pipeline
          </div>
        </div>

        {/* Countdown */}
        <div style={{ marginTop: 4 }}>
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.16em', textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: 6 }}>
            NEXT LOOP IN
          </div>
          <div style={{ fontFamily: 'var(--font-display)', fontSize: 30, color: 'var(--text-dim)', letterSpacing: '0.06em' }}>
            {countdownLabel}
          </div>
        </div>

        {/* Loop counter */}
        <div style={{ display: 'flex', gap: 7, fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-muted)', letterSpacing: '0.08em' }}>
          <span>LOOP</span>
          <strong style={{ color: 'var(--text-dim)', fontWeight: 500 }}>{loopCount.toLocaleString()}</strong>
        </div>
      </div>
    </Panel>
  )
}
