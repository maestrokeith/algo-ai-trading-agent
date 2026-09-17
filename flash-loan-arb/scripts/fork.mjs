import {execFileSync} from 'node:child_process';
import {readFile} from 'node:fs/promises';
import {localRPC} from '../src/rpc.mjs';
import {config} from '../src/config.mjs';
import {scan} from '../src/engine.mjs';
import {address,callData,tradeData,words,json,formatUnits} from '../src/abi.mjs';
const url=process.env.FORK_RPC_URL||'http://127.0.0.1:8545';
const rpc=localRPC(url);
const accounts=await rpc.sendLocal('eth_accounts');
if(!accounts.length)throw Error('Start Anvil with unlocked test accounts');
const from=accounts[0];
execFileSync('forge',['build'],{stdio:'inherit'});
const artifact=JSON.parse(await readFile('out/FlashArb.sol/FlashArb.json','utf8'));
const snapshot=await scan(url,{fork:true,rpc});
const candidates=snapshot.rows.filter(x=>x.candidate).sort((a,b)=>a.net>b.net?-1:1);
if(!candidates.length){console.log(json({...snapshot,message:'No eligible candidate; no fork trade executed.'}));process.exit(0);}
async function receipt(hash){
  for(let i=0;i<60;i++){
    const r=await rpc.request('eth_getTransactionReceipt',[hash]);
    if(r){if(BigInt(r.status)!==1n)throw Error('Local transaction reverted');return r;}
    await new Promise(r=>setTimeout(r,250));
  }throw Error('Local transaction receipt timed out');
}
const deployData=artifact.bytecode.object+[snapshot.pool,config.asset,config.middle,...config.routers.map(x=>x.address)].map(address).join('');
const deployed=await receipt(await rpc.sendLocal('eth_sendTransaction',[{from,data:deployData,gas:'0x4c4b40'}]));
const to=deployed.contractAddress;
const trade={...candidates[0].trade};
const block=await rpc.request('eth_getBlockByNumber',['latest',false]);
trade.deadline=BigInt(block.timestamp)+120n;
let tx={from,to,data:tradeData(trade)};
let estimate=BigInt(await rpc.request('eth_estimateGas',[tx]));
trade.minProfit=(estimate*125n/100n)*snapshot.bufferedGasPrice+config.minNetProfit;
tx={from,to,data:tradeData(trade)};
const simulated=words(await rpc.request('eth_call',[tx,'latest']))[0];
const balance=async()=>words(await rpc.call(config.asset,callData('balanceOf(address)',[address(from)]),'latest'))[0];
const before=await balance();
estimate=BigInt(await rpc.request('eth_estimateGas',[tx]));
const result=await receipt(await rpc.sendLocal('eth_sendTransaction',[{...tx,gas:'0x'+(estimate*125n/100n).toString(16)}]));
const delta=(await balance())-before;
const gasCost=BigInt(result.gasUsed)*BigInt(result.effectiveGasPrice);
console.log(json({mode:'LOCAL FORK ONLY — no real funds',sourceBlock:snapshot.block,
  transactionHash:result.transactionHash,simulatedProfitWeth:formatUnits(simulated),
  receivedWeth:formatUnits(delta),gasCostEth:formatUnits(gasCost),netAfterGasWeth:formatUnits(delta-gasCost)}));
