import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClient } from '@/lib/queryClient'
import { ThemeProvider } from '@/components/layout/ThemeProvider'
import { AppShell }   from '@/components/layout/AppShell'
import { AuthGuard }  from '@/components/layout/AuthGuard'
import { Dashboard }  from '@/pages/Dashboard'
import { Trades }     from '@/pages/Trades'
import { Settings }   from '@/pages/Settings'
import { Login }      from '@/pages/Login'
import { Signup }     from '@/pages/Signup'
import { Onboarding } from '@/pages/Onboarding'

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
      <BrowserRouter>
        <Routes>
          {/* Public auth routes */}
          <Route path="/login"      element={<Login />} />
          <Route path="/signup"     element={<Signup />} />
          <Route path="/onboarding" element={<Onboarding />} />

          {/* Protected app routes */}
          <Route element={<AuthGuard />}>
            <Route element={<AppShell />}>
              <Route index             element={<Navigate to="/dashboard" replace />} />
              <Route path="/dashboard" element={<Dashboard />} />
              <Route path="/trades"    element={<Trades />} />
              <Route path="/settings"  element={<Settings />} />
            </Route>
          </Route>
        </Routes>
      </BrowserRouter>
      </ThemeProvider>
    </QueryClientProvider>
  )
}
