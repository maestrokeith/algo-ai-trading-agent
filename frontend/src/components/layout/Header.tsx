import { useEffect, useState } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import { useBotStore } from '@/store/botStore'
import { useThemeStore, resolveTheme } from '@/store/themeStore'
import { useAuthStore } from '@/store/authStore'
import { UserSwitcher } from './UserSwitcher'
import type { RegimeScores } from '@/types'

const MOCK_REGIME: RegimeScores = {
  label: 'bullish',
  spy: 0.72,
  qqq: 0.68,
  vix: 14.2,
}

const STATUS_LABELS: Record<string, string> = {
  active: 'ACTIVE',
  paused: 'PAUSED',
  stopped: 'STOPPED',
}

const STATUS_COLORS: Record<string, string> = {
  active:  'var(--amber)',
  paused:  'var(--amber)',
  stopped: 'var(--red)',
}

const REGIME_COLORS: Record<string, string> = {
  bullish: 'var(--green)',
  neutral: 'var(--amber)',
  bearish: 'var(--red)',
}

function useEstClock() {
  const [time, setTime] = useState('')
  useEffect(() => {
    const tick = () => {
      const opts: Intl.DateTimeFormatOptions = {
        timeZone: 'America/New_York',
        hour12: false,
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      }
      setTime(new Date().toLocaleTimeString('en-US', opts) + ' EST')
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [])
  return time
}

export function Header() {
  const { status, mode, loopCount } = useBotStore()
  const { mode: themeMode, setMode: setThemeMode } = useThemeStore()
  const { userId, role, paper, logout } = useAuthStore()
  const navigate = useNavigate()
  const clock = useEstClock()
  const regime = MOCK_REGIME
  const statusColor = STATUS_COLORS[status]

  const resolvedTheme = resolveTheme(themeMode)

  function handleLogout() {
    logout()
    navigate('/login')
  }

  function cycleTheme() {
    if (themeMode === 'system') setThemeMode('dark')
    else if (themeMode === 'dark') setThemeMode('light')
    else setThemeMode('system')
  }

  const themeIcon = themeMode === 'system' ? '⊙' : resolvedTheme === 'dark' ? '◐' : '○'
  const themeLabel = themeMode === 'system' ? 'SYS' : themeMode === 'dark' ? 'DARK' : 'LITE'

  return (
    <header style={{
      position: 'relative',
      display: 'flex',
      alignItems: 'center',
      padding: '14px 20px',
      borderBottom: '1px solid var(--border)',
      gap: 0,
      zIndex: 10,
    }}>
      {/* amber underline accent */}
      <div style={{
        position: 'absolute',
        bottom: -1,
        left: 20,
        width: 320,
        height: 1,
        background: 'linear-gradient(90deg, var(--amber) 0%, transparent 100%)',
      }} />

      {/* Logo */}
      <div style={{
        fontFamily: 'var(--font-display)',
        fontSize: 26,
        letterSpacing: '0.08em',
        color: 'var(--amber)',
        marginRight: 28,
        textShadow: '0 0 24px rgba(255,179,0,0.4)',
        lineHeight: 1,
        flexShrink: 0,
      }}>
        ALGO<span style={{ color: 'var(--text-dim)' }}>SPHERE</span>
      </div>

      {/* Status pill */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 7,
        fontFamily: 'var(--font-mono)',
        fontSize: 10,
        fontWeight: 500,
        letterSpacing: '0.12em',
        textTransform: 'uppercase',
        padding: '4px 10px',
        border: `1px solid ${statusColor}4d`,
        background: `${statusColor}14`,
        color: statusColor,
        flexShrink: 0,
      }}>
        <span style={{
          width: 6,
          height: 6,
          borderRadius: '50%',
          background: statusColor,
          flexShrink: 0,
          animation: status === 'active' ? 'pulse-dot 2s ease-in-out infinite' : 'none',
        }} />
        {mode.toUpperCase()} · {STATUS_LABELS[status]}
      </div>

      {/* Divider */}
      <div style={{ width: 1, height: 26, background: 'var(--border)', margin: '0 18px', flexShrink: 0 }} />

      {/* Nav links */}
      <nav style={{ display: 'flex', gap: 2, marginRight: 18 }}>
        {[
          { to: '/dashboard', label: 'DASHBOARD' },
          { to: '/trades',    label: 'TRADES' },
          { to: '/settings',  label: 'SETTINGS' },
        ].map(({ to, label }) => (
          <NavLink
            key={to}
            to={to}
            style={({ isActive }) => ({
              fontFamily: 'var(--font-mono)',
              fontSize: 9,
              letterSpacing: '0.18em',
              textTransform: 'uppercase',
              padding: '4px 10px',
              color: isActive ? 'var(--amber)' : 'var(--text-muted)',
              background: isActive ? 'var(--amber-dim)' : 'transparent',
              border: `1px solid ${isActive ? 'var(--amber-dim)' : 'transparent'}`,
              textDecoration: 'none',
              transition: 'all 0.15s',
            })}
          >
            {label}
          </NavLink>
        ))}
      </nav>

      {/* Divider */}
      <div style={{ width: 1, height: 26, background: 'var(--border)', margin: '0 18px', flexShrink: 0 }} />

      {/* Regime badge */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexShrink: 0 }}>
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.16em', textTransform: 'uppercase', color: 'var(--text-dim)' }}>
          REGIME
        </span>
        <span style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          fontWeight: 500,
          letterSpacing: '0.1em',
          textTransform: 'uppercase',
          color: REGIME_COLORS[regime.label],
        }}>
          {regime.label.toUpperCase()}
        </span>
      </div>

      {/* Regime scores */}
      <div style={{ display: 'flex', gap: 14, marginLeft: 14, flexShrink: 0 }}>
        {[
          { ticker: 'SPY', value: `${regime.spy > 0 ? '+' : ''}${regime.spy.toFixed(2)}` },
          { ticker: 'QQQ', value: `${regime.qqq > 0 ? '+' : ''}${regime.qqq.toFixed(2)}` },
          { ticker: 'VIX', value: regime.vix.toFixed(1) },
        ].map(({ ticker, value }) => (
          <div key={ticker} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2 }}>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 8, color: 'var(--text-muted)', letterSpacing: '0.08em' }}>
              {ticker}
            </span>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-dim)' }}>
              {value}
            </span>
          </div>
        ))}
      </div>

      {/* Admin user switcher */}
      {role === 'admin' && (
        <>
          <div style={{ width: 1, height: 26, background: 'var(--border)', margin: '0 12px', flexShrink: 0 }} />
          <UserSwitcher />
        </>
      )}

      {/* Right meta */}
      <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 20, flexShrink: 0 }}>
        <div style={{ display: 'flex', gap: 6, fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-muted)', letterSpacing: '0.08em' }}>
          <span>LOOP</span>
          <strong style={{ color: 'var(--text-dim)', fontWeight: 500 }}>{loopCount.toLocaleString()}</strong>
        </div>

        {/* Theme toggle */}
        <button
          onClick={cycleTheme}
          title={`Theme: ${themeMode} — click to cycle`}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 5,
            fontFamily: 'var(--font-mono)',
            fontSize: 9,
            letterSpacing: '0.14em',
            textTransform: 'uppercase',
            padding: '4px 9px',
            border: '1px solid var(--border)',
            background: 'transparent',
            color: 'var(--text-muted)',
            cursor: 'pointer',
            transition: 'all 0.15s',
            flexShrink: 0,
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.borderColor = 'var(--amber)'
            e.currentTarget.style.color = 'var(--amber)'
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.borderColor = 'var(--border)'
            e.currentTarget.style.color = 'var(--text-muted)'
          }}
        >
          <span style={{ fontSize: 11 }}>{themeIcon}</span>
          {themeLabel}
        </button>

        {/* User pill + logout */}
        {userId && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <div style={{
              fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.1em',
              textTransform: 'uppercase', color: 'var(--text-dim)',
              padding: '3px 8px', border: '1px solid var(--border)',
            }}>
              {role === 'admin' ? '★ ' : ''}{userId}{paper ? ' · PAPER' : ' · LIVE'}
            </div>
            <button
              onClick={handleLogout}
              title="Sign out"
              style={{
                fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.14em',
                textTransform: 'uppercase', padding: '3px 8px',
                border: '1px solid var(--border)', background: 'transparent',
                color: 'var(--text-muted)', cursor: 'pointer',
              }}
              onMouseEnter={(e) => { e.currentTarget.style.borderColor = 'var(--red)'; e.currentTarget.style.color = 'var(--red)' }}
              onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.color = 'var(--text-muted)' }}
            >
              OUT
            </button>
          </div>
        )}

        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text-dim)', letterSpacing: '0.06em', minWidth: 88, textAlign: 'right' }}>
          {clock}
        </div>
      </div>
    </header>
  )
}
