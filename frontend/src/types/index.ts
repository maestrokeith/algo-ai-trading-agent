export interface Position {
  symbol: string
  side: 'long' | 'short'
  shares: number
  entryPrice: number
  currentPrice: number
  unrealizedPnl: number
  returnPct: number
  barsHeld: number
  atrPct: number
}

export interface Trade {
  time: string
  symbol: string
  side: 'long' | 'short'
  shares: number
  entryPrice: number
  exitPrice: number
  pnl: number
  returnPct: number
  exitReason: 'profit-target' | 'stop-loss' | 'trail-stop' | 'time-exit'
  barsHeld: number
}

export interface GateLogEntry {
  id: string
  time: string
  symbol: string
  message: string
}

export interface PortfolioStats {
  equity: number
  dayPnl: number
  dayPnlPct: number
  unrealized: number
  totalReturn: number
  winRate: number
  winCount: number
  totalTrades: number
}

export interface RegimeScores {
  label: 'bullish' | 'neutral' | 'bearish'
  spy: number
  qqq: number
  vix: number
}
