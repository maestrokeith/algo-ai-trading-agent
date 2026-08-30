import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export type UserRole = 'admin' | 'trader'

interface AuthState {
  token: string | null
  userId: string | null
  email: string | null
  role: UserRole | null
  paper: boolean

  setAuth: (token: string, userId: string, email: string, role: UserRole, paper: boolean) => void
  logout: () => void
  isAuthenticated: () => boolean
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      token: null,
      userId: null,
      email: null,
      role: null,
      paper: true,

      setAuth: (token, userId, email, role, paper) =>
        set({ token, userId, email, role, paper }),

      logout: () =>
        set({ token: null, userId: null, email: null, role: null, paper: true }),

      isAuthenticated: () => get().token !== null,
    }),
    { name: 'algosphere-auth' },
  ),
)
