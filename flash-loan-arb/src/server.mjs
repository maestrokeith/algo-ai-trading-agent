import {CircuitBreaker} from './governance.mjs';
import {monitor} from './mempool.mjs';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {scan} from './engine.mjs';
import {demo} from './demo.mjs';
import {json} from './abi.mjs';
import {config} from './config.mjs';
const html=await readFile(new URL('../public/index.html',import.meta.url));
let state={mode:process.env.RPC_URL?'read-only chain quotes':'DEMO',status:'starting',rows:[]};
let stopped=false,timer,busy=false;
const breaker=new CircuitBreaker();
async function tick() {
  if(stopped||busy)return;busy=true;clearTimeout(timer);
  try {breaker.assert();state={...(process.env.RPC_URL?await scan(process.env.RPC_URL):demo()),status:'ready'};breaker.success();}
  catch(error) {if(breaker.available())breaker.failure();state={mode:'read-only chain quotes',status:'error',message:error.message,rows:[],timestamp:new Date().toISOString()};}
  busy=false;
  if(!stopped) timer=setTimeout(tick,config.pollMs);
}
void tick();
const stopMonitor=process.env.WS_RPC_URL&&process.env.RPC_URL?monitor(process.env.WS_RPC_URL,()=>tick(),{minimumIntervalMs:5000}):()=>{};
const server=createServer((req,res)=>{
  res.setHeader('Cache-Control','no-store');
  res.setHeader('X-Content-Type-Options','nosniff');
  res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'");
  if(req.method!=='GET'){res.writeHead(405);return res.end();}
  if(req.url==='/api/status'){res.setHeader('Content-Type','application/json');return res.end(json(state));}
  if(req.url!=='/'){res.writeHead(404);return res.end('Not found');}
  res.setHeader('Content-Type','text/html; charset=utf-8');res.end(html);
});
const port=Number(process.env.PORT||8787);
server.listen(port,'127.0.0.1',()=>console.log('Dashboard: http://127.0.0.1:'+port));
for(const signal of ['SIGINT','SIGTERM']) process.on(signal,()=>{stopped=true;stopMonitor();clearTimeout(timer);server.close();});
