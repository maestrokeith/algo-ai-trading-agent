import {keccak,selector,words} from './abi.mjs';
// External signer boundary: this module never loads a key. The caller supplies
// an already signed EIP-1559 transaction and a separate relay-auth signer.
function rlp(bytes,offset=0) {
  const prefix=bytes[offset];if(prefix===undefined)throw Error('Truncated RLP');
  if(prefix<128)return [bytes.slice(offset,offset+1),offset+1];
  const list=prefix>=192,base=list?192:128,short=prefix-base;
  let len,start;
  if(short<=55){len=short;start=offset+1;}
  else {
    const size=short-55;if(size>4||offset+1+size>bytes.length)throw Error('RLP length');
    len=0;for(let i=0;i<size;i++)len=len*256+bytes[offset+1+i];start=offset+1+size;
  }
  const end=start+len;if(end>bytes.length)throw Error('Truncated RLP payload');
  if(!list)return [bytes.slice(start,end),end];
  const result=[];let pos=start;
  while(pos<end){const [item,next]=rlp(bytes,pos);if(next>end)throw Error('RLP boundary');result.push(item);pos=next;}
  return [result,end];
}
const hex=b=>'0x'+Buffer.from(b).toString('hex');
const num=b=>{if(Array.isArray(b)||b.length>32)throw Error('RLP integer');return b.length?BigInt(hex(b)):0n;};
export function inspectSignedTransaction(raw,{executor,minNetProfit,maxGas=1_200_000n,maxFee=50_000_000_000n}) {
  if(!/^0x02(?:[a-fA-F0-9]{2})+$/.test(raw)||raw.length>200000)throw Error('Signed EIP-1559 transaction required');
  const bytes=Buffer.from(raw.slice(4),'hex');const [f,end]=rlp(bytes);
  if(end!==bytes.length||!Array.isArray(f)||f.length!==12||f.slice(0,8).some(Array.isArray))throw Error('Invalid EIP-1559 fields');
  if(num(f[0])!==1n||hex(f[5]).toLowerCase()!==executor.toLowerCase()||num(f[6])!==0n)throw Error('Transaction target, chain, or value rejected');
  if(!Array.isArray(f[8])||num(f[9])>1n||num(f[10])===0n||num(f[11])===0n)throw Error('Invalid signed transaction');
  const tip=num(f[2]),fee=num(f[3]),gas=num(f[4]);
  if(gas===0n||gas>maxGas||fee===0n||fee>maxFee||tip>fee)throw Error('Fee bounds');
  const data=hex(f[7]);
  if(data.slice(2,10)!==selector('execute((bool,uint256,uint256,uint256,uint256,uint256,uint256))')||data.length!==458)throw Error('Only FlashArb.execute is allowed');
  const trade=words('0x'+data.slice(10));
  if(trade[0]>1n||trade[1]===0n||trade[1]>10n**19n||trade[2]===0n||trade[3]===0n||trade[6]>100n)throw Error('Trade bounds');
  if(typeof minNetProfit!=='bigint'||minNetProfit<=0n||trade[4]<minNetProfit+gas*fee)throw Error('Insufficient on-chain profit floor');
  return {gasLimit:gas,maxFeePerGas:fee,deadline:trade[5],minProfit:trade[4]};
}
export class PrivateRelay {
  constructor({authSigner,rpc,executor,minNetProfit,enableSubmission=false,fetcher=fetch}) {
    if(typeof authSigner!=='function'||!/^0x[0-9a-fA-F]{40}$/.test(executor))throw Error('External auth signer and deployed executor required');
    Object.assign(this,{authSigner,rpc,executor,minNetProfit,enableSubmission,fetcher});
  }
  async request(method,params) {
    if(!['eth_callBundle','eth_sendBundle'].includes(method))throw Error('Private relay method rejected');
    const body=JSON.stringify({jsonrpc:'2.0',id:1,method,params});
    // Sign the UTF-8 hex digest string as documented by Flashbots, using EIP-191.
    const auth=await this.authSigner('0x'+keccak(body));
    if(!/^0x[0-9a-fA-F]{40}:0x[0-9a-fA-F]{130}$/.test(auth))throw Error('Invalid relay authentication');
    const response=await this.fetcher('https://relay.flashbots.net',{method:'POST',
      headers:{'content-type':'application/json','X-Flashbots-Signature':auth},body,signal:AbortSignal.timeout(10000)});
    if(!response.ok)throw Error('Private relay HTTP '+response.status);
    const data=await response.json();if(data.error||!data.result)throw Error('Private relay rejected request');return data.result;
  }
  async simulate(raw) {
    const tx=inspectSignedTransaction(raw,this);
    if(BigInt(await this.rpc.request('eth_chainId'))!==1n)throw Error('Ethereum mainnet RPC required');
    const head=await this.rpc.request('eth_getBlockByNumber',['latest',false]);
    const stamp=BigInt(head.timestamp),now=BigInt(Math.floor(Date.now()/1000));
    if(now-stamp>60n||stamp>now+15n||tx.deadline<stamp+12n||tx.deadline>stamp+120n)throw Error('Stale bundle');
    const target='0x'+(BigInt(head.number)+1n).toString(16);
    const result=await this.request('eth_callBundle',[{txs:[raw],blockNumber:target,stateBlockNumber:head.number,timestamp:Number(stamp+12n)}]);
    if(!Array.isArray(result.results)||result.results.length!==1)throw Error('Invalid relay simulation');
    const r=result.results[0];
    if(r.error||r.revert||BigInt(r.gasUsed)<=0n||BigInt(r.gasUsed)>tx.gasLimit)throw Error('Bundle simulation failed');
    const profit=words(r.value)[0];
    if(profit<tx.minProfit||profit-tx.gasLimit*tx.maxFeePerGas<this.minNetProfit)throw Error('Unprofitable simulation');
    return {head,target,profit,gasUsed:BigInt(r.gasUsed)};
  }
  async submit(raw) {
    if(!this.enableSubmission)throw Error('Live submission disabled');
    const simulation=await this.simulate(raw);
    const latest=await this.rpc.request('eth_getBlockByNumber',['latest',false]);
    if(latest.hash!==simulation.head.hash)throw Error('Head changed; re-simulate and re-sign');
    // No public-RPC fallback and no allowed reverting transactions.
    return this.request('eth_sendBundle',[{txs:[raw],blockNumber:simulation.target}]);
  }
}
