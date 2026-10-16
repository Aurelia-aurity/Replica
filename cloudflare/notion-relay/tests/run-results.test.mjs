import test from 'node:test';
import assert from 'node:assert/strict';
import {fixture} from './local-d1.mjs';
import {RunResults,runName,WORKFLOW_PATH,GENESIS} from '../src/run-results.js';
import {canonical,sha256,REPOSITORY_ID,PROJECT_ID} from '../src/protocol.js';

const id='11111111-1111-4111-8111-111111111111';
const sha='a'.repeat(40);
const binding={request_id:id,generation:1,run_id:123,sha,attempt:1};
async function setup(){
  const {db,env}=await fixture();env.GITHUB_DISPATCH_ACTOR_ID='7';env.GITHUB_ACTOR_ID='999';env.GITHUB_WORKFLOW_ID='8';
  await db.prepare(`INSERT INTO sync_requests(request_id,generation,receipt_ids,expected_sha,
    run_id,phase,created_at,updated_at) VALUES(?,1,?,?,NULL,'DISPATCH_UNKNOWN',1,1)`)
    .bind(id,JSON.stringify(['notion:synthetic']),sha).run();
  await db.prepare('UPDATE request_slot SET request_id=? WHERE singleton=1').bind(id).run();
  const run={id:123,repository:{id:REPOSITORY_ID,full_name:'Aurelia-aurity/Replica'},
    head_repository:{id:REPOSITORY_ID},event:'workflow_dispatch',head_branch:'main',head_sha:sha,
    run_attempt:1,workflow_id:8,path:WORKFLOW_PATH,display_title:runName(id,1),actor:{id:7},
    triggering_actor:{id:7},status:'in_progress',conclusion:null};
  const results=new RunResults(db,env,{getRun:async()=>structuredClone(run)},()=>100);
  return {db,env,run,results};
}
function entry(n){return {page_id:`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`,
  key:`gh:${REPOSITORY_ID}:issue:${n}`,repository_id:REPOSITORY_ID,project_id:PROJECT_ID,
  item_id:`PVTI_item${n}`,status:'진행 중',state_sha256:'b'.repeat(64),outcome:'handled',
  kind:'echo_observation',reflection:false};}
async function chunk(index,count,entries,previous=GENESIS){
  const value={...binding,index,chunk_count:count,previous_digest:previous,entries};
  return {...value,chunk_digest:await sha256(canonical(value))};
}
function seal(count,entries,final){return {...binding,chunk_count:count,entry_count:entries,
  final_digest:final,report_digest:'c'.repeat(64),scan_complete:true,failed:false};}
async function request(db){return db.prepare('SELECT * FROM sync_requests WHERE request_id=?').bind(id).first();}

