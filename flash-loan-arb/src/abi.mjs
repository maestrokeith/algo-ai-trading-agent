// Minimal read-only ABI codec. No wallet, signing, or third-party dependencies.
const MASK = (1n << 64n) - 1n;
const ROT = [0,1,62,28,27,36,44,6,55,20,3,10,43,25,39,41,45,15,21,8,18,2,61,56,14];
const RC = ['1','8082','800000000000808a','8000000080008000','808b','80000001','8000000080008081','8000000000008009','8a','88','80008009','8000000a','8000808b','800000000000008b','8000000000008089','8000000000008003','8000000000008002','8000000000000080','800a','800000008000000a','8000000080008081','8000000000008080','80000001','8000000080008008'].map(x=>BigInt('0x'+x));
const rot = (v,n) => ((v << BigInt(n)) | (v >> BigInt(64-n))) & MASK;
export function keccak(text) {
  const bytes = [...new TextEncoder().encode(text)];
  const remaining=136-bytes.length%136;
  if(remaining===1) bytes.push(129);
  else {bytes.push(1);while(bytes.length%136!==135)bytes.push(0);bytes.push(128);}
  const s = Array(25).fill(0n);
  for (let off=0; off<bytes.length; off+=136) {
    for(let i=0;i<136;i++) s[Math.floor(i/8)] ^= BigInt(bytes[off+i]) << BigInt((i%8)*8);
    for(const rc of RC) {
      const c = Array.from({length:5},(_,x)=>s[x]^s[x+5]^s[x+10]^s[x+15]^s[x+20]);
      for(let x=0;x<5;x++) for(let y=0;y<5;y++) s[x+5*y] ^= c[(x+4)%5]^rot(c[(x+1)%5],1);
      const b=Array(25).fill(0n);
      for(let x=0;x<5;x++) for(let y=0;y<5;y++) b[y+5*((2*x+3*y)%5)]=rot(s[x+5*y],ROT[x+5*y]);
      for(let x=0;x<5;x++) for(let y=0;y<5;y++) s[x+5*y]=b[x+5*y]^((~b[(x+1)%5+5*y])&b[(x+2)%5+5*y]);
      s[0]^=rc;
    }
  }
  return Array.from({length:32},(_,i)=>Number((s[Math.floor(i/8)]>>BigInt((i%8)*8))&255n).toString(16).padStart(2,'0')).join('');
}
export const selector = signature => keccak(signature).slice(0,8);
export function word(value) {
  const n = BigInt(value); if(n<0n || n >= 1n<<256n) throw Error('ABI integer out of range');
  return n.toString(16).padStart(64,'0');
}
export function address(value) {
  if(!/^0x[0-9a-fA-F]{40}$/.test(value)) throw Error('Invalid address');
  return value.slice(2).toLowerCase().padStart(64,'0');
}
export const callData = (sig,words=[])=>'0x'+selector(sig)+words.join('');
export function words(data) {
  if(!/^0x(?:[0-9a-fA-F]{64})+$/.test(data)) throw Error('Malformed ABI response');
  return data.slice(2).match(/.{64}/g).map(x=>BigInt('0x'+x));
}
export function asAddress(data) {
  const w=words(data); if(w.length!==1 || w[0] >= 1n<<160n) throw Error('Malformed address');
  return '0x'+w[0].toString(16).padStart(40,'0');
}
export function quoteData(amount,path) {
  if(path.length!==2) throw Error('Only direct two-token paths supported');
  return callData('getAmountsOut(uint256,address[])',[word(amount),word(64),word(2),...path.map(address)]);
}
export function quoteOutput(data) {
  const w=words(data); if(w.length!==4 || w[0]!==32n || w[1]!==2n) throw Error('Malformed quote');
  if(w[3]<=0n) throw Error('Zero quote'); return w[3];
}
export function tradeData(t) {
  return callData('execute((bool,uint256,uint256,uint256,uint256,uint256,uint256))',
    [t.reverse?1:0,t.amount,t.minOutA,t.minOutB,t.minProfit,t.deadline,t.maxPremiumBps].map(word));
}
export function parseUnits(s,decimals=18) {
  if(!/^\d+(\.\d+)?$/.test(String(s))) throw Error('Invalid decimal amount');
  const [whole,fraction='']=String(s).split('.');
  if(fraction.length>decimals) throw Error('Too many decimal places');
  return BigInt(whole)*10n**BigInt(decimals)+BigInt(fraction.padEnd(decimals,'0')||'0');
}
export function formatUnits(n,decimals=18) {
  n=BigInt(n); const sign=n<0n?'-':''; if(n<0n)n=-n;
  const scale=10n**BigInt(decimals);
  return sign+(n/scale)+'.'+(n%scale).toString().padStart(decimals,'0');
}
export const json = value => JSON.stringify(value,(_,v)=>typeof v==='bigint'?v.toString():v,2);
