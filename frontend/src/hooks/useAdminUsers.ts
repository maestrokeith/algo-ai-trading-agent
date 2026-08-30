import { useQuery } from '@tanstack/react-query'
import { api, type UserSummary } from '@/lib/api'
import { useAuthStore } from '@/store/authStore'

export function useAdminUsers(): { users: UserSummary[]; isLoading: boolean } {
  const role = useAuthStore((s) => s.role)

  const { data, isLoading } = useQuery({
    queryKey: ['adminUsers'],
    queryFn: api.adminUsers,
    enabled: role === 'admin',
    staleTime: 30_000,
  })

  return { users: data ?? [], isLoading }
}
