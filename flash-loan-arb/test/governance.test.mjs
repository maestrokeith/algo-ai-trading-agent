import test from 'node:test';
import assert from 'node:assert/strict';
import {negativeCycle} from '../src/graph.mjs';
import {CircuitBreaker,feeBudget} from '../src/governance.mjs';
import {PrivateRelay,inspectSignedTransaction} from '../src/private-relay.mjs';
import {parseUnits,tradeData,word} from '../src/abi.mjs';
const edge=(from,to,rate,router='a')=>({from,to,rate,router});
test('Bellman-Ford retains parallel edges and finds profitable cycle',()=>{
 const cycle=negativeCycle(['A','B'],[edge('A','B',1),edge('A','B',1.1,'b'),edge('B','A',0.95)]);
 assert.equal(cycle.length,2);assert.ok(cycle.some(e=>e.router==='b'));
});
test('Bellman-Ford finds triangular cycles and rejects losing graph',()=>{
 assert.equal(negativeCycle(['A','B','C'],[edge('A','B',2),edge('B','C',2),edge('C','A',0.3)]).length,3);
 assert.deepEqual(negativeCycle(['A','B'],[edge('A','B',2),edge('B','A',0.4)]),[]);
 assert.throws(()=>negativeCycle(['A'],[edge('A','B',1)]));
});
test('circuit breaker trips, cools down and supports emergency pause',()=>{
 let now=0;const c=new CircuitBreaker({now:()=>now});
 c.failure();c.failure();assert.equal(c.available(),true);c.failure();assert.throws(()=>c.assert());
 now=60001;assert.equal(c.available(),true);c.success();assert.equal(c.failures,0);c.pause();now+=100000;assert.equal(c.available(),false);
});
test('tip rises with missed inclusion but cannot consume profit reserve',()=>{
 const args={baseFee:1_000_000_000n,gasLimit:500000n,grossProfit:parseUnits('0.01'),minNetProfit:parseUnits('0.001'),observedPriority:1_000_000_000n};
 const a=feeBudget(args),b=feeBudget({...args,missedBlocks:5});
 assert.ok(b.maxPriorityFeePerGas>a.maxPriorityFeePerGas);assert.ok(b.conservativeNet>=args.minNetProfit);
 assert.equal(feeBudget({...args,grossProfit:100n}),null);
});
// RLP encoder is test-only. Production signing remains outside the bot.
function enc(x){
 const bytes=Array.isArray(x)?Buffer.concat(x.map(enc)):Buffer.from(x);
 const offset=Array.isArray(x)?192:128;
 if(!Array.isArray(x)&&bytes.length===1&&bytes[0]<128)return bytes;
 if(bytes.length<=55)return Buffer.concat([Buffer.from([offset+bytes.length]),bytes]);
 let h=bytes.length.toString(16);if(h.length%2)h='0'+h;const len=Buffer.from(h,'hex');
 return Buffer.concat([Buffer.from([offset+55+len.length]),len,bytes]);
}
const integer=n=>{if(n===0n)return Buffer.alloc(0);let h=n.toString(16);return Buffer.from(h.length%2?'0'+h:h,'hex');};
const executor='0x'+'12'.repeat(20),minNetProfit=parseUnits('0.001');
function signed({chain=1n,to=executor,minimum=parseUnits('0.01'),fee=1_000_000_000n}={}){
 const data=tradeData({reverse:false,amount:parseUnits('1'),minOutA:1n,minOutB:1n,minProfit:minimum,deadline:BigInt(Math.floor(Date.now()/1000)+60),maxPremiumBps:5n});
 return '0x02'+enc([integer(chain),integer(0n),integer(1n),integer(fee),integer(650000n),Buffer.from(to.slice(2),'hex'),integer(0n),Buffer.from(data.slice(2),'hex'),[],integer(0n),integer(1n),integer(1n)]).toString('hex');
}
test('signed transaction inspection rejects wrong chain, target, fees and profit floor',()=>{
 assert.equal(inspectSignedTransaction(signed(),{executor,minNetProfit}).gasLimit,650000n);
 for(const opts of [{chain:137n},{to:'0x'+'34'.repeat(20)},{minimum:1n},{fee:100_000_000_000n}])
  assert.throws(()=>inspectSignedTransaction(signed(opts),{executor,minNetProfit}));
 assert.throws(()=>inspectSignedTransaction('0x1234',{executor,minNetProfit}));
});
function relayFixture({enableSubmission=false,reorg=false,revert=false}={}){
 const methods=[];let blocks=0;
 const rpc={request:async method=>method==='eth_chainId'?'0x1':{number:'0x100',timestamp:'0x'+BigInt(Math.floor(Date.now()/1000)).toString(16),hash:++blocks>1&&reorg?'changed':'same'}};
 const relay=new PrivateRelay({rpc,executor,minNetProfit,enableSubmission,
  authSigner:async()=>executor+':0x'+'11'.repeat(65),
  fetcher:async(url,request)=>{
   assert.equal(url,'https://relay.flashbots.net');const payload=JSON.parse(request.body);methods.push(payload.method);
   assert.ok(request.headers['X-Flashbots-Signature']);
   return {ok:true,json:async()=>({result:payload.method==='eth_callBundle'?{results:[{gasUsed:500000,value:'0x'+word(parseUnits('0.02')),...(revert?{error:'revert'}:{})}]}:{bundleHash:'test'}})};
  }});return {relay,methods};
}
test('private execution disabled by default without any network call',async()=>{
 const {relay,methods}=relayFixture();await assert.rejects(relay.submit(signed()),/disabled/);assert.deepEqual(methods,[]);
});
test('explicit relay adapter simulates before private submission; never public fallback',async()=>{
 const {relay,methods}=relayFixture({enableSubmission:true});await relay.submit(signed());assert.deepEqual(methods,['eth_callBundle','eth_sendBundle']);
});
test('relay rejects reverted simulation and changed head',async()=>{
 for(const opts of [{revert:true},{reorg:true}]){
  const {relay,methods}=relayFixture({...opts,enableSubmission:true});await assert.rejects(relay.submit(signed()));assert.deepEqual(methods,['eth_callBundle']);
 }
});
