import { usePortfolio } from '@/hooks/usePortfolio'
import { Sparkline } from '@/components/charts/Sparkline'
import { RegimeRadar } from '@/components/charts/RegimeRadar'
import { Panel } from '@/components/layout/Panel'

const REGIME_COLORS: Record<string, string> = {
  bullish: 'var(--green)',
  neutral: 'var(--amber)',
  bearish: 'var(--red)',
}

function fmt(n: number, decimals = 2) {
  return Math.abs(n).toFixed(decimals)
}

function fmtSign(n: number) {
  return n >= 0 ? '+' : '-'
}

export function PortfolioPanel() {
  const { stats, regime } = usePortfolio()
  const whole = Math.floor(stats.equity).toLocaleString('en-US')
  const cents = String(Math.round((stats.equity % 1) * 100)).padStart(2, '0')

  return (
    <Panel title="PORTFOLIO · default" tag="ALPACA PAPER" accented style={{ gridColumn: 1, gridRow: '1 / 3', display: 'flex', flexDirection: 'column' }}>

      {/* Equity */}
      <div style={{ padding: '22px 20px 18px', borderBottom: '1px solid var(--border)' }}>
        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: 10 }}>
          ACCOUNT EQUITY
        </div>
        <div style={{
          fontFamily: 'var(--font-display)',
          fontSize: 62,
          lineHeight: 1,
          color: 'var(--amber)',
          textShadow: '0 0 40px rgba(255,179,0,0.3)',
          letterSpacing: '0.02em',
          display: 'flex',
          alignItems: 'baseline',
          gap: 3,
        }} key={stats.equity}>
          <span style={{ fontSize: 26, color: 'var(--text-dim)' }}>$</span>
          <span>{whole}</span>
          <span style={{ fontSize: 30, opacity: 0.7 }}>.{cents}</span>
        </div>
      </div>

      {/* Stat grid */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr' }}>
        {[
          {
            label: 'Day P&L',
            value: `${fmtSign(stats.dayPnl)}$${fmt(stats.dayPnl)}`,
            sub: `${fmtSign(stats.dayPnlPct)}${fmt(stats.dayPnlPct)}%`,
            color: stats.dayPnl >= 0 ? 'var(--green)' : 'var(--red)',
          },
          {
            label: 'Unrealized',
            value: `+$${fmt(stats.unrealized)}`,
            sub: '3 positions',
            color: 'var(--green)',
          },
          {
            label: 'Total Return',
            value: `+${stats.totalReturn.toFixed(1)}%`,
            sub: 'from $100,000',
            color: 'var(--green)',
          },
          {
            label: 'Win Rate',
            value: `${stats.winRate.toFixed(1)}%`,
            sub: `${stats.winCount} of ${stats.totalTrades} trades`,
            color: 'var(--text-primary)',
          },
        ].map(({ label, value, sub, color }, i) => (
          <div key={label} style={{
            padding: '14px 20px',
            borderRight: i % 2 === 0 ? '1px solid var(--border)' : 'none',
            borderBottom: i < 2 ? '1px solid var(--border)' : 'none',
          }}>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.16em', textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: 5 }}>
              {label}
            </div>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 19, fontWeight: 500, letterSpacing: '0.02em', color }}>
              {value}
            </div>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-muted)', marginTop: 2 }}>
              {sub}
            </div>
          </div>
        ))}
      </div>

      {/* Sparkline */}
      <div style={{ padding: '12px 20px 14px', borderBottom: '1px solid var(--border)' }}>
        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: 8 }}>
          EQUITY CURVE · TODAY
        </div>
        <Sparkline />
      </div>

      {/* Radar */}
      <div style={{ padding: '12px 16px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8, borderBottom: '1px solid var(--border)', flex: 1, justifyContent: 'center' }}>
        <RegimeRadar />
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.16em', textTransform: 'uppercase', color: 'var(--text-muted)' }}>
            REGIME
          </span>
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, fontWeight: 500, color: REGIME_COLORS[regime.label], letterSpacing: '0.1em', textTransform: 'uppercase' }}>
            {regime.label}
          </span>
        </div>
      </div>
    </Panel>
  )
}
