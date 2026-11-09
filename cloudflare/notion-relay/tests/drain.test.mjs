import test from 'node:test';
import assert from 'node:assert/strict';
import {fixture} from './local-d1.mjs';
import {Drain} from '../src/drain.js';
import {REPOSITORY_ID,PROJECT_ID,canonical} from '../src/protocol.js';

const id='11111111-1111-4111-8111-111111111111';
const page='22222222-2222-4222-8222-222222222222';
const key=`gh:${REPOSITORY_ID}:issue:555`;
function latest(status='백로그'){return {page_id:page,key,repository_id:REPOSITORY_ID,status,
  state_sha256:'b'.repeat(64)};}
function echo(status='백로그'){return {...latest(status),project_id:PROJECT_ID,item_id:'PVTI_synthetic',
  outcome:'handled',kind:'echo_observation',reflection:false};}
async function receipt(db,name='notion:signal',generation=1,source='notion'){
  await db.prepare(`INSERT INTO receipts(receipt_id,source,semantic_digest,page_id,sync_key,
    metadata,generation,state,created_at) VALUES(?,?,?,?,?,?,?,'queued',1)`)
    .bind(name,source,'a'.repeat(64),page,source==='agent'?key:null,'{}',generation).run();
}
async function setup(){
  const {db,env}=await fixture();let dispatches=0;let correlations=0;
  const github={mainSHA:async()=>'a'.repeat(40),workflow:async()=>{},
    syncInFlight:async()=>false,
    dispatch:async()=>{dispatches++;return {state:'DISPATCH_UNKNOWN',run_id:null};},
    correlate:async()=>{correlations++;return {state:'DISPATCH_UNKNOWN',run_id:null};}};
  const drain=new Drain(env,{github,readPage:async()=>latest(),newID:()=>id,now:()=>100});
  return {db,env,drain,github,counts:()=>({dispatches,correlations})};
}
async function activated(db,entry=echo()){
  await db.prepare(`INSERT INTO sync_requests(request_id,generation,receipt_ids,expected_sha,
    run_id,phase,claimed_at,created_at,updated_at) VALUES(?,1,'[]',?,123,'ACTIVATED',1,1,1)`)
    .bind('33333333-3333-4333-8333-333333333333','a'.repeat(40)).run();
  await db.prepare(`INSERT INTO run_observations(request_id,page_id,sync_key,entry,chunk_index)
    VALUES(?,?,?,?,0)`).bind('33333333-3333-4333-8333-333333333333',page,key,canonical(entry)).run();
}
test('intent and assigned receipts exist before exactly one POST, ambiguous delivery never reposts',async()=>{
  const {db,drain,github,counts}=await setup();await receipt(db);
  const original=github.dispatch;
  github.dispatch=async request=>{
    assert.equal((await db.prepare('SELECT phase FROM sync_requests WHERE request_id=?').bind(id).first()).phase,'POSTING');
    assert.equal((await db.prepare('SELECT state FROM receipts').first()).state,'assigned');
    return original(request);};
  await drain.tick();await drain.tick();await drain.tick();
  assert.equal(counts().dispatches,1);assert.equal(counts().correlations,2);
  assert.equal((await db.prepare('SELECT phase FROM sync_requests').first()).phase,'DISPATCH_UNKNOWN');
});
test('concurrent drains retain one active request and one POST',async()=>{
  const {db,drain,env,github,counts}=await setup();await receipt(db);
  const second=new Drain(env,{github,readPage:async()=>latest(),newID:()=>
    '44444444-4444-4444-8444-444444444444',now:()=>100});
  await Promise.all([drain.tick(),second.tick()]);
  assert.equal(counts().dispatches,1);
  assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM sync_requests').first()).n,1);
});
test('stopped/draining never create a dispatch, existing matching replay becomes echo',async()=>{
  for(const selected of ['STOPPED','DRAINING']){
    const {db,drain,env,counts}=await setup();await receipt(db);await activated(db);
    env.RELAY_MODE=selected;await drain.tick();
    assert.equal(counts().dispatches,0);
    assert.equal((await db.prepare('SELECT state FROM receipts').first()).state,'ignored_echo');}
});
test('later generation own B/C event is ignored while actual late human change stays work',async()=>{
  const {db,drain,counts}=await setup();await activated(db);
  await receipt(db,'notion:own-late',9);await drain.tick();
  assert.equal(counts().dispatches,0);
  await receipt(db,'notion:human-late',10);drain.readPage=async()=>latest('진행 중');await drain.tick();
  assert.equal(counts().dispatches,1);
  assert.equal((await db.prepare("SELECT state FROM receipts WHERE receipt_id='notion:human-late'").first()).state,'assigned');
});
test('helper success always requests latest reconciliation even if a previous echo matches',async()=>{
  const {db,drain,counts}=await setup();await activated(db);await receipt(db,'agent:success',2,'agent');
  await drain.tick();assert.equal(counts().dispatches,1);
});
test('every tick freshly classifies actionable receipts and own echo cannot force a follow-up',async()=>{
  const {db,drain,counts}=await setup();await receipt(db);await activated(db);
  await db.prepare("UPDATE receipts SET state='actionable',sync_key=?").bind(key).run();
  await drain.tick();assert.equal(counts().dispatches,0);
});
test('bound receipt uses its own run checkpoint; no checkpoint cannot be reflection',async()=>{
  const {db,drain}=await setup();await receipt(db);await activated(db);
  await db.prepare("UPDATE receipts SET state='scanned',request_id=?").bind(
    '33333333-3333-4333-8333-333333333333').run();
  await drain.tick();assert.equal((await db.prepare('SELECT state FROM receipts').first()).state,'completed');
  const second=await setup();await receipt(second.db);await activated(second.db);
  await second.db.prepare("UPDATE receipts SET state='scanned',request_id=?").bind(id).run();
  await second.drain.tick();assert.equal((await second.db.prepare('SELECT state FROM receipts').first()).state,'blocked');
});
test('fresh current mismatch after handled scan preserves one subsequent reconciliation',async()=>{
  const {db,drain,counts}=await setup();await receipt(db);await activated(db);
  await db.prepare("UPDATE receipts SET state='scanned',request_id=?").bind(
    '33333333-3333-4333-8333-333333333333').run();
  drain.readPage=async()=>latest('진행 중');await drain.tick();assert.equal(counts().dispatches,1);
});
test('partial/inactive observations never suppress a receipt and preparation does not imply dispatch',async()=>{
  const {db,drain,counts}=await setup();await activated(db);
  await db.prepare("UPDATE sync_requests SET phase='CLAIMED'").run();await receipt(db);
  await drain.tick();assert.equal(counts().dispatches,1);
});
test('read/config failures keep pending receipt, schema binding blocks only affected receipt',async()=>{
  const {db,drain,counts}=await setup();await receipt(db);
  drain.readPage=async()=>{throw new Error('synthetic unavailable');};await drain.tick();
  assert.equal(counts().dispatches,0);assert.equal((await db.prepare('SELECT state FROM receipts').first()).state,'queued');
});

