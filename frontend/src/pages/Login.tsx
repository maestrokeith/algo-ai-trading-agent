import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { login, getMe } from '@/lib/api'
import { useAuthStore } from '@/store/authStore'
import { useSessionStore } from '@/store/sessionStore'

export function Login() {
  const navigate = useNavigate()
  const setAuth = useAuthStore((s) => s.setAuth)
  const setViewingUserId = useSessionStore((s) => s.setViewingUserId)

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!email || !password) {
      setError('Email and password are required.')
      return
    }
    setLoading(true)
    setError('')
    try {
      const { access_token } = await login(email, password)
      // Temporarily store token so getMe() can use the interceptor
      setAuth(access_token, '', email, 'trader', true)
      const profile = await getMe()
      setAuth(access_token, profile.id, profile.email, profile.role, profile.paper)
      setViewingUserId(profile.id)
      navigate('/dashboard')
    } catch {
      setError('Invalid email or password.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{
      minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center',
      position: 'relative', zIndex: 1,
    }}>
      <div style={{
        width: 400, background: 'var(--bg-panel)',
        border: '1px solid var(--border)', position: 'relative', overflow: 'hidden',
        animation: 'panel-enter 0.5s ease',
      }}>
        <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: 1, background: 'linear-gradient(90deg, var(--amber) 0%, transparent 60%)' }} />

        <div style={{ padding: '32px 32px 28px' }}>
          <div style={{ marginBottom: 28, textAlign: 'center' }}>
            <div style={{ fontFamily: 'var(--font-display)', fontSize: 32, letterSpacing: '0.1em', color: 'var(--amber)', textShadow: '0 0 24px rgba(255,179,0,0.3)' }}>
              ALGO<span style={{ color: 'var(--text-dim)' }}>SPHERE</span>
            </div>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--text-muted)', marginTop: 6 }}>
              OPERATIONS TERMINAL · SIGN IN
            </div>
          </div>

          <form onSubmit={handleSubmit} noValidate style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <Field label="EMAIL" type="email" value={email} onChange={setEmail} placeholder="you@example.com" />
            <Field label="PASSWORD" type="password" value={password} onChange={setPassword} placeholder="············" />

            {error && (
              <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--red)', letterSpacing: '0.08em', padding: '6px 10px', border: '1px solid rgba(255,59,92,0.25)', background: 'var(--red-dim)' }}>
                {error}
              </div>
            )}

            <button type="submit" disabled={loading} style={{ ...submitBtn, opacity: loading ? 0.6 : 1 }}>
              {loading ? 'AUTHENTICATING...' : 'SIGN IN'}
            </button>
          </form>

          <div style={{ marginTop: 20, textAlign: 'center', fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-muted)', letterSpacing: '0.08em' }}>
            No account?{' '}
            <Link to="/signup" style={{ color: 'var(--amber)', textDecoration: 'none' }}>CREATE ONE</Link>
          </div>
        </div>
      </div>
    </div>
  )
}

function Field({ label, type, value, onChange, placeholder }: {
  label: string; type: string; value: string
  onChange: (v: string) => void; placeholder: string
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      <label style={{ fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.18em', textTransform: 'uppercase', color: 'var(--text-muted)' }}>
        {label}
      </label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoComplete={type === 'email' ? 'email' : 'current-password'}
        style={inputStyle}
      />
    </div>
  )
}

const inputStyle: React.CSSProperties = {
  background: 'var(--bg-panel-alt)',
  border: '1px solid var(--border)',
  color: 'var(--text-primary)',
  fontFamily: 'var(--font-mono)',
  fontSize: 13,
  padding: '9px 12px',
  outline: 'none',
  width: '100%',
  letterSpacing: '0.04em',
}

const submitBtn: React.CSSProperties = {
  fontFamily: 'var(--font-display)',
  fontSize: 18,
  letterSpacing: '0.14em',
  padding: '11px',
  background: 'var(--amber-mid)',
  border: '1px solid rgba(255,179,0,0.4)',
  color: 'var(--amber)',
  cursor: 'pointer',
  marginTop: 6,
  transition: 'all 0.15s',
  width: '100%',
}
