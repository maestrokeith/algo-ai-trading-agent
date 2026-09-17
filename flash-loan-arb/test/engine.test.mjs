import test from 'node:test';
import assert from 'node:assert/strict';
import {selector,keccak,word,address,words,quoteOutput,parseUnits,formatUnits,tradeData} from '../src/abi.mjs';
import {accounting,haircut,scan} from '../src/engine.mjs';
import {demo,amountOut} from '../src/demo.mjs';
import {RPC,localRPC} from '../src/rpc.mjs';
import {config} from '../src/config.mjs';
const p=parseUnits;
const addr=n=>'0x'+n.toString(16).padStart(40,'0');
const response=(...w)=>'0x'+w.map(word).join('');
test('Ethereum Keccak and known ABI selectors',()=>{
 assert.equal(keccak(''),'c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470');
 assert.equal(selector('transfer(address,uint256)'),'a9059cbb');
 assert.equal(selector('getAmountsOut(uint256,address[])'),'d06ca61f');
 assert.equal(selector('balanceOf(address)'),'70a08231');
});
test('exact decimal parsing and formatting beyond Number precision',()=>{
 assert.equal(p('123456789.123456789012345678'),123456789123456789012345678n);
 assert.equal(formatUnits(-p('0.000000000000000001')),'-0.000000000000000001');
 assert.throws(()=>p('1.0000001',6));assert.throws(()=>p('-1'));assert.throws(()=>word(-1));
 assert.throws(()=>address('0x123'));assert.throws(()=>words('0x12'));
});
test('quote ABI validates dynamic length and offsets',()=>{
 assert.equal(quoteOutput(response(32,2,100,200)),200n);
 assert.throws(()=>quoteOutput(response(64,2,100,200)));
 assert.throws(()=>quoteOutput(response(32,3,100,200)));
});
test('net accounts for premium and gas, includes precise threshold',()=>{
 const r=accounting(p('1'),p('1.01'),5n,p('0.003'),p('0.0065'));
 assert.equal(r.net,p('0.0065'));assert.equal(r.candidate,true);
 assert.equal(accounting(p('1'),p('1.01'),5n,p('0.004'),p('0.0065')).candidate,false);
 assert.equal(accounting(1n,10n,5n,0n,1n).premium,1n);
});
test('slippage rejects invalid bps and constant product includes price impact',()=>{
 assert.equal(haircut(10000n,30n),9970n);assert.throws(()=>haircut(100n,10000n));
 assert.ok(amountOut(100n,1000n,1000n)<100n);
});
test('synthetic demo exercises candidates and rejections without fake profits',()=>{
 const s=demo();assert.equal(s.rows.length,12);assert.ok(s.rows.some(r=>r.candidate));
 assert.ok(s.rows.some(r=>!r.candidate));assert.equal(s.executedTrades,0);assert.equal(s.realizedProfitWeth,'0');
});
test('read-only RPC cannot broadcast; fork rejects remote hosts',async()=>{
 const rpc=new RPC('http://localhost:8545');await assert.rejects(rpc.request('eth_sendRawTransaction'),/Read-only/);
 assert.throws(()=>localRPC('https://example.com'),/localhost/);
});
class FakeRPC {
 constructor({chain=1n,stale=false,reorg=false,quoteFail=false,samePair=false}={}) {Object.assign(this,{chain,stale,reorg,quoteFail,samePair});this.tags=[];}
 async request(method,params=[]) {
  if(method==='eth_chainId')return '0x'+this.chain.toString(16);
  if(method==='eth_getBlockByNumber')return {number:'0x100',hash:this.reorg&&params[0]!=='latest'?'different':'hash',timestamp:'0x'+(BigInt(Math.floor(Date.now()/1000))-(this.stale?1000n:0n)).toString(16)};
  if(method==='eth_getCode')return '0x1234';if(method==='eth_gasPrice')return '0x1';throw Error(method);
 }
 async simple(to,sig,tag) {
  this.tags.push(tag);
  if(sig==='getPool()')return response(9);
  if(sig==='decimals()')return response(18);
  if(sig==='FLASHLOAN_PREMIUM_TOTAL()')return response(5);
  if(sig==='WETH()')return '0x'+address(config.asset);
  if(sig==='factory()')return response(to===config.routers[0].address?10:11);
  throw Error(sig);
 }
 async call(to,data,tag){
  this.tags.push(tag);
  if(data.startsWith('0x'+selector('getPair(address,address)')))return response(this.samePair?20:BigInt(to)+20n);
  if(this.quoteFail)throw Error('no quote');
  const amount=BigInt('0x'+data.slice(10,74));return response(32,2,amount,amount*102n/100n);
 }
}
test('scanner pins quote reads and reports estimates only',async()=>{
 const rpc=new FakeRPC();const result=await scan('http://unused',{rpc});
 assert.equal(result.rows.length,12);assert.ok(rpc.tags.every(t=>t==='0x100'));
 assert.equal(result.realizedProfitWeth,'0');assert.ok(result.rows.some(r=>r.candidate));
});
for(const [name,opts,pattern] of [['wrong chain',{chain:137n},/Wrong chain/],['stale block',{stale:true},/Stale/],['reorg',{reorg:true},/reorganized/],['shared liquidity',{samePair:true},/share a pool/]])
 test('scanner rejects '+name,async()=>assert.rejects(scan('http://unused',{rpc:new FakeRPC(opts)}),pattern));
test('quote failures never become candidates',async()=>{
 await assert.rejects(scan('http://unused',{rpc:new FakeRPC({quoteFail:true})}),/no quote/);
});
test('trade encoding is seven static ABI words',()=>{
 const data=tradeData({reverse:false,amount:1n,minOutA:2n,minOutB:3n,minProfit:4n,deadline:5n,maxPremiumBps:6n});
 assert.equal(data.length,2+8+7*64);
});
