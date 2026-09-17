import {spawn} from 'node:child_process';
import {RPC} from '../src/rpc.mjs';
if(!process.env.RPC_URL)throw Error('RPC_URL required');
const url='http://127.0.0.1:18545';
const child=spawn('anvil',['--fork-url',process.env.RPC_URL,'--chain-id','31337','--host','127.0.0.1','--port','18545','--silent'],{stdio:'ignore'});
let startupError;child.on('error',e=>{startupError=e;});
try {
 let ready=false;
 for(let i=0;i<40;i++){
  if(startupError)throw startupError;
  if(child.exitCode!==null)throw Error('Anvil exited before startup');
  try {ready=BigInt(await new RPC(url).request('eth_chainId'))===31337n;}catch{}
  if(ready)break;await new Promise(r=>setTimeout(r,500));
 }
 if(!ready)throw Error('Anvil startup timed out');
 await new Promise((resolve,reject)=>{
  const runner=spawn(process.execPath,['scripts/fork.mjs'],{stdio:'inherit',env:{...process.env,FORK_RPC_URL:url}});
  runner.once('error',reject);runner.once('exit',code=>code===0?resolve():reject(Error('Fork runner failed')));
 });
} finally {child.kill('SIGTERM');}
