// Public pending-hash/new-head notifications. Requires a provider that exposes
// eth_subscribe; Flashbots Protect is NOT treated as a public mempool feed.
export function monitor(url,onSignal,{onStatus=()=>{},minimumIntervalMs=2000}={}) {
  const u=new URL(url);if(!['ws:','wss:'].includes(u.protocol))throw Error('WebSocket RPC required');
  let socket,timer,stopped=false,attempt=0,last=0;const subscriptions=new Set();
  function connect(){
    socket=new WebSocket(url);
    socket.addEventListener('open',()=>{
      attempt=0;subscriptions.clear();onStatus('connected');
      for(const [i,kind]of ['newHeads','newPendingTransactions'].entries())
        socket.send(JSON.stringify({jsonrpc:'2.0',id:i+1,method:'eth_subscribe',params:[kind]}));
    });
    socket.addEventListener('message',event=>{
      let data;try{data=JSON.parse(event.data);}catch{return;}
      if(data.error){onStatus('subscription rejected');return;}
      if(data.id&&typeof data.result==='string'){subscriptions.add(data.result);return;}
      if(data.method!=='eth_subscription'||!subscriptions.has(data.params?.subscription))return;
      const now=Date.now();if(now-last<minimumIntervalMs)return;last=now;
      Promise.resolve(onSignal(data.params.result)).catch(()=>onStatus('signal handler failed'));
    });
    socket.addEventListener('error',()=>{onStatus('connection error');socket.close();});
    socket.addEventListener('close',()=>{
      onStatus('disconnected');if(!stopped)timer=setTimeout(connect,Math.min(30000,1000*2**Math.min(attempt++,5)));
    });
  }
  connect();return ()=>{stopped=true;clearTimeout(timer);socket?.close();};
}
