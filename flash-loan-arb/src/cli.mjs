import {CircuitBreaker} from './governance.mjs';
import {scan} from './engine.mjs';
import {demo} from './demo.mjs';
import {json} from './abi.mjs';
import {config} from './config.mjs';
import {mkdir,appendFile,stat,rename} from 'node:fs/promises';
const args=new Set(process.argv.slice(2));
if([...args].some(x=>!['--demo','--watch'].includes(x))) throw Error('Supported flags: --demo --watch');
if(!args.has('--demo') && !process.env.RPC_URL) throw Error('Set RPC_URL to an Ethereum RPC, or use npm run demo');
const breaker=new CircuitBreaker();
let stop=false; process.on('SIGINT',()=>{stop=true}); process.on('SIGTERM',()=>{stop=true});
do {
  try {
    breaker.assert();
    const result=args.has('--demo')?demo():await scan(process.env.RPC_URL);
    breaker.success();
    console.log(json(result));
    await mkdir('data',{recursive:true});
    if(await stat('data/scans.jsonl').then(s=>s.size>5_000_000).catch(()=>false))
      await rename('data/scans.jsonl','data/scans.previous.jsonl');
    await appendFile('data/scans.jsonl',JSON.stringify(JSON.parse(json(result)))+'\n');
  } catch(error) {
    if(breaker.available())breaker.failure();
    console.error(json({status:'error',message:error.message,timestamp:new Date().toISOString()}));
    if(!args.has('--watch')) {process.exitCode=1;break;}
  }
  if(!args.has('--watch'))break;
  for(let elapsed=0;elapsed<config.pollMs && !stop;elapsed+=250) await new Promise(r=>setTimeout(r,250));
} while(!stop);
