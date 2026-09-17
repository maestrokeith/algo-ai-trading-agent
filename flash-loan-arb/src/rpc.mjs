import {callData} from './abi.mjs';
const READ = new Set(['eth_chainId','eth_blockNumber','eth_getBlockByNumber','eth_getCode','eth_call','eth_gasPrice','eth_estimateGas','eth_getTransactionReceipt']);
export class RPC {
  constructor(url) {
    const u=new URL(url);
    if(!['http:','https:'].includes(u.protocol)) throw Error('HTTP(S) RPC required');
    this.url=url; this.id=0;
  }
  async request(method,params=[]) {
    if(!READ.has(method)) throw Error('Read-only RPC method rejected');
    return this.transport(method,params);
  }
  async transport(method,params=[]) {
    const id=++this.id;
    const response=await fetch(this.url,{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({jsonrpc:'2.0',id,method,params}),signal:AbortSignal.timeout(15000)});
    if(!response.ok) throw Error('RPC HTTP '+response.status);
    const data=await response.json();
    if(data.id!==id || data.error || data.result===undefined) throw Error('RPC request failed: '+method);
    return data.result;
  }
  call(to,data,block) { return this.request('eth_call',[{to,data},block]); }
  simple(to,sig,block) { return this.call(to,callData(sig),block); }
}
export function localRPC(url) {
  const u=new URL(url);
  if(!['127.0.0.1','localhost','[::1]'].includes(u.hostname)) throw Error('Fork transactions require localhost');
  const rpc=new RPC(url);
  rpc.sendLocal=async(method,params=[])=>{
    if(!['eth_accounts','eth_sendTransaction'].includes(method)) throw Error('Local method rejected');
    if(BigInt(await rpc.request('eth_chainId'))!==31337n) throw Error('Local fork chain ID must be 31337');
    return rpc.transport(method,params);
  };
  return rpc;
}
