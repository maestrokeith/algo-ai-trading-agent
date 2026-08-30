import { create } from 'zustand'

export type BotStatus = 'active' | 'paused' | 'stopped'
export type TradingMode = 'paper' | 'live'

interface BotState {
  status: BotStatus
  mode: TradingMode
  loopCount: number
  nextLoopSecs: number
  setStatus: (status: BotStatus) => void
  setMode: (mode: TradingMode) => void
  tickCountdown: () => void
}

export const useBotStore = create<BotState>((set) => ({
  status: 'active',
  mode: 'paper',
  loopCount: 247,
  nextLoopSecs: 42,
  setStatus: (status) => set({ status }),
  setMode: (mode) => set({ mode }),
  tickCountdown: () =>
    set((state) => {
      if (state.nextLoopSecs <= 0) {
        return { nextLoopSecs: 600, loopCount: state.loopCount + 1 }
      }
      return { nextLoopSecs: state.nextLoopSecs - 1 }
    }),
}))
