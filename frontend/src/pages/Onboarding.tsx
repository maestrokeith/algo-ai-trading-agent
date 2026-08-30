import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { StepIndicator } from '@/components/ui/StepIndicator'

const STEPS = ['CONNECT BROKER', 'RISK PROFILE', 'CONFIRM']

type RiskProfile = 'conservative' | 'balanced' | 'aggressive'

const PROFILES: { id: RiskProfile; label: string; desc: string; color: string }[] = [
  { id: 'conservative', label: 'CONSERVATIVE', desc: 'Lower position sizes, tighter stops, fewer trades. Capital preservation first.', color: 'var(--blue)'  },
  { id: 'balanced',     label: 'BALANCED',     desc: 'Default trend-following profile. Standard 12-gate risk pipeline.',            color: 'var(--amber)' },
  { id: 'aggressive',   label: 'AGGRESSIVE',   desc: 'Larger positions, wider ATR stops, higher trade frequency.',                  color: 'var(--red)'   },
]

export function Onboarding() {
  const navigate = useNavigate()
  const [step, setStep]         = useState(0)
  const [apiKey, setApiKey]     = useState('')
  const [apiSecret, setApiSecret] = useState('')
  const [isPaper, setIsPaper]   = useState(true)
  const [profile, setProfile]   = useState<RiskProfile>('balanced')
  const [error, setError]       = useState('')

  function nextStep() {
    if (step === 0 && (!apiKey || !apiSecret)) { setError('API key and secret are required.'); return }
    setError('')
    setStep((s) => s + 1)
  }

  function finish() {
    navigate('/dashboard')
  }

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', position: 'relative', zIndex: 1, padding: '24px 20px' }}>
      <div style={{ width: 520, background: 'var(--bg-panel)', border: '1px solid var(--border)', position: 'relative', overflow: 'hidden', animation: 'panel-enter 0.5s ease' }}>
        <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: 1, background: 'linear-gradient(90deg, var(--amber) 0%, transparent 60%)' }} />

        <div style={{ padding: '32px 32px 28px' }}>
          {/* Title */}
          <div style={{ marginBottom: 28, textAlign: 'center' }}>
            <div style={{ fontFamily: 'var(--font-display)', fontSize: 28, letterSpacing: '0.1em', color: 'var(--amber)' }}>
              ALGO<span style={{ color: 'var(--text-dim)' }}>SPHERE</span>
            </div>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--text-muted)', marginTop: 5 }}>
              ACCOUNT SETUP
            </div>
          </div>

          <StepIndicator steps={STEPS} current={step} />

          {/* Step 0: Connect Broker */}
          {step === 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              <SectionLabel>ALPACA CREDENTIALS</SectionLabel>
              <Field label="API KEY ID"    type="text"     value={apiKey}    onChange={setApiKey}    placeholder="APCA_API_KEY_ID"    autoComplete="off" />
              <Field label="API SECRET"    type="password" value={apiSecret} onChange={setApiSecret} placeholder="APCA_API_SECRET_KEY" autoComplete="off" />

              <div style={{ display: 'flex', border: '1px solid var(--border)' }}>
                {[{ v: true, label: 'PAPER' }, { v: false, label: 'LIVE' }].map(({ v, label }, i) => (
                  <button key={label} onClick={() => setIsPaper(v)} style={{
                    flex: 1, padding: '8px 0',
                    fontFamily: 'var(--font-mono)', fontSize: 10, letterSpacing: '0.14em',
                    textTransform: 'uppercase', cursor: 'pointer', border: 'none',
                    borderRight: i === 0 ? '1px solid var(--border)' : 'none',
                    background: isPaper === v ? 'var(--amber-mid)' : 'transparent',
                    color: isPaper === v ? 'var(--amber)' : 'var(--text-muted)',
                    transition: 'all 0.15s',
                  }}>
                    {label}
                  </button>
                ))}
              </div>

              <div style={{ fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-muted)', letterSpacing: '0.06em', lineHeight: 1.6, padding: '8px 10px', background: 'var(--bg-panel-alt)', border: '1px solid var(--border)' }}>
                Your keys are stored as environment variables and never logged or transmitted. Start with paper trading to verify setup.
              </div>

              {error && <ErrorMsg>{error}</ErrorMsg>}
              <ActionBtn onClick={nextStep}>CONNECT BROKER →</ActionBtn>
            </div>
          )}

          {/* Step 1: Risk Profile */}
          {step === 1 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <SectionLabel>SELECT RISK PROFILE</SectionLabel>
              {PROFILES.map((p) => (
                <button key={p.id} onClick={() => setProfile(p.id)} style={{
                  padding: '14px 16px', background: profile === p.id ? `${p.color}14` : 'var(--bg-panel-alt)',
                  border: `1px solid ${profile === p.id ? p.color + '4d' : 'var(--border)'}`,
                  cursor: 'pointer', textAlign: 'left', transition: 'all 0.15s',
                  boxShadow: profile === p.id ? `0 0 12px ${p.color}18` : 'none',
                }}>
                  <div style={{ fontFamily: 'var(--font-display)', fontSize: 18, letterSpacing: '0.1em', color: p.color, marginBottom: 4 }}>
                    {p.label}
                  </div>
                  <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-dim)', lineHeight: 1.5 }}>
                    {p.desc}
                  </div>
                </button>
              ))}
              <ActionBtn onClick={nextStep} style={{ marginTop: 4 }}>CONFIRM PROFILE →</ActionBtn>
            </div>
          )}

          {/* Step 2: Confirm */}
          {step === 2 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              <SectionLabel>READY TO LAUNCH</SectionLabel>

              {[
                ['MODE',    isPaper ? 'PAPER TRADING' : 'LIVE TRADING'],
                ['PROFILE', profile.toUpperCase()],
                ['API KEY', apiKey.slice(0, 8) + '···'],
              ].map(([label, value]) => (
                <div key={label} style={{ display: 'flex', justifyContent: 'space-between', padding: '10px 14px', background: 'var(--bg-panel-alt)', border: '1px solid var(--border)' }}>
                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.16em', textTransform: 'uppercase', color: 'var(--text-muted)' }}>{label}</span>
                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--amber)', fontWeight: 500 }}>{value}</span>
                </div>
              ))}

              <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-dim)', lineHeight: 1.6, padding: '10px 12px', border: '1px solid rgba(255,179,0,0.2)', background: 'var(--amber-dim)' }}>
                The bot will start on the next market open. You can pause or stop it at any time from the dashboard.
              </div>

              <ActionBtn onClick={finish} style={{ background: 'var(--green-dim)', border: '1px solid rgba(0,212,139,0.4)', color: 'var(--green)' }}>
                START PAPER TRADING ▶
              </ActionBtn>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: 2 }}>
      {children}
    </div>
  )
}

