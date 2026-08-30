import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { useSessionStore } from '@/store/sessionStore'
import { useViewingUserId } from '@/store/sessionStore'

export function UserSwitcher() {
  const { data: users } = useQuery({
    queryKey: ['adminUsers'],
    queryFn: api.adminUsers,
    staleTime: 30_000,
  })

  const setViewingUserId = useSessionStore((s) => s.setViewingUserId)
  const viewingUserId = useViewingUserId()

  if (!users || users.length === 0) return null

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
      <span style={{
        fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.14em',
        textTransform: 'uppercase', color: 'var(--text-muted)',
      }}>
        VIEWING
      </span>
      <select
        value={viewingUserId ?? ''}
        onChange={(e) => setViewingUserId(e.target.value)}
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          letterSpacing: '0.08em',
          background: 'var(--bg-panel-alt)',
          border: '1px solid var(--amber-dim)',
          color: 'var(--amber)',
          padding: '3px 8px',
          cursor: 'pointer',
          outline: 'none',
        }}
      >
        {users.map((u) => (
          <option key={u.id} value={u.id}>
            {u.id}{u.paper ? ' (paper)' : ' (live)'}
          </option>
        ))}
      </select>
    </div>
  )
}
