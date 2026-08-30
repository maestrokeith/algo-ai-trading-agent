import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { useViewingUserId } from '@/store/sessionStore'
import type { Position } from '@/types'

export function usePositions(): { positions: Position[]; isLoading: boolean } {
  const userId = useViewingUserId()

  const { data, isLoading } = useQuery({
    queryKey: ['positions', userId],
    queryFn: () => api.positions(userId!),
    enabled: !!userId,
    staleTime: 10_000,
    refetchInterval: 15_000,
  })

  const positions: Position[] = (data ?? []).map((p) => ({
    symbol: p.symbol,
    side: p.side as 'long' | 'short',
    shares: p.qty,
    entryPrice: p.avg_entry_price ?? 0,
    currentPrice: p.current_price ?? 0,
    unrealizedPnl: p.unrealized_pnl ?? 0,
    returnPct: p.avg_entry_price
      ? ((( p.current_price ?? 0) - p.avg_entry_price) / p.avg_entry_price) * 100
      : 0,
    barsHeld: 0,   // not tracked in DB yet
    atrPct: 0,     // not tracked in DB yet
  }))

  return { positions, isLoading }
}