function ErrorMsg({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--red)', letterSpacing: '0.08em', padding: '6px 10px', border: '1px solid rgba(255,59,92,0.25)', background: 'var(--red-dim)' }}>
      {children}
    </div>
  )
}

function ActionBtn({ children, onClick, style }: { children: React.ReactNode; onClick: () => void; style?: React.CSSProperties }) {
  return (
    <button onClick={onClick} style={{
      fontFamily: 'var(--font-display)', fontSize: 18, letterSpacing: '0.14em',
      padding: '11px', background: 'var(--amber-mid)', border: '1px solid rgba(255,179,0,0.4)',
      color: 'var(--amber)', cursor: 'pointer', transition: 'all 0.15s', width: '100%',
      ...style,
    }}>
      {children}
    </button>
  )
}

function Field({ label, type, value, onChange, placeholder, autoComplete }: {
  label: string; type: string; value: string; placeholder: string; autoComplete: string
  onChange: (v: string) => void
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      <label style={{ fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.18em', textTransform: 'uppercase', color: 'var(--text-muted)' }}>{label}</label>
      <input type={type} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} autoComplete={autoComplete} style={{
        background: 'var(--bg-panel-alt)', border: '1px solid var(--border)',
        color: 'var(--text-primary)', fontFamily: 'var(--font-mono)', fontSize: 13,
        padding: '9px 12px', outline: 'none', width: '100%', letterSpacing: '0.04em',
      }} />
    </div>
  )
}