test('claim is single-use, unknown UUID/HMAC metadata alone cannot authorize and stopped refuses',async()=>{
  const {results,db,env}=await setup();
  const claims=await Promise.all([results.claim(binding),results.claim(binding)]);
  assert.equal(claims.filter(value=>value.newly_authorized).length,1);
  await assert.rejects(results.claim({...binding,request_id:'22222222-2222-4222-8222-222222222222'}));
  env.RELAY_MODE='STOPPED';await assert.rejects(results.claim(binding),{message:'CLAIM_DISABLED'});
  assert.equal((await request(db)).phase,'CLAIMED');
});
test('independent run binding rejects SHA/ref/repo/actor/workflow/title/rerun mismatch before claim',async()=>{
  const changes=[r=>r.head_sha='d'.repeat(40),r=>r.head_branch='feature',r=>r.actor.id=9,
    r=>r.triggering_actor.id=9,r=>r.repository.id=99,r=>r.head_repository.id=99,
    r=>r.run_attempt=2,r=>r.event='schedule',r=>r.path='.github/workflows/notion-sync.yml',
    r=>r.display_title=runName(id,2),r=>r.workflow_id=99];
  for(const change of changes){const {results,run,db}=await setup();change(run);
    await assert.rejects(results.claim(binding),{message:'RUN_BINDING'});
    assert.equal((await request(db)).claimed_at,null);}
});
test('all 23 full-run observations use three exact chained chunks, only complete terminal activates',async()=>{
  const {results,db,run}=await setup();await results.claim(binding);
  const chunks=[await chunk(0,3,Array.from({length:10},(_,i)=>entry(i+1)))];
  chunks.push(await chunk(1,3,Array.from({length:10},(_,i)=>entry(i+11)),chunks[0].chunk_digest));
  chunks.push(await chunk(2,3,Array.from({length:3},(_,i)=>entry(i+21)),chunks[1].chunk_digest));
  await results.chunk(chunks[2]);await results.chunk(chunks[0]);
  await assert.rejects(results.result(seal(3,23,chunks[2].chunk_digest)),{message:'RESULT_INCOMPLETE'});
  await results.chunk(chunks[1]);
  assert.equal((await results.chunk(chunks[1])).duplicate,true);
  assert.equal((await results.result(seal(3,23,chunks[2].chunk_digest))).activated,false);
  assert.equal((await results.activate(await request(db))).pending,true);
  run.status='completed';run.conclusion='success';
  assert.equal((await results.activate(await request(db))).activated,true);
  assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM run_observations').first()).n,23);
  assert.equal((await db.prepare('SELECT request_id FROM request_slot').first()).request_id,null);
});
test('STOPPED records already claimed chunks and seal; green without seal never activates',async()=>{
  const {results,db,env,run}=await setup();await results.claim(binding);env.RELAY_MODE='STOPPED';
  run.status='completed';run.conclusion='success';
  await assert.rejects(results.activate(await request(db)),{message:'ACTIVATE_PHASE'});
  const value=await chunk(0,1,[entry(1)]);await results.chunk(value);
  await results.result(seal(1,1,value.chunk_digest));
  assert.equal((await results.activate(await request(db))).activated,true);
});
test('contradictory indexed evidence stays fenced and retains active request',async()=>{
  const {results,db}=await setup();await results.claim(binding);
  await results.chunk(await chunk(0,1,[entry(1)]));
  await assert.rejects(results.chunk(await chunk(0,1,[{...entry(1),status:'백로그'}])),
    {message:'CHUNK_CONTRADICTION'});
  assert.equal((await request(db)).phase,'RESULT_FENCED');
  assert.equal((await db.prepare('SELECT request_id FROM request_slot').first()).request_id,id);
});
test('cross-chunk duplicate/out-of-order keys/pages are not silently truncated',async()=>{
  const {results,db}=await setup();await results.claim(binding);
  const a=await chunk(0,2,[entry(1)]);await results.chunk(a);
  await assert.rejects(results.chunk(await chunk(1,2,[entry(1)],a.chunk_digest)),{message:'CHUNK_CHAIN'});
  assert.equal((await request(db)).phase,'RESULT_FENCED');
});
test('failed/incomplete scan seal never becomes reflection/active echo from a green job',async()=>{
  for(const failure of [{failed:true},{scan_complete:false}]){
    const {results,db,run}=await setup();await results.claim(binding);
    await results.result({...seal(0,0,GENESIS),...failure});run.status='completed';run.conclusion='success';
    assert.equal((await results.activate(await request(db))).fenced,true);
    assert.equal((await request(db)).phase,'FAILED_FENCED');}
});
test('bounded entries, forged digest and blocked reflection rejected before data writes',async()=>{
  const {results,db}=await setup();await results.claim(binding);
  for(const value of [await chunk(0,1,Array.from({length:11},(_,i)=>entry(i+1))),
    {...await chunk(0,1,[entry(1)]),chunk_digest:'f'.repeat(64)},
    await chunk(0,1,[{...entry(1),outcome:'blocked',reflection:true}])])
    await assert.rejects(results.chunk(value));
  assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM run_observations').first()).n,0);
});
test('concurrent identical upload and lost seal ACK retry are evidence-only',async()=>{
  const {results,db}=await setup();await results.claim(binding);
  const value=await chunk(0,1,[entry(1)]);
  assert.equal((await Promise.all([results.chunk(value),results.chunk(value)])).length,2);
  const result=seal(1,1,value.chunk_digest);await results.result(result);
  assert.equal((await results.result(result)).duplicate,true);
  assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM run_observations').first()).n,1);
  assert.equal((await results.claim(binding).catch(()=>null)),null);
});

test('chunk inserted after seal validation invalidates zero seal atomically',async()=>{
  const {results,db,run}=await setup();await results.claim(binding);
  const value=await chunk(0,1,[entry(1)]);
  const prepare=db.prepare.bind(db);let fired=false;
  db.prepare=sql=>{
    const statement=prepare(sql);
    if(!sql.includes("SET phase='SEALED'"))return statement;
    return {...statement,bind:(...args)=>{
      const bound=statement.bind(...args);return {...bound,run:async()=>{
        if(!fired){fired=true;await results.chunk(value);}
        return bound.run();
      }};
    }};
  };
  await assert.rejects(results.result(seal(0,0,GENESIS)),{message:'RESULT_INCOMPLETE'});
  assert.equal((await request(db)).phase,'CLAIMED');
  run.status='completed';run.conclusion='success';
  await assert.rejects(results.activate(await request(db)),{message:'ACTIVATE_PHASE'});
  await results.result(seal(1,1,value.chunk_digest));
  assert.equal((await results.activate(await request(db))).activated,true);
  assert.equal(JSON.parse((await request(db)).sealed_result).entry_count,1);
});

test('zero seal committed before chunk batch excludes every unsealed observation',async()=>{
  const {results,db,run}=await setup();await results.claim(binding);
  const batch=db.batch.bind(db);let fired=false;
  db.batch=async queries=>{
    if(!fired){fired=true;await results.result(seal(0,0,GENESIS));}
    return batch(queries);
  };
  await assert.rejects(results.chunk(await chunk(0,1,[entry(1)])),{message:'CHUNK_NOT_STORED'});
  assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM run_observations').first()).n,0);
  run.status='completed';run.conclusion='success';
  assert.equal((await results.activate(await request(db))).activated,true);
  assert.equal(JSON.parse((await request(db)).sealed_result).entry_count,0);
});

