import {Miniflare,convertV4MiniflareOptions,Response} from 'miniflare';
import {readFile} from 'node:fs/promises';
import {createHmac,createHash,randomUUID,generateKeyPairSync} from 'node:crypto';
import assert from 'node:assert/strict';
import {canonical} from '../../src/protocol.js';
import {fileURLToPath} from 'node:url';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
const root=fileURLToPath(new URL('../..',import.meta.url));
const state=join(tmpdir(),`replica-results-state-${randomUUID()}`);
const helper='synthetic-helper-only-key-not-real-000000';
const workflow='synthetic-workflow-only-key-not-real-0000';
const sha='a'.repeat(40);let id=randomUUID(),runID=123,generation=1;
const binding={request_id:id,generation:1,run_id:123,sha,attempt:1};
let terminal=false,apiReads=0,tokenRequests=0;
const run=()=>({id:runID,repository:{id:1392442366,full_name:'Aurelia-aurity/Replica'},
  head_repository:{id:1392442366},event:'workflow_dispatch',head_branch:'main',head_sha:sha,
  run_attempt:1,workflow_id:8,path:'.github/workflows/notion-sync-request.yml',
  display_title:`replica-sync-request:${id}:${generation}`,actor:{id:7},triggering_actor:{id:7},
  status:terminal?'completed':'in_progress',conclusion:terminal?'success':null});
