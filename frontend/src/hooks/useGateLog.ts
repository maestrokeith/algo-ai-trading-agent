import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { useViewingUserId } from '@/store/sessionStore'
import type { GateLogEntry } from '@/types'

export function useGateLog(limit = 12): { entries: GateLogEntry[]; isLoading: boolean } {
  const userId = useViewingUserId()

  const { data, isLoading } = useQuery({
    queryKey: ['gateLog', userId, limit],
    queryFn: () => api.gateLog(userId!, limit),
    enabled: !!userId,
    staleTime: 5_000,
    refetchInterval: 8_000,
  })

  const entries: GateLogEntry[] = (data ?? []).map((g) => {
    const loggedAt = new Date(g.logged_at)
    const time = loggedAt.toLocaleTimeString('en-US', {
      timeZone: 'America/New_York',
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
    })
    return {
      id: String(g.id),
      time,
      symbol: g.symbol ?? g.gate,
      message: g.reason ?? (g.passed ? `${g.gate}: passed` : `${g.gate}: blocked`),
    }
  })

  return { entries, isLoading }
}