test('previously actionable receipt cannot dispatch after a fresh read outage; recovery reclassifies',async()=>{
  for(const ownEcho of [false,true]){
    const {db,drain,github,counts}=await setup();await receipt(db);
    const main=github.mainSHA;github.mainSHA=async()=>{throw new Error('main unavailable');};
    await assert.rejects(drain.tick());
    assert.equal((await db.prepare('SELECT state FROM receipts').first()).state,'actionable');
    github.mainSHA=main;drain.readPage=async()=>{throw new Error('Notion unavailable');};
    await drain.tick();assert.equal(counts().dispatches,0);
    const pending=await db.prepare('SELECT state,dispatch_count FROM receipts').first();
    assert.equal(pending.state,'actionable');assert.equal(pending.dispatch_count,0);
    if(ownEcho)await activated(db);
    drain.readPage=async()=>latest();await drain.tick();
    assert.equal(counts().dispatches,ownEcho?0:1);
    assert.equal((await db.prepare('SELECT state FROM receipts').first()).state,ownEcho?'ignored_echo':'assigned');
  }
});

test('fenced own checkpoint cannot finalize scanned receipts, including fence after lookup',async()=>{
  for(const race of [false,true]){
    const {db,drain}=await setup();await receipt(db);await activated(db,{...echo(),reflection:true});
    const ownID='33333333-3333-4333-8333-333333333333';
    await db.prepare("UPDATE receipts SET state='scanned',request_id=?").bind(ownID).run();
    if(!race)await db.prepare("UPDATE sync_requests SET phase='RESULT_FENCED'").run();
    else{
      const prepare=db.prepare.bind(db);let fired=false;
      db.prepare=sql=>{
        const statement=prepare(sql);
        if(!sql.startsWith('UPDATE receipts SET state='))return statement;
        return {...statement,bind:(...args)=>{
          const bound=statement.bind(...args);return {...bound,run:async()=>{
            if(!fired){fired=true;await prepare("UPDATE sync_requests SET phase='RESULT_FENCED'").run();}
            return bound.run();
          }};
        }};
      };
    }
    await drain.tick();
    assert.equal((await db.prepare('SELECT phase FROM sync_requests').first()).phase,'RESULT_FENCED');
    const final=await db.prepare('SELECT state,reason_code FROM receipts').first();
    assert.ok(!['completed','reflected','ignored_echo'].includes(final.state));
    assert.notEqual(final.reason_code,'STRICT_REFLECTION');
  }
});

