export class CircuitBreaker {
  constructor({maxFailures=3,cooldownMs=60000,now=()=>Date.now()}={}) {
    this.maxFailures=maxFailures;this.cooldownMs=cooldownMs;this.now=now;this.failures=0;this.until=0;this.paused=false;
  }
  available(){return !this.paused&&this.now()>=this.until;}
  assert(){if(!this.available())throw Error('Circuit breaker open');}
  success(){this.failures=0;this.until=0;}
  failure(){if(++this.failures>=this.maxFailures)this.until=this.now()+this.cooldownMs;}
  pause(){this.paused=true;}
}
export function feeBudget({baseFee,gasLimit,grossProfit,minNetProfit,observedPriority,missedBlocks=0,maxTip=5_000_000_000n}) {
  if([baseFee,gasLimit,grossProfit,minNetProfit,observedPriority,maxTip].some(x=>typeof x!=='bigint'||x<0n)||gasLimit===0n)
    throw Error('Invalid fee inputs');
  if(!Number.isInteger(missedBlocks)||missedBlocks<0||missedBlocks>20)throw Error('Invalid inclusion feedback');
  const nextBase=(baseFee*9n+7n)/8n+1n;
  const available=grossProfit-minNetProfit-gasLimit*nextBase;
  if(available<=0n)return null;
  const budgetPerGas=(available*20n/100n)/gasLimit;
  const demand=observedPriority*BigInt(100+Math.min(missedBlocks,8)*15)/100n;
  const tip=[demand,budgetPerGas,maxTip].reduce((a,b)=>a<b?a:b);
  if(tip===0n)return null;
  return {maxPriorityFeePerGas:tip,maxFeePerGas:nextBase+tip,gasLimit,
    conservativeNet:grossProfit-gasLimit*(nextBase+tip)};
}
