import { useEffect } from 'react'
import { useThemeStore, resolveTheme } from '@/store/themeStore'

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const mode = useThemeStore((s) => s.mode)

  useEffect(() => {
    function applyTheme() {
      const resolved = resolveTheme(mode)
      document.documentElement.classList.toggle('light', resolved === 'light')
      document.documentElement.classList.toggle('dark',  resolved === 'dark')
    }

    applyTheme()

    // Re-apply if OS preference changes while mode is 'system'
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    mq.addEventListener('change', applyTheme)
    return () => mq.removeEventListener('change', applyTheme)
  }, [mode])

  return <>{children}</>
}
