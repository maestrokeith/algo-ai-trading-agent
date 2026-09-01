import { Panel } from '@/components/layout/Panel'

const chain = [
  ['Market Agent', 'NVDA quote validated, spread 0.04%'],
  ['Regime Agent', 'TREND_UP · 78%'],
  ['Strategy Agent', 'VWAP_BREAKOUT · BUY · 82%'],
  ['Critic', 'PASS · late-entry warning'],
  ['Risk Agent', 'PASS'],
  ['Policy Engine', 'PASS'],
  ['Execution', 'DRY RUN / PAPER READY'],
]

const memory = [
  ['VWAP_BREAKOUT', 'TREND_UP', '18', '66%', '+0.42%'],
  ['VWAP_BREAKOUT', 'CHOP', '15', '27%', '-0.31%'],
]

const scenarios = [
  {
    name: 'Scenario A',
    symbol: 'NVDA',
    regime: 'TREND_UP',
    regimeConfidence: '78%',
    strategy: 'VWAP_BREAKOUT',
    strategyConfidence: '82%',
    critic: 'PASS',
    concerns: 'spread controlled, volume confirmed',
    risk: 'PASS',
    policy: 'PASS',
    execution: 'ALPACA PAPER',
    lesson: 'Trend-up VWAP breakouts remain eligible when spread and volume gates pass.',
  },
  {
    name: 'Scenario B',
    symbol: 'TSLA',
    regime: 'CHOP',
    regimeConfidence: '61%',
    strategy: 'ENTER',
    strategyConfidence: '73%',
    critic: 'REJECT',
    concerns: 'entry_too_extended',
    risk: 'NOT RUN',
    policy: 'BLOCKED',
    execution: 'BLOCKED',
    lesson: 'Extended entries are rejected before execution preparation.',
  },
  {
    name: 'Scenario C',
    symbol: 'AAPL',
    regime: 'TREND_UP',
    regimeConfidence: '80%',
    strategy: 'ENTER',
    strategyConfidence: '96%',
    critic: 'PASS',
    concerns: 'none blocking',
    risk: 'SAFE',
    policy: 'BLOCKED · daily_loss_limit',
    execution: 'BLOCKED',
    lesson: 'AI WANTED THE TRADE. SAFETY BLOCKED IT.',
  },
]