test('activated echo lookup fenced before receipt CAS cannot suppress new receipt',async()=>{
  const {db,drain,counts}=await setup();await receipt(db);await activated(db);
  const prepare=db.prepare.bind(db);let fired=false;
  db.prepare=sql=>{
    const statement=prepare(sql);if(!sql.startsWith('UPDATE receipts SET state='))return statement;
    return {...statement,bind:(...args)=>{
      const bound=statement.bind(...args);return {...bound,run:async()=>{
        if(!fired){fired=true;await prepare("UPDATE sync_requests SET phase='RESULT_FENCED'").run();}
        return bound.run();
      }};
    }};
  };
  await drain.tick();assert.equal(counts().dispatches,0);
  assert.equal((await db.prepare('SELECT state FROM receipts').first()).state,'queued');
});

test('periodic full scan dispatches without webhook receipts and uses the same durable claim slot',async()=>{
  const {db,drain,counts}=await setup();
  await drain.tick({scheduled:true});
  const request=await db.prepare('SELECT * FROM sync_requests').first();
  assert.equal(request.receipt_ids,'[]');
  assert.equal(request.expected_sha,'a'.repeat(40));
  assert.equal(request.phase,'DISPATCH_UNKNOWN');
  await drain.tick({scheduled:true});
  assert.equal(counts().dispatches,1);
  assert.equal(counts().correlations,1);
});

test('ten-minute cadence is enforced across restarts and concurrent scheduled ticks',async()=>{
  const {db,env,drain,github,counts}=await setup();
  await activated(db);
  drain.now=()=>600;
  assert.deepEqual(await drain.tick({scheduled:true}),{rate_limited:true});
  drain.now=()=>601;
  const second=new Drain(env,{github,newID:()=>'44444444-4444-4444-8444-444444444444',now:()=>601});
  await Promise.all([drain.tick({scheduled:true}),second.tick({scheduled:true})]);
  assert.equal(counts().dispatches,1);
  assert.equal((await db.prepare("SELECT COUNT(*) AS n FROM sync_requests WHERE phase!='ACTIVATED'").first()).n,1);
});

test('configured cadence cannot bypass the safe minimum and a longer interval survives restart',async()=>{
  for(const configured of ['0','300','599','NaN','600.5']){
    const {drain,env,counts}=await setup();env.PERIODIC_MIN_SECONDS=configured;
    await assert.rejects(drain.tick({scheduled:true}),{message:'PERIODIC_INTERVAL_INVALID'});
    assert.equal(counts().dispatches,0);
  }
  const {drain,db,env,counts}=await setup();await activated(db);
  env.PERIODIC_MIN_SECONDS='1200';drain.now=()=>1200;
  assert.deepEqual(await drain.tick({scheduled:true}),{rate_limited:true});
  drain.now=()=>1201;await drain.tick({scheduled:true});
  assert.equal(counts().dispatches,1);
});

