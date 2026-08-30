import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { useViewingUserId } from '@/store/sessionStore'
import type { RegimeScores } from '@/types'

export function useRegime(): { regime: RegimeScores | null; isLoading: boolean } {
  const userId = useViewingUserId()

  const { data, isLoading } = useQuery({
    queryKey: ['regime', userId],
    queryFn: () => api.regime(userId!),
    enabled: !!userId,
    staleTime: 30_000,
    refetchInterval: 30_000,
  })

  const regime: RegimeScores | null = data
    ? {
        label: data.label as RegimeScores['label'],
        spy: data.spy_score ?? 0,
        qqq: data.qqq_score ?? 0,
        vix: data.vix ?? 0,
      }
    : null

  return { regime, isLoading }
}