test('storage rollback remains retryable; committed ACK loss duplicates identical evidence',async()=>{
  for(const ackLoss of [false,true]){
    const {results,db}=await setup();await results.claim(binding);
    const value=await chunk(0,1,[entry(1)]);const batch=db.batch.bind(db);let fired=false;
    db.batch=async queries=>{
      if(fired)return batch(queries);fired=true;
      if(ackLoss){await batch(queries);throw new Error('synthetic ACK loss');}
      return batch([...queries,{_execute:()=>{throw new Error('synthetic storage rollback');}}]);
    };
    if(ackLoss)assert.equal((await results.chunk(value)).duplicate,true);
    else await assert.rejects(results.chunk(value),{message:'CHUNK_STORAGE_UNAVAILABLE',status:503});
    assert.equal((await request(db)).phase,'CLAIMED');
    assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM run_observations').first()).n,ackLoss?1:0);
    await results.chunk(value);
    const result=seal(1,1,value.chunk_digest);
    const replies=await Promise.all([results.result(result),results.result(result)]);
    assert.equal(replies.filter(reply=>!reply.duplicate).length,1);
    assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM run_observations').first()).n,1);
  }
});

test('durable cross-chunk key collision stays fenced rather than retryable storage error',async()=>{
  const {results,db}=await setup();await results.claim(binding);
  const first=await chunk(0,2,[entry(1)]);await results.chunk(first);
  await assert.rejects(results.chunk(await chunk(1,2,[{...entry(2),key:entry(1).key}],first.chunk_digest)),
    {message:'CHUNK_CONFLICT'});
  assert.equal((await request(db)).phase,'RESULT_FENCED');
  assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM run_observations').first()).n,1);
});

test('rollback recovery snapshot tolerates identical concurrent commit before or after its read',async()=>{
  for(const commitBeforeRead of [false,true]){
    const {results,db,env,run}=await setup();await results.claim(binding);
    const second=new RunResults(db,env,{getRun:async()=>structuredClone(run)},()=>100);
    const first=await chunk(0,2,[entry(1)]);const batch=db.batch.bind(db);let failed=false;
    db.batch=async queries=>{
      if(failed)return batch(queries);failed=true;
      return batch([...queries,{_execute:()=>{throw new Error('actual rollback');}}]);
    };
    const prepare=db.prepare.bind(db);let interleaved=false;
    db.prepare=sql=>{
      const statement=prepare(sql);
      if(!sql.includes('AS collision'))return statement;
      return {...statement,bind:(...args)=>{
        const bound=statement.bind(...args);return {...bound,first:async()=>{
          assert.equal(interleaved,false);interleaved=true;
          assert.equal((await prepare('SELECT COUNT(*) AS n FROM run_observations').first()).n,0);
          if(commitBeforeRead){await second.chunk(first);return bound.first();}
          const snapshot=await bound.first();await second.chunk(first);return snapshot;
        }};
      }};
    };
    if(commitBeforeRead)assert.equal((await results.chunk(first)).duplicate,true);
    else await assert.rejects(results.chunk(first),{message:'CHUNK_STORAGE_UNAVAILABLE',status:503});
    assert.equal((await request(db)).phase,'CLAIMED');
    assert.equal((await results.chunk(first)).duplicate,true);
    const last=await chunk(1,2,[entry(2)],first.chunk_digest);await results.chunk(last);
    await results.result(seal(2,2,last.chunk_digest));run.status='completed';run.conclusion='success';
    assert.equal((await results.activate(await request(db))).activated,true);
    assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM run_observations').first()).n,2);
    assert.equal((await db.prepare('SELECT request_id FROM request_slot').first()).request_id,null);
  }
});

test('rollback recovery still fences a different digest committed at the same index',async()=>{
  const {results,db,env,run}=await setup();await results.claim(binding);
  const second=new RunResults(db,env,{getRun:async()=>structuredClone(run)},()=>100);
  const first=await chunk(0,2,[entry(1)]);
  const different=await chunk(0,2,[{...entry(1),status:'백로그'}]);
  const batch=db.batch.bind(db);let failed=false;
  db.batch=async queries=>{
    if(failed)return batch(queries);failed=true;
    return batch([...queries,{_execute:()=>{throw new Error('actual rollback');}}]);
  };
  const prepare=db.prepare.bind(db);let interleaved=false;
  db.prepare=sql=>{
    const statement=prepare(sql);if(!sql.includes('AS collision'))return statement;
    return {...statement,bind:(...args)=>{
      const bound=statement.bind(...args);return {...bound,first:async()=>{
        assert.equal(interleaved,false);interleaved=true;await second.chunk(different);return bound.first();
      }};
    }};
  };
  await assert.rejects(results.chunk(first),{message:'CHUNK_CONFLICT'});
  assert.equal((await request(db)).phase,'RESULT_FENCED');
  assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM run_observations').first()).n,1);
  assert.equal((await db.prepare('SELECT request_id FROM request_slot').first()).request_id,id);
});
