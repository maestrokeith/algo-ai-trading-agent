import type { CSSProperties, ReactNode } from 'react'

interface PanelProps {
  title: string
  tag?: string
  accented?: boolean
  children: ReactNode
  style?: CSSProperties
}

export function Panel({ title, tag, accented, children, style }: PanelProps) {
  return (
    <div style={{
      background: 'var(--bg-panel)',
      border: '1px solid var(--border)',
      position: 'relative',
      overflow: 'hidden',
      animation: 'panel-enter 0.5s ease both',
      ...style,
    }}>
      {/* Amber top-edge accent */}
      {accented && (
        <div aria-hidden style={{
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          height: 1,
          background: 'linear-gradient(90deg, var(--amber) 0%, transparent 60%)',
          zIndex: 1,
        }} />
      )}

      {/* Subtle line grain */}
      <div aria-hidden style={{
        position: 'absolute',
        inset: 0,
        background: 'repeating-linear-gradient(0deg, rgba(255,255,255,0.012) 0px, rgba(255,255,255,0.012) 1px, transparent 1px, transparent 4px)',
        pointerEvents: 'none',
      }} />

      {/* Header */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '9px 16px 8px',
        borderBottom: '1px solid var(--border)',
        position: 'relative',
        zIndex: 1,
      }}>
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, fontWeight: 500, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--text-muted)' }}>
          {title}
        </span>
        {tag && (
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.12em', textTransform: 'uppercase', color: 'var(--amber)', opacity: 0.6 }}>
            {tag}
          </span>
        )}
      </div>

      {/* Content */}
      <div style={{ position: 'relative', zIndex: 1 }}>
        {children}
      </div>
    </div>
  )
}