test('periodic dispatch respects remote in-flight runs, read failures and both stopped modes',async()=>{
  for(const state of ['busy','unavailable','STOPPED','DRAINING']){
    const {db,env,drain,github,counts}=await setup();
    if(state==='busy')github.syncInFlight=async()=>true;
    else if(state==='unavailable')github.syncInFlight=async()=>{throw Error('synthetic outage');};
    else env.RELAY_MODE=state;
    if(state==='unavailable')await assert.rejects(drain.tick({scheduled:true}));
    else await drain.tick({scheduled:true});
    assert.equal(counts().dispatches,0);
    assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM sync_requests').first()).n,0);
  }
});

test('periodic cleanup retains ambiguous records, receipt-linked history and newest confirmed evidence',async()=>{
  const {db,drain}=await setup();
  await activated(db);
  const old='33333333-3333-4333-8333-333333333333';
  for(const [request,phase,receipts] of [[id,'ACTIVATED','[]'],
    ['44444444-4444-4444-8444-444444444444','CLAIMED','[]'],
    ['55555555-5555-4555-8555-555555555555','ACTIVATED','["notion:old"]']]){
    await db.prepare(`INSERT INTO sync_requests(request_id,generation,receipt_ids,expected_sha,phase,created_at,updated_at)
      VALUES(?,1,?,?,?,2,2)`).bind(request,receipts,'a'.repeat(40),phase).run();
  }
  drain.now=()=>8*86400;
  await drain.prunePeriodic();
  assert.equal(await db.prepare('SELECT * FROM sync_requests WHERE request_id=?').bind(old).first(),null);
  assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM run_observations WHERE request_id=?').bind(old).first()).n,0);
  assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM sync_requests').first()).n,3);
});

test('cleanup is attempted at most hourly across restarts and concurrent ticks',async()=>{
  const {db,env,drain,github}=await setup();
  let now=8*86400,batches=0;
  drain.now=()=>now;
  const batch=db.batch.bind(db);
  db.batch=queries=>{batches++;return batch(queries);};
  await drain.prunePeriodic();
  assert.equal(batches,1);
  const restarted=new Drain(env,{github,now:()=>now});
  now+=3599;
  await Promise.all([drain.prunePeriodic(),restarted.prunePeriodic()]);
  assert.equal(batches,1);
  now++;
  await Promise.all([drain.prunePeriodic(),restarted.prunePeriodic()]);
  assert.equal(batches,2);
  assert.equal((await db.prepare('SELECT last_periodic_prune_at FROM relay_meta').first()).last_periodic_prune_at,now);
});

test('periodic query plans use indexes for time, eligible requests and receipt references',async()=>{
  const {db,drain}=await setup();
  const statements=[];
  const prepare=db.prepare.bind(db);
  db.prepare=sql=>{statements.push(sql);return prepare(sql);};
  await drain.prunePeriodic();
  await drain.periodic();
  const max=statements.find(sql=>sql.startsWith('SELECT MAX(created_at)'));
  const deletion=statements.find(sql=>sql.startsWith('DELETE FROM sync_requests'));
  const maxPlan=db.sqlite.prepare('EXPLAIN QUERY PLAN '+max).all().map(row=>row.detail).join('\\n');
  const prunePlan=db.sqlite.prepare('EXPLAIN QUERY PLAN '+deletion).all(1).map(row=>row.detail).join('\\n');
  assert.match(maxPlan,/USING COVERING INDEX sync_requests_created/);
  assert.match(prunePlan,/SEARCH r USING INDEX sync_requests_prune/);
  assert.match(prunePlan,/SEARCH e USING COVERING INDEX receipts_request/);
  assert.match(prunePlan,/SEARCH newer(?: EXISTS)? USING COVERING INDEX sync_requests_activated_created/);
});
