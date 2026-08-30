/**
 * sessionStore — tracks which user's data is currently being viewed.
 *
 * - Traders: viewingUserId is always their own userId (non-switchable).
 * - Admins: can switch viewingUserId to any user via the UserSwitcher.
 */
import { create } from 'zustand'
import { useAuthStore } from './authStore'

interface SessionState {
  viewingUserId: string | null
  setViewingUserId: (id: string) => void
  resetToSelf: () => void
}

export const useSessionStore = create<SessionState>()((set) => ({
  viewingUserId: null,

  setViewingUserId: (id) => set({ viewingUserId: id }),

  resetToSelf: () =>
    set({ viewingUserId: useAuthStore.getState().userId }),
}))

/** Returns the effective user ID to fetch data for. Falls back to auth user. */
export function useViewingUserId(): string | null {
  const viewingUserId = useSessionStore((s) => s.viewingUserId)
  const authUserId = useAuthStore((s) => s.userId)
  return viewingUserId ?? authUserId
}
