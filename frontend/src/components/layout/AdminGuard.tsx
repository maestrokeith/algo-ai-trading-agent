import { Navigate, Outlet } from 'react-router-dom'
import { useAuthStore } from '@/store/authStore'

/** Redirects non-admin users to /dashboard. */
export function AdminGuard() {
  const role = useAuthStore((s) => s.role)
  if (role !== 'admin') return <Navigate to="/dashboard" replace />
  return <Outlet />
}
