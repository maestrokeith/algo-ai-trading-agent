import { useEffect, useState } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import { useBotStore } from '@/store/botStore'
import { useThemeStore, resolveTheme } from '@/store/themeStore'
import { useAuthStore } from '@/store/authStore'
import { UserSwitcher } from './UserSwitcher'

function useUtcClock() {
  const [time, setTime] = useState('')
  useEffect(() => {
    const tick = () => setTime(new Date().toLocaleTimeString('en-GB', { timeZone: 'UTC', hour12: false }) + ' UTC')
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [])
  return time
}

export function Header() {
  const { status, loopCount } = useBotStore()
  const { mode: themeMode, setMode: setThemeMode } = useThemeStore()
  const { userId, role, logout } = useAuthStore()
  const navigate = useNavigate()
  const clock = useUtcClock()
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

  const statusColor = status === 'stopped' ? 'var(--red)' : 'var(--green)'
  const themeIcon = themeMode === 'system' ? '⊙' : resolvedTheme === 'dark' ? '◐' : '○'

  return (
    <header style={{ position: 'relative', display: 'flex', alignItems: 'center', flexWrap: 'wrap', padding: '14px 20px', borderBottom: '1px solid var(--border)', gap: 12, zIndex: 10 }}>
      <div style={{ position: 'absolute', bottom: -1, left: 20, width: 420, maxWidth: '80vw', height: 1, background: 'linear-gradient(90deg, var(--amber), transparent)' }} />

      <div style={{ fontFamily: 'var(--font-display)', fontSize: 25, letterSpacing: '0.08em', color: 'var(--amber)', flexShrink: 0 }}>
        ALGO<span style={{ color: 'var(--text-dim)' }}>SPHERE</span>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 7, fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.12em', padding: '4px 9px', border: `1px solid ${statusColor}`, color: statusColor, flexShrink: 0 }}>
        <span style={{ width: 6, height: 6, borderRadius: '50%', background: statusColor }} />
        PAPER RESEARCH · {status.toUpperCase()}
      </div>

      <nav style={{ display: 'flex', gap: 2, flexWrap: 'wrap' }}>
        {[
          { to: '/command', label: 'COMMAND' },
          { to: '/autonomy', label: 'AUTONOMY' },
          { to: '/engine', label: 'ENGINE' },
          { to: '/dashboard', label: 'DASHBOARD' },
          { to: '/agents', label: 'AGENTS' },
          { to: '/trades', label: 'JOURNAL' },
          { to: '/settings', label: 'SETTINGS' },
        ].map(({ to, label }) => (
          <NavLink
            key={to}
            to={to}
            style={({ isActive }) => ({
              fontFamily: 'var(--font-mono)',
              fontSize: 9,
              letterSpacing: '0.14em',
              padding: '5px 9px',
              color: isActive ? 'var(--amber)' : 'var(--text-muted)',
              background: isActive ? 'var(--amber-dim)' : 'transparent',
              border: `1px solid ${isActive ? 'var(--amber-dim)' : 'transparent'}`,
              textDecoration: 'none',
            })}
          >
            {label}
          </NavLink>
        ))}
      </nav>

      {role === 'admin' && <UserSwitcher />}

      <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-muted)' }}>LOOP {loopCount.toLocaleString()}</span>
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--green)', border: '1px solid var(--border)', padding: '4px 8px' }}>LIVE ORDERS OFF</span>
        <button onClick={cycleTheme} title={`Theme: ${themeMode}`} style={buttonStyle}>{themeIcon}</button>
        {userId && (
          <>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-dim)', border: '1px solid var(--border)', padding: '4px 8px' }}>
              {role === 'admin' ? '★ ' : ''}{userId} · PAPER
            </span>
            <button onClick={handleLogout} style={buttonStyle}>OUT</button>
          </>
        )}
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-dim)', minWidth: 78 }}>{clock}</span>
      </div>
    </header>
  )
}

const buttonStyle = {
  fontFamily: 'var(--font-mono)',
  fontSize: 9,
  border: '1px solid var(--border)',
  background: 'transparent',
  color: 'var(--text-muted)',
  padding: '4px 8px',
  cursor: 'pointer',
}
