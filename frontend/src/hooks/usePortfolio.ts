import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { useViewingUserId } from '@/store/sessionStore'
import type { PortfolioStats, RegimeScores } from '@/types'

const FALLBACK_REGIME: RegimeScores = { label: 'neutral', spy: 0, qqq: 0, vix: 0 }

export function usePortfolio() {
  const userId = useViewingUserId()

  const { data: portfolio, isLoading: portfolioLoading } = useQuery({
    queryKey: ['portfolio', userId],
    queryFn: () => api.portfolio(userId!),
    enabled: !!userId,
    staleTime: 10_000,
    refetchInterval: 15_000,
  })

  const { data: regime } = useQuery({
    queryKey: ['regime', userId],
    queryFn: () => api.regime(userId!),
    enabled: !!userId,
    staleTime: 30_000,
    refetchInterval: 30_000,
  })

  const { data: trades } = useQuery({
    queryKey: ['trades', userId],
    queryFn: () => api.trades(userId!, 200),
    enabled: !!userId,
    staleTime: 30_000,
  })

  const snap = portfolio?.latest
  const equity = snap?.equity ?? 0
  const dayPnl = snap?.daily_pnl ?? 0
  const dayPnlPct = snap?.daily_pnl_pct ?? 0

  const winTrades = trades?.filter((t) => (t.pnl ?? 0) > 0) ?? []
  const totalTrades = trades?.length ?? 0
  const winRate = totalTrades > 0 ? (winTrades.length / totalTrades) * 100 : 0

  const stats: PortfolioStats = {
    equity,
    dayPnl,
    dayPnlPct,
    unrealized: 0,
    totalReturn: 0,
    winRate,
    winCount: winTrades.length,
    totalTrades,
  }

  const regimeOut: RegimeScores = regime
    ? {
        label: regime.label as RegimeScores['label'],
        spy: regime.spy_score ?? 0,
        qqq: regime.qqq_score ?? 0,
        vix: regime.vix ?? 0,
      }
    : FALLBACK_REGIME

  const equityHistory = (portfolio?.history ?? []).map((s) => s.equity ?? 0)

  return { stats, regime: regimeOut, equityHistory, isLoading: portfolioLoading }
}
