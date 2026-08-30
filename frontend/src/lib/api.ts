/**
 * Typed axios client for the AlgoSphere FastAPI backend.
 * Base URL is read from VITE_API_URL (defaults to http://localhost:8000).
 * JWT token is injected automatically from authStore on every request.
 */
import axios from 'axios'
import { useAuthStore } from '@/store/authStore'

export const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_URL ?? 'http://localhost:8000',
})

apiClient.interceptors.request.use((config) => {
  const token = useAuthStore.getState().token
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

apiClient.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      useAuthStore.getState().logout()
    }
    return Promise.reject(err)
  },
)

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export interface TokenResponse {
  access_token: string
  token_type: string
}

export interface UserProfile {
  id: string
  email: string
  role: 'admin' | 'trader'
  paper: boolean
}

export async function login(email: string, password: string): Promise<TokenResponse> {
  const res = await apiClient.post<TokenResponse>('/auth/login', { email, password })
  return res.data
}

export async function getMe(): Promise<UserProfile> {
  const res = await apiClient.get<UserProfile>('/auth/me')
  return res.data
}

// ---------------------------------------------------------------------------
// Per-user endpoints
// ---------------------------------------------------------------------------

export interface SnapshotOut {
  equity: number | null
  cash: number | null
  buying_power: number | null
  daily_pnl: number | null
  daily_pnl_pct: number | null
  captured_at: string
}

export interface PortfolioOut {
  latest: SnapshotOut | null
  history: SnapshotOut[]
}

export interface PositionOut {
  symbol: string
  side: string
  qty: number
  avg_entry_price: number | null
  current_price: number | null
  unrealized_pnl: number | null
  stop_pct: number | null
  partial_taken: boolean
}

export interface TradeOut {
  id: number
  symbol: string
  side: string
  qty: number
  entry_price: number | null
  exit_price: number | null
  pnl: number | null
  pnl_pct: number | null
  exit_reason: string | null
  entered_at: string | null
  exited_at: string | null
}

export interface GateLogOut {
  id: number
  gate: string
  symbol: string | null
  passed: boolean
  reason: string | null
  logged_at: string
}

export interface RegimeOut {
  label: string
  spy_score: number | null
  qqq_score: number | null
  vix: number | null
  logged_at: string
}

export interface UserSummary {
  id: string
  email: string
  role: string
  paper: boolean
  equity: number | null
}

export const api = {
  portfolio: (userId: string, historyLimit = 100) =>
    apiClient.get<PortfolioOut>(`/api/users/${userId}/portfolio?history_limit=${historyLimit}`).then(r => r.data),

  positions: (userId: string) =>
    apiClient.get<PositionOut[]>(`/api/users/${userId}/positions`).then(r => r.data),

  trades: (userId: string, limit = 50) =>
    apiClient.get<TradeOut[]>(`/api/users/${userId}/trades?limit=${limit}`).then(r => r.data),

  gateLog: (userId: string, limit = 50) =>
    apiClient.get<GateLogOut[]>(`/api/users/${userId}/gate-log?limit=${limit}`).then(r => r.data),

  regime: (userId: string) =>
    apiClient.get<RegimeOut | null>(`/api/users/${userId}/regime`).then(r => r.data),

  adminUsers: () =>
    apiClient.get<UserSummary[]>('/api/admin/users').then(r => r.data),
}
