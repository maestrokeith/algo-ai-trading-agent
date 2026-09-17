import {config} from './config.mjs';
import {accounting,haircut} from './engine.mjs';
import {parseUnits,formatUnits} from './abi.mjs';
export function amountOut(input,reserveIn,reserveOut) {
  if(input<=0n || reserveIn<=0n || reserveOut<=0n) throw Error('Invalid reserves');
  const adjusted=input*997n;
  return adjusted*reserveOut/(reserveIn*1000n+adjusted);
}
export function demo() {
  // Deliberately synthetic price gap for exercising both accept and reject paths.
  const pools=[{weth:parseUnits('1000'),usdc:parseUnits('3000000',6)},
    {weth:parseUnits('1000'),usdc:parseUnits('3060000',6)}];
  const rows=[];
  for(const reverse of [false,true]) for(const amount of config.amounts) {
    const a=pools[reverse?1:0],b=pools[reverse?0:1];
    const mid=haircut(amountOut(amount,a.weth,a.usdc),config.slippageBps);
    const output=haircut(amountOut(mid,b.usdc,b.weth),config.slippageBps);
    const result=accounting(amount,output,5n,parseUnits('0.0013'));
    rows.push({route:reverse?'Demo B → Demo A':'Demo A → Demo B',amountWeth:formatUnits(amount),
      netWeth:formatUnits(result.net),status:result.candidate?'synthetic candidate':'rejected',...result});
  }
  return {mode:'DEMO — synthetic reserves, no chain connection',timestamp:new Date().toISOString(),
    block:null,premiumBps:5,executedTrades:0,realizedProfitWeth:'0',rows};
}
