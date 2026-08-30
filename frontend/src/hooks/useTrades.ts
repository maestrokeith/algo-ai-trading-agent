import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { useViewingUserId } from '@/store/sessionStore'
import type { Trade } from '@/types'

const EXIT_REASON_MAP: Record<string, Trade['exitReason']> = {
  'profit-target': 'profit-target',
  'target':        'profit-target',
  'trail-stop':    'trail-stop',
  'trail_stop':    'trail-stop',
  'stop-loss':     'stop-loss',
  'stop_loss':     'stop-loss',
  'time-exit':     'time-exit',
  'time_exit':     'time-exit',
}

export function useTrades(): { trades: Trade[]; isLoading: boolean } {
  const userId = useViewingUserId()

  const { data, isLoading } = useQuery({
    queryKey: ['trades', userId],
    queryFn: () => api.trades(userId!, 50),
    enabled: !!userId,
    staleTime: 10_000,
    refetchInterval: 15_000,
  })

  const trades: Trade[] = (data ?? []).map((t) => {
    const exitedAt = t.exited_at ? new Date(t.exited_at) : null
    const enteredAt = t.entered_at ? new Date(t.entered_at) : null
    const barsHeld = exitedAt && enteredAt
      ? Math.max(0, Math.round((exitedAt.getTime() - enteredAt.getTime()) / 86_400_000))
      : 0
    const timeLabel = exitedAt
      ? exitedAt.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
      : '--:--:--'

    return {
      time: timeLabel,
      symbol: t.symbol,
      side: t.side as 'long' | 'short',
      shares: t.qty,
      entryPrice: t.entry_price ?? 0,
      exitPrice: t.exit_price ?? 0,
      pnl: t.pnl ?? 0,
      returnPct: t.pnl_pct ?? 0,
      exitReason: EXIT_REASON_MAP[t.exit_reason ?? ''] ?? 'time-exit',
      barsHeld,
    }
  })

  return { trades, isLoading }
}
