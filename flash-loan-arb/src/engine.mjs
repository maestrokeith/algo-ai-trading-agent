import {negativeCycle} from './graph.mjs';
import {RPC} from './rpc.mjs';
import {config} from './config.mjs';
import {address,asAddress,callData,words,quoteData,quoteOutput,formatUnits} from './abi.mjs';
export const haircut=(n,bps)=>{
  if(n<0n || bps<0n || bps>=10000n) throw Error('Invalid slippage');
  return n*(10000n-bps)/10000n;
};
export function accounting(amount,output,premiumBps,gasWei,minNet=config.minNetProfit) {
  if(amount<=0n || output<0n || premiumBps<0n || gasWei<0n || minNet<=0n) throw Error('Invalid accounting input');
  // Ceiling is conservative relative to Aave's half-up percentage rounding.
  const premium=(amount*premiumBps+9999n)/10000n;
  const net=output-amount-premium-gasWei;
  return {premium,gasWei,net,candidate:net>=minNet};
}
export async function scan(url,{fork=false,rpc=new RPC(url),cfg=config}={}) {
  if(cfg.slippageBps<0n || cfg.slippageBps>50n || cfg.amounts.some(a=>a<=0n||a>10n**19n)) throw Error('Slippage or borrow cap exceeded');
  const chainId=BigInt(await rpc.request('eth_chainId'));
  if(chainId!==(fork?31337n:cfg.chainId)) throw Error('Wrong chain');
  const block=await rpc.request('eth_getBlockByNumber',['latest',false]);
  if(!block?.hash || !block.number) throw Error('Missing block');
  const age=Date.now()/1000-Number(BigInt(block.timestamp));
  if(age>cfg.maxBlockAgeSeconds || age < -15) throw Error('Stale or future block');
  const tag=block.number;
  const pool=asAddress(await rpc.simple(cfg.provider,'getPool()',tag));
  const addresses=[pool,cfg.asset,cfg.middle,...cfg.routers.map(r=>r.address)];
  for(const target of addresses) {
    if((await rpc.request('eth_getCode',[target,tag]))==='0x') throw Error('Missing contract code');
  }
  const decimals=words(await rpc.simple(cfg.asset,'decimals()',tag))[0];
  if(decimals!==18n) throw Error('Borrowed asset must be 18-decimal WETH');
  const premiumBps=words(await rpc.simple(pool,'FLASHLOAN_PREMIUM_TOTAL()',tag))[0];
  if(premiumBps>100n) throw Error('Flash premium exceeds cap');
  const pairs=[];
  for(const router of cfg.routers) {
    if(asAddress(await rpc.simple(router.address,'WETH()',tag))!==cfg.asset) throw Error('Router WETH mismatch');
    const factory=asAddress(await rpc.simple(router.address,'factory()',tag));
    const pair=asAddress(await rpc.call(factory,callData('getPair(address,address)',[address(cfg.asset),address(cfg.middle)]),tag));
    if(BigInt(pair)===0n) throw Error('Missing pair'); pairs.push(pair);
  }
  if(pairs[0]===pairs[1]) throw Error('Routes share a pool');
  const edges=[];
  for(const router of cfg.routers) for(const [from,to,probe] of [[cfg.asset,cfg.middle,10n**16n],[cfg.middle,cfg.asset,30n*10n**6n]]) {
    const output=quoteOutput(await rpc.call(router.address,quoteData(probe,[from,to]),tag));
    edges.push({from,to,router:router.name,rate:Number(output)/Number(probe)});
  }
  const cycles=[];
  for(const reverse of [false,true]) {
    const first=cfg.routers[reverse?1:0].name,second=cfg.routers[reverse?0:1].name;
    const selected=edges.filter(e=>(e.from===cfg.asset&&e.router===first)||(e.from===cfg.middle&&e.router===second));
    const cycle=negativeCycle([cfg.asset,cfg.middle],selected);
    if(cycle.length)cycles.push({reverse,edges:cycle});
  }
  const gasPrice=BigInt(await rpc.request('eth_gasPrice'));
  // Buffer gas price and use fixed conservative gas units; fork simulation refines this.
  const bufferedGasPrice=gasPrice*125n/100n;
  const gasWei=bufferedGasPrice*cfg.gasUnits;
  const rows=[];
  for(const reverse of [false,true]) for(const amount of cfg.amounts) {
    const first=cfg.routers[reverse?1:0],second=cfg.routers[reverse?0:1];
    try {
      const outA=quoteOutput(await rpc.call(first.address,quoteData(amount,[cfg.asset,cfg.middle]),tag));
      const minOutA=haircut(outA,cfg.slippageBps);
      const outB=quoteOutput(await rpc.call(second.address,quoteData(minOutA,[cfg.middle,cfg.asset]),tag));
      const minOutB=haircut(outB,cfg.slippageBps);
      const result=accounting(amount,minOutB,premiumBps,gasWei,cfg.minNetProfit);
      result.candidate=result.candidate&&cycles.some(c=>c.reverse===reverse);
      const trade={reverse,amount,minOutA,minOutB,minProfit:gasWei+cfg.minNetProfit,
        deadline:BigInt(block.timestamp)+120n,maxPremiumBps:premiumBps};
      rows.push({route:first.name+' → '+second.name,amountWeth:formatUnits(amount),
        netWeth:formatUnits(result.net),status:result.candidate?'candidate — not executed':'rejected',
        ...result,trade});
    } catch { rows.push({route:first.name+' → '+second.name,amountWeth:formatUnits(amount),status:'quote unavailable'}); }
  }
  const check=await rpc.request('eth_getBlockByNumber',[tag,false]);
  if(check?.hash!==block.hash) throw Error('Block reorganized; discarded snapshot');
  if(Date.now()/1000-Number(BigInt(block.timestamp))>cfg.maxBlockAgeSeconds) throw Error('Snapshot became stale');
  return {mode:fork?'fork quotes':'read-only chain quotes',timestamp:new Date().toISOString(),
    block:Number(BigInt(tag)),blockHash:block.hash,pool,premiumBps,bufferedGasPrice,
    graphCycles:cycles,gasUnitsAssumption:cfg.gasUnits,executedTrades:0,realizedProfitWeth:'0',rows};
}
