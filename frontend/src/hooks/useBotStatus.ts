import { useBotStore } from '@/store/botStore'

export function useBotStatus() {
  const store = useBotStore()

  // Countdown is ticked globally in AppShell; expose readable state here
  const mins = Math.floor(store.nextLoopSecs / 60)
  const secs = store.nextLoopSecs % 60
  const countdownLabel = `${mins}:${String(secs).padStart(2, '0')}`

  return { ...store, countdownLabel }
}
