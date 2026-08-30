import { Outlet } from 'react-router-dom'
import { Header } from './Header'
import { useEffect } from 'react'
import { useBotStore } from '@/store/botStore'

/** Scanline overlay rendered as a fixed layer */
function Scanlines() {
  return (
    <div
      aria-hidden
      style={{
        position: 'fixed',
        inset: 0,
        background: 'repeating-linear-gradient(0deg, rgba(0,0,0,0.06) 0px, rgba(0,0,0,0.06) 1px, transparent 1px, transparent 3px)',
        pointerEvents: 'none',
        zIndex: 100,
        opacity: 0.4,
      }}
    />
  )
}

export function AppShell() {
  const tickCountdown = useBotStore((s) => s.tickCountdown)

  // Global countdown ticker
  useEffect(() => {
    const id = setInterval(tickCountdown, 1000)
    return () => clearInterval(id)
  }, [tickCountdown])

  return (
    <>
      <Scanlines />
      <div style={{ position: 'relative', zIndex: 1, minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
        <Header />
        <main style={{ flex: 1, padding: '0 20px 24px', display: 'flex', flexDirection: 'column' }}>
          <Outlet />
        </main>
        <footer style={{
          padding: '10px 20px 12px',
          borderTop: '1px solid var(--border)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}>
          <div style={{ display: 'flex', gap: 24 }}>
            {[
              ['BROKER', 'ALPACA · PAPER'],
              ['UNIVERSE', '124 SYMBOLS'],
              ['INTERVAL', '10 MIN'],
              ['STRATEGY', 'TREND-FOLLOWING'],
            ].map(([label, value]) => (
              <div key={label} style={{ display: 'flex', gap: 7, fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-muted)', letterSpacing: '0.08em' }}>
                <span>{label}</span>
                <strong style={{ color: 'var(--text-dim)', fontWeight: 500 }}>{value}</strong>
              </div>
            ))}
          </div>
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-muted)', letterSpacing: '0.1em' }}>
            ALGOSPHERE · v2.1.0 · BUILD 20260328
          </span>
        </footer>
      </div>
    </>
  )
}