const {privateKey}=generateKeyPairSync('rsa',{modulusLength:2048});
const mf=new Miniflare(convertV4MiniflareOptions({name:'replica-results-local',host:'127.0.0.1',
  port:0,cf:false,telemetry:{enabled:false},compatibilityDate:'2026-10-01',
  modulesRoot:root,modules:[
    {type:'ESModule',path:`${root}/tests/runtime/entry.mjs`},
    ...['worker','protocol','agent-journal','notion-receiver','notion-reader','run-results','github-app','drain']
      .map(name=>({type:'ESModule',path:`${root}/src/${name}.js`}))],
  resourcePersistencePath:state,d1Databases:{DB:'replica-results-local'},
  bindings:{RELAY_MODE:'ACTIVE',HELPER_REQUEST_KEY:helper,WORKFLOW_CALLBACK_KEY:workflow,
    GITHUB_BOT_ACTOR_ID:'7',GITHUB_WORKFLOW_ID:'8',GITHUB_APP_CLIENT_ID:'synthetic-local',
    GITHUB_INSTALLATION_ID:'99',GITHUB_APP_PRIVATE_KEY_PKCS8:privateKey.export({type:'pkcs8',format:'pem'})},
  outboundService:async request=>{
    const url=new URL(request.url);
    assert.equal(url.origin,'https://api.github.com');
    let body,status=200;
    if(url.pathname==='/app/installations/99/access_tokens'){
      assert.equal(request.method,'POST');
      assert.deepEqual(await request.json(),{repository_ids:[1392442366],permissions:{actions:'write',metadata:'read'}});
      tokenRequests++;status=201;body={token:'synthetic-local-installation-token-0000',
        expires_at:new Date(Date.now()+3500000).toISOString(),permissions:{actions:'write',metadata:'read'}};
    }else if(url.pathname===`/repos/Aurelia-aurity/Replica/actions/runs/${runID}`){
      assert.equal(request.method,'GET');apiReads++;body=run();
    }else throw new Error('UNEXPECTED_SYNTHETIC_OUTBOUND');
    return new Response(JSON.stringify(body),{status,headers:{'content-type':'application/json'}});
  }
}));
const digest=value=>createHash('sha256').update(canonical(value)).digest('hex');
async function post(path,body,{key=workflow,domain='replica-workflow-v1'}={}){
  const raw=JSON.stringify(body),ts=String(Math.floor(Date.now()/1000));
  const sig=createHmac('sha256',key).update(`${domain}\nPOST\n${path}\n${ts}\n`).update(raw).digest('hex');
  const r=await mf.dispatchFetch(`https://relay.invalid${path}`,{method:'POST',body:raw,
    headers:{'content-type':'application/json','x-replica-timestamp':ts,'x-replica-signature':sig}});
  return {status:r.status,body:await r.json()};
}
try {
  const db=await mf.getD1Database('DB');
  const outboundCheck=await mf.dispatchFetch('https://relay.invalid/__local_outbound');
  assert.equal(outboundCheck.status,200,await outboundCheck.text());
  apiReads=0;
  for(const name of ['0001_journal','0002_bootstrap','0003_requests']){
    const sql=await readFile(`${root}/migrations/${name}.sql`,'utf8');
    for(const statement of sql.replace(/^--.*$/gm,'').split(';').map(x=>x.trim()).filter(Boolean))
      await db.prepare(statement).run();
  }
  await db.prepare(`INSERT INTO sync_requests(request_id,generation,receipt_ids,expected_sha,phase,created_at,updated_at)
    VALUES(?,1,?,?,'DISPATCH_UNKNOWN',1,1)`).bind(id,JSON.stringify(['notion:synthetic']),sha).run();
  await db.prepare('UPDATE request_slot SET request_id=? WHERE singleton=1').bind(id).run();
  const forged=await post('/claim',binding,{key:helper,domain:'replica-helper-v1'});
  assert.notEqual(forged.status,200);assert.equal(apiReads,0);
  const claims=await Promise.all([post('/claim',binding),post('/claim',binding)]);
  assert.ok(claims.every(x=>x.status===200),JSON.stringify(claims));
  assert.equal(claims.filter(x=>x.body.newly_authorized).length,1);
  const entries=Array.from({length:23},(_,i)=>({
    page_id:`00000000-0000-4000-8000-${String(i+1).padStart(12,'0')}`,
    key:`gh:1392442366:issue:${i+1}`,repository_id:1392442366,project_id:'PVT_kwHOBda_2c4BmKKV',
    item_id:`PVTI_synthetic${i+1}`,status:'진행 중',state_sha256:'b'.repeat(64),
    outcome:i===22?'blocked':'handled',kind:'echo_observation',reflection:false}));
  const chunks=[];let previous='0'.repeat(64);
  for(let i=0;i<3;i++){
    const unsigned={...binding,index:i,chunk_count:3,previous_digest:previous,entries:entries.slice(i*10,i*10+10)};
    previous=digest(unsigned);chunks.push({...unsigned,chunk_digest:previous});
  }
  const seal={...binding,chunk_count:3,entry_count:23,final_digest:previous,report_digest:'c'.repeat(64),
    scan_complete:true,failed:false};
  for(const i of [2,0])assert.equal((await post('/result/chunk',chunks[i])).status,200);
  assert.equal((await post('/result',seal)).body.error,'RESULT_INCOMPLETE');
  assert.equal(await db.prepare('SELECT phase FROM sync_requests WHERE request_id=?').bind(id).first('phase'),'CLAIMED');
  assert.equal((await post('/result/chunk',chunks[1])).status,200);
  assert.equal((await post('/result/chunk',chunks[1])).body.duplicate,true);
  assert.equal((await post('/result',seal)).body.activated,false);
  assert.equal((await post('/result',seal)).body.duplicate,true);
  await mf.dispatchFetch('https://relay.invalid/__local_tick');
  assert.equal(await db.prepare('SELECT phase FROM sync_requests WHERE request_id=?').bind(id).first('phase'),'SEALED');
  terminal=true;await mf.dispatchFetch('https://relay.invalid/__local_tick');
  assert.equal(await db.prepare('SELECT phase FROM sync_requests WHERE request_id=?').bind(id).first('phase'),'ACTIVATED');
  assert.equal(await db.prepare('SELECT COUNT(*) AS n FROM run_observations').first('n'),23);
  assert.equal(await db.prepare('SELECT request_id FROM request_slot').first('request_id'),null);
  // Exercise the actual D1 batch rollback and single-snapshot recovery SQL.
  // A duplicate key across distinct chunks is a real durable contradiction.
  id=randomUUID();runID=456;generation=2;terminal=false;
  const collisionBinding={request_id:id,generation,run_id:runID,sha,attempt:1};
  await db.prepare(`INSERT INTO sync_requests(request_id,generation,receipt_ids,expected_sha,phase,created_at,updated_at)
    VALUES(?,2,?,?,'DISPATCH_UNKNOWN',1,1)`).bind(id,JSON.stringify(['notion:collision']),sha).run();
  await db.prepare('UPDATE request_slot SET request_id=? WHERE singleton=1').bind(id).run();
  assert.equal((await post('/claim',collisionBinding)).body.newly_authorized,true);
  const unsigned={...collisionBinding,index:0,chunk_count:2,previous_digest:'0'.repeat(64),entries:[entries[0]]};
  const first={...unsigned,chunk_digest:digest(unsigned)};
  assert.equal((await post('/result/chunk',first)).status,200);
  const lastUnsigned={...collisionBinding,index:1,chunk_count:2,previous_digest:first.chunk_digest,
    entries:[{...entries[1],key:entries[0].key}]};
  const conflict=await post('/result/chunk',{...lastUnsigned,chunk_digest:digest(lastUnsigned)});
  assert.equal(conflict.body.error,'CHUNK_CONFLICT');
  assert.equal(await db.prepare('SELECT phase FROM sync_requests WHERE request_id=?').bind(id).first('phase'),'RESULT_FENCED');
  assert.equal(await db.prepare('SELECT COUNT(*) AS n FROM result_chunks WHERE request_id=?').bind(id).first('n'),1);
  assert.equal(await db.prepare('SELECT COUNT(*) AS n FROM run_observations WHERE request_id=?').bind(id).first('n'),1);
  console.log(JSON.stringify({scope:'candidate Worker callbacks + scheduler on LOCAL Miniflare/D1',
    separateKeyRejection:true,mainRequestFreshClaims:1,collisionRequestFreshClaims:1,
    completeObservations:23,partialSealRejected:true,
    evidenceOnlyRetries:true,terminalRequired:true,actualD1RollbackConflict:true,apiReads,tokenRequests,statePath:state,
    upstream:'strict in-process synthetic GitHub service; no network/account/Project/Notion writes'}));
}catch(error){console.error(JSON.stringify({code:'LOCAL_RESULTS_PROBE_FAILED',type:error.name,message:error.message}));process.exitCode=1;}
finally{await mf.dispose();}
