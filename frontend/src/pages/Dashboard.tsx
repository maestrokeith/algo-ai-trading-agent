import { PortfolioPanel }  from '@/components/panels/PortfolioPanel'
import { PositionsPanel }  from '@/components/panels/PositionsPanel'
import { BotControlsPanel } from '@/components/panels/BotControlsPanel'
import { RiskCapsPanel }   from '@/components/panels/RiskCapsPanel'
import { TradesPanel }     from '@/components/panels/TradesPanel'
import { GateLogPanel }    from '@/components/panels/GateLogPanel'

export function Dashboard() {
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: '360px 1fr 240px',
      gridTemplateRows: 'auto auto auto',
      gap: 12,
      marginTop: 14,
      flex: 1,
    }}>
      <PortfolioPanel />
      <PositionsPanel />
      <BotControlsPanel />
      <RiskCapsPanel />
      <TradesPanel />
      <GateLogPanel />
    </div>
  )
}
