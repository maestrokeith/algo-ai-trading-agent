interface StepIndicatorProps {
  steps: string[]
  current: number
}

export function StepIndicator({ steps, current }: StepIndicatorProps) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 0, marginBottom: 36 }}>
      {steps.map((label, i) => {
        const done    = i < current
        const active  = i === current
        const color   = done || active ? 'var(--amber)' : 'var(--text-muted)'
        return (
          <div key={label} style={{ display: 'flex', alignItems: 'center', flex: i < steps.length - 1 ? 1 : 0 }}>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6, flexShrink: 0 }}>
              <div style={{
                width: 24, height: 24,
                border: `1px solid ${color}`,
                background: active ? 'var(--amber-mid)' : done ? 'var(--amber)' : 'transparent',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontFamily: 'var(--font-mono)', fontSize: 10,
                color: done ? 'var(--bg)' : color,
                transition: 'all 0.3s',
              }}>
                {done ? '✓' : i + 1}
              </div>
              <span style={{
                fontFamily: 'var(--font-mono)', fontSize: 9,
                letterSpacing: '0.14em', textTransform: 'uppercase',
                color, whiteSpace: 'nowrap',
              }}>
                {label}
              </span>
            </div>
            {i < steps.length - 1 && (
              <div style={{
                flex: 1, height: 1, marginBottom: 20,
                background: done ? 'var(--amber)' : 'var(--border)',
                transition: 'background 0.3s',
              }} />
            )}
          </div>
        )
      })}
    </div>
  )
}
