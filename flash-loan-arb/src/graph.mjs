// Negative-log multigraph screen. Float math proposes cycles only;
// exact BigInt router quotes and EVM simulation must validate economics.
export function negativeCycle(vertices,edges) {
  const known=new Set(vertices);
  for(const e of edges) if(!known.has(e.from)||!known.has(e.to)||!Number.isFinite(e.rate)||e.rate<=0)
    throw Error('Invalid graph edge');
  const d=new Map(vertices.map(v=>[v,0])),parent=new Map();let changed;
  for(let i=0;i<vertices.length;i++) {
    changed=undefined;
    for(const e of edges) {
      const next=d.get(e.from)-Math.log(e.rate);
      if(next<d.get(e.to)-1e-12){d.set(e.to,next);parent.set(e.to,e);changed=e.to;}
    }
  }
  if(changed===undefined)return [];
  let v=changed;
  for(let i=0;i<vertices.length;i++){if(!parent.has(v))return [];v=parent.get(v).from;}
  const start=v,cycle=[];
  do {const e=parent.get(v);if(!e)return [];cycle.push(e);v=e.from;}while(v!==start&&cycle.length<=vertices.length);
  cycle.reverse();
  if(cycle.reduce((sum,e)=>sum-Math.log(e.rate),0)>=-1e-12)return [];
  return cycle;
}