export function AgentDashboard() {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1.2fr 1fr', gap: 16, paddingTop: 16 }}>
      <Panel title="Safety" tag="MODE: PAPER" accented>
        <div style={{ padding: 16, display: 'grid', gap: 8 }}>
          <div style={{ fontFamily: 'var(--font-display)', fontSize: 30, color: 'var(--amber)', letterSpacing: '0.04em' }}>ALGO</div>
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-dim)', letterSpacing: '0.12em' }}>
            SELF-IMPROVING AUTONOMOUS TRADING AGENT
          </div>
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--green)', letterSpacing: '0.12em' }}>
            LIVE ORDERING BLOCKED FROM AGENT DEMO PATH
          </div>
        </div>
      </Panel>

      <Panel title="Market" tag="REGIMES">
        <div style={{ padding: 16, display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 10 }}>
          {[
            ['NVDA', '$500.10', 'TREND_UP'],
            ['TSLA', '$250.20', 'CHOP'],
            ['AAPL', '$190.40', 'TREND_UP'],
          ].map(([symbol, price, regime]) => (
            <div key={symbol} style={{ border: '1px solid var(--border)', padding: 10 }}>
              <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-muted)' }}>{symbol}</div>
              <div style={{ fontFamily: 'var(--font-mono)', fontSize: 18, color: 'var(--text-dim)' }}>{price}</div>
              <div style={{ fontFamily: 'var(--font-mono)', fontSize: 9, color: regime === 'CHOP' ? 'var(--amber)' : 'var(--green)' }}>{regime}</div>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="Agent Decisions" tag="TRACE" accented>
        <div style={{ padding: 16, display: 'grid', gap: 8 }}>
          {chain.map(([agent, decision], idx) => (
            <div key={agent} style={{ display: 'grid', gridTemplateColumns: '150px 1fr', gap: 12, alignItems: 'center' }}>
              <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-muted)' }}>{String(idx + 1).padStart(2, '0')} · {agent}</span>
              <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: decision.includes('PASS') ? 'var(--green)' : 'var(--text-dim)' }}>{decision}</span>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="Judge Scenarios" tag="REPLAY / DEMO" accented>
        <div style={{ padding: 16, display: 'grid', gap: 10 }}>
          {scenarios.map((scenario) => (
            <div key={scenario.name} style={{ border: '1px solid var(--border)', padding: 12, display: 'grid', gap: 8 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'baseline' }}>
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-muted)' }}>{scenario.name}</span>
                <span style={{ fontFamily: 'var(--font-display)', fontSize: 22, color: 'var(--amber)', letterSpacing: '0.04em' }}>{scenario.symbol}</span>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 6 }}>
                <Metric label="Regime" value={`${scenario.regime} · ${scenario.regimeConfidence}`} />
                <Metric label="Strategy" value={`${scenario.strategy} · ${scenario.strategyConfidence}`} />
                <Metric label="Critic" value={scenario.critic} danger={scenario.critic === 'REJECT'} />
                <Metric label="Concerns" value={scenario.concerns} />
                <Metric label="AI Risk" value={scenario.risk} />
                <Metric label="Policy" value={scenario.policy} danger={scenario.policy.includes('BLOCKED')} />
                <Metric label="Execution" value={scenario.execution} danger={scenario.execution === 'BLOCKED'} />
                <Metric label="Post-Trade / Memory" value={scenario.lesson} danger={scenario.lesson.startsWith('AI WANTED')} />
              </div>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="Trade Timeline" tag="DEMO">
        <div style={{ padding: 16, display: 'grid', gap: 7 }}>
          {[
            '10:04:01 Market data updated',
            '10:04:02 Regime -> TREND_UP',
            '10:04:02 Strategy -> VWAP_BREAKOUT',
            '10:04:03 Critic -> PASS',
            '10:04:03 Risk -> PASS',
            '10:04:03 Policy -> PASS',
            '10:04:04 Alpaca paper order ready',
          ].map((line) => (
            <div key={line} style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-dim)' }}>{line}</div>
          ))}
        </div>
      </Panel>

      <Panel title="Memory" tag="LEARNING">
        <div style={{ padding: 16 }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontFamily: 'var(--font-mono)', fontSize: 10 }}>
            <tbody>
              {memory.map(([strategy, regime, trades, winRate, avgReturn]) => (
                <tr key={`${strategy}-${regime}`} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td style={{ padding: '8px 4px', color: 'var(--text-dim)' }}>{strategy}</td>
                  <td style={{ padding: '8px 4px', color: 'var(--text-muted)' }}>{regime}</td>
                  <td style={{ padding: '8px 4px', color: 'var(--text-muted)' }}>{trades}</td>
                  <td style={{ padding: '8px 4px', color: 'var(--text-dim)' }}>{winRate}</td>
                  <td style={{ padding: '8px 4px', color: avgReturn.startsWith('+') ? 'var(--green)' : 'var(--red)' }}>{avgReturn}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Safety Example" tag="POLICY BLOCK">
        <div style={{ padding: 16, fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-dim)', display: 'grid', gap: 8 }}>
          <div>AI Proposal: BUY AAPL · 86%</div>
          <div>Critic: PASS</div>
          <div>Risk Agent: PASS</div>
          <div style={{ color: 'var(--red)' }}>Deterministic Policy: REJECT · daily_loss_limit_reached</div>
          <div>Execution: BLOCKED</div>
        </div>
      </Panel>
    </div>
  )
}

function Metric({ label, value, danger = false }: { label: string; value: string; danger?: boolean }) {
  return (
    <div>
      <div style={{ fontFamily: 'var(--font-mono)', fontSize: 8, color: 'var(--text-muted)', letterSpacing: '0.12em', textTransform: 'uppercase' }}>{label}</div>
      <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: danger ? 'var(--red)' : 'var(--text-dim)' }}>{value}</div>
    </div>
  )
}
