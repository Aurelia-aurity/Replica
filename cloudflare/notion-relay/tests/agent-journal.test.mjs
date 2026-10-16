import {test} from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID, createHmac} from 'node:crypto';
import {AgentJournal} from '../src/agent-journal.js';
import worker from '../src/worker.js';
import {canonical, sha256, GENERAL_OPTIONS, PROJECT_ID, STATUS_FIELD_ID,
  REPOSITORY_ID} from '../src/protocol.js';
import {fixture as localFixture} from './local-d1.mjs';

// SQLite adapter reproduces the D1 prepared-query and atomic-batch contract.
// Separate Miniflare/D1 integration is still required before final acceptance.
async function fixture(mode = 'ACTIVE') {
  const {db,env}=await localFixture(mode);
  return {db,env,journal:new AgentJournal(db,env,() => 1700000000)};
}
function prepare(target = '진행 중') {
  return {operation_id:randomUUID(), repository_id:REPOSITORY_ID, project_id:PROJECT_ID,
    item_id:'PVTI_synthetic_item', status_field_id:STATUS_FIELD_ID, issue_id:555,
    page_id:'11111111-1111-4111-8111-111111111111',
    sync_key:`gh:${REPOSITORY_ID}:issue:555`,
    expected_before:{field_id:STATUS_FIELD_ID, option_id:GENERAL_OPTIONS['준비 중'],
      value_id:'PVTSV_synthetic',updated_at:'2026-10-01T00:00:00Z'},
    target,source_fingerprint:'a'.repeat(64)};
}
function binding(op) { return {operation_id:op.operation_id,binding:op.binding}; }
function success(op, metadata, kind = 'mutation') {
  return {...binding(op),kind,evidence_digest:'b'.repeat(64),
    readback_stamp:{...metadata.expected_before,option_id:GENERAL_OPTIONS[metadata.target]},
    source_fingerprint:metadata.source_fingerprint};
}
async function signed(env,path,body,{timestamp,signature,key,raw} = {}) {
  raw ??= canonical(body); timestamp ??= String(Math.floor(Date.now()/1000));
  signature ??= createHmac('sha256',key ?? env.HELPER_REQUEST_KEY)
    .update(`replica-helper-v1\nPOST\n${path}\n${timestamp}\n`).update(raw).digest('hex');
  return worker.fetch(new Request(`https://relay.invalid${path}`, {method:'POST',body:raw,
    headers:{'content-type':'application/json','x-replica-timestamp':timestamp,
      'x-replica-signature':signature}}),env);
}
function scalar(db, sql) { return db.sqlite.prepare(sql).get(); }

test('prepare durably pins resource and only identical replay is idempotent', async () => {
  const {journal}=await fixture();const data=prepare();const op=await journal.prepare(data);
  assert.equal(op.state,'PREPARED');assert.deepEqual(await journal.prepare(data),op);
  await assert.rejects(journal.prepare({...data,target:'백로그'}),/OPERATION_CONTRADICTION/);
  await assert.rejects(journal.prepare(prepare()),/RESOURCE_RESERVED/);
});
test('concurrent fresh UUID reservations select exactly one owner', async () => {
  const {journal,db}=await fixture(); const values=await Promise.allSettled([
    journal.prepare(prepare()),journal.prepare(prepare())]);
  assert.equal(values.filter(x=>x.status==='fulfilled').length,1);
  assert.equal(scalar(db,'SELECT COUNT(*) AS n FROM agent_reservations WHERE active=1').n,1);
  assert.equal(scalar(db,'SELECT COUNT(*) AS n FROM agent_operations').n,1);
});
test('same UUID concurrent attempts produce only one fresh permit', async () => {
  const {journal}=await fixture();const op=await journal.prepare(prepare());
  const results=await Promise.all([journal.attempt(binding(op)),journal.attempt(binding(op))]);
  assert.equal(results.filter(x=>x.newly_authorized).length,1);
  assert.equal((await journal.attempt(binding(op))).newly_authorized,false);
  await assert.rejects(journal.prepare(prepare()),/RESOURCE_RESERVED/);
});
test('attempt vs abort cannot both win, including after response loss', async () => {
  for (const order of ['attempt','abort']) {
    const {journal,db}=await fixture();const op=await journal.prepare(prepare());
    const calls=order==='attempt'?[journal.attempt(binding(op)),journal.abort(binding(op))]:
      [journal.abort(binding(op)),journal.attempt(binding(op))];
    const results=await Promise.allSettled(calls);
    assert.equal(results.filter(x=>x.status==='fulfilled').length,1);
    const current=await journal.operation(op.operation_id);
    if (current.state==='ABORTED') {
      await assert.rejects(journal.attempt(binding(op)),/ATTEMPT_REFUSED/);
      assert.equal((await journal.prepare(prepare())).state,'PREPARED');
    } else {
      assert.equal(current.state,'ATTEMPTING');
      await assert.rejects(journal.abort(binding(op)),/ATTEMPT_CANNOT_ABORT/);
      await assert.rejects(journal.prepare(prepare()),/RESOURCE_RESERVED/);
    }
    assert.equal(scalar(db,'SELECT COUNT(*) AS n FROM receipts').n,0);
  }
});
test('stopped default refuses fresh prepare; abort is permitted in every mode', async () => {
  const {journal,env}=await fixture();const op=await journal.prepare(prepare());
  delete env.RELAY_MODE;
  await assert.rejects(journal.prepare(prepare()),/PREPARE_DISABLED/);
  await assert.rejects(journal.attempt(binding(op)),/ATTEMPT_DISABLED/);
  assert.equal((await journal.abort(binding(op))).state,'ABORTED');
  assert.equal((await journal.abort(binding(op))).state,'ABORTED');
});
test('draining may attempt reserved work but rejects new preparation', async () => {
  const {journal,env}=await fixture();const op=await journal.prepare(prepare());env.RELAY_MODE='DRAINING';
  assert.equal((await journal.attempt(binding(op))).newly_authorized,true);
  await assert.rejects(journal.prepare(prepare()),/PREPARE_DISABLED/);
});
test('ambiguous/rejected outcomes retain reservation with no receipt in every mode', async () => {
  for (const relayMode of ['ACTIVE','DRAINING','STOPPED']) {
    for (const outcome of ['unknown','rejected']) {
      const {journal,env,db}=await fixture();const op=await journal.prepare(prepare());
      await journal.attempt(binding(op));env.RELAY_MODE=relayMode;
      const body={...binding(op),outcome,evidence_digest:'c'.repeat(64),
        code:outcome==='unknown'?'ACK_LOST':'API_REJECTED'};
      const result=await journal.outcome(body);
      assert.equal(result.state,outcome==='unknown'?'UNKNOWN_FENCED':'REJECTED_FENCED');
      assert.deepEqual(await journal.outcome(body),result);
      await assert.rejects(journal.outcome({...body,evidence_digest:'d'.repeat(64)}),/OUTCOME_CONTRADICTION/);
      assert.equal(scalar(db,'SELECT COUNT(*) AS n FROM receipts').n,0);
      assert.equal(scalar(db,'SELECT generation FROM relay_meta').generation,0);
      env.RELAY_MODE='ACTIVE';await assert.rejects(journal.prepare(prepare()),/RESOURCE_RESERVED/);
      await assert.rejects(journal.abort(binding(op)),/ATTEMPT_CANNOT_ABORT/);
    }
  }
});
test('unknown fence cannot be promoted by a later matching target', async () => {
  const {journal}=await fixture();const data=prepare();const op=await journal.prepare(data);
  await journal.attempt(binding(op));await journal.outcome({...binding(op),outcome:'unknown',
    evidence_digest:'c'.repeat(64),code:'API_OUTCOME_UNKNOWN'});
  await assert.rejects(journal.request(success(op,data)),/SUCCESS_STATE/);
});
test('attempted success records one receipt atomically in all modes', async () => {
  for (const relayMode of ['ACTIVE','DRAINING','STOPPED']) {
    const {journal,env,db}=await fixture();const data=prepare();const op=await journal.prepare(data);
    await journal.attempt(binding(op));env.RELAY_MODE=relayMode;
    const body=success(op,data);
    const results=await Promise.all([journal.request(body),journal.request(body)]);
    assert.ok(results.every(r=>r.state==='CONFIRMED'&&r.sync_queued));
    assert.deepEqual(await journal.request(body),results[0]);
    assert.equal(scalar(db,'SELECT COUNT(*) AS n FROM receipts').n,1);
    assert.equal(scalar(db,'SELECT generation FROM relay_meta').generation,1);
    assert.equal(scalar(db,'SELECT active FROM agent_reservations').active,0);
    await assert.rejects(journal.request({...body,evidence_digest:'d'.repeat(64)}),/RESULT_CONTRADICTION/);
  }
});
test('stopped prepared noop preserves reservation until delivery or safe abort', async () => {
  const {journal,env,db}=await fixture();const data=prepare('준비 중');const op=await journal.prepare(data);
  env.RELAY_MODE='STOPPED';await assert.rejects(journal.request(success(op,data,'noop')),/NOOP_RECEIPT_DISABLED/);
  assert.equal((await journal.operation(op.operation_id)).state,'PREPARED');
  assert.equal(scalar(db,'SELECT active FROM agent_reservations').active,1);
  assert.equal(scalar(db,'SELECT COUNT(*) AS n FROM receipts').n,0);
  env.RELAY_MODE='DRAINING';assert.equal((await journal.request(success(op,data,'noop'))).state,'CONFIRMED_NOOP');
});
test('forged success target/source and no-op stamp are rejected before transition', async () => {
  const {journal}=await fixture();const data=prepare();const op=await journal.prepare(data);
  await assert.rejects(journal.request(success(op,data,'noop')),/NOOP_BINDING/);
  await journal.attempt(binding(op));const body=success(op,data);
  await assert.rejects(journal.request({...body,source_fingerprint:'d'.repeat(64)}),/SUCCESS_BINDING/);
  await assert.rejects(journal.request({...body,readback_stamp:{...body.readback_stamp,
    option_id:GENERAL_OPTIONS['백로그']}}),/SUCCESS_BINDING/);
});
test('exact raw-body auth covers method/path/timestamp, rejects workflow-key impersonation', async () => {
  const {env,db,journal}=await fixture();const body=prepare();const raw=canonical(body);
  const timestamp=String(Math.floor(Date.now()/1000));
  const sig=createHmac('sha256',env.HELPER_REQUEST_KEY)
    .update(`replica-helper-v1\nPOST\n/agent-prepare\n${timestamp}\n`).update(raw).digest('hex');
  assert.equal((await signed(env,'/agent-prepare',body,{timestamp,signature:sig,raw:raw+' '})).status,401);
  assert.equal((await signed(env,'/agent-abort',body,{timestamp,signature:sig,raw})).status,401);
  assert.equal((await signed(env,'/agent-prepare',body,{key:'synthetic-workflow-key-000000000000000'})).status,401);
  assert.equal((await signed(env,'/agent-prepare',body,{timestamp:'1000000000'})).status,401);
  assert.equal(scalar(db,'SELECT COUNT(*) AS n FROM agent_operations').n,0);
  await journal.prepare(body);
  assert.equal((await signed(env,'/agent-prepare',body)).status,200);
});
test('new authenticated helper binds fresh Notion page before reservation; existing recovery survives outage',async()=>{
  const {env,db}=await fixture();const body=prepare();
  Object.assign(env,{NOTION_READ_TOKEN:'synthetic-read-only-token',NOTION_KIND_PROPERTY_ID:'kind',
    NOTION_KEY_PROPERTY_ID:'key',NOTION_TASK_PROPERTY_ID:'task',NOTION_STATE_PROPERTY_ID:'state'});
  const rich=(id,raw)=>({id,type:'rich_text',rich_text:[{type:'text',text:{content:raw}}]});
  const page={object:'page',id:body.page_id,parent:{type:'data_source_id',
    data_source_id:'398c7ff6-9c66-43de-ba4d-3f2670b25a87'},in_trash:false,
    properties:{'종류':{id:'kind',type:'select',select:{name:'Issue'}},
      '작업 상태':{id:'task',type:'select',select:{name:'준비 중'}},
      '동기화 키':rich('key',body.sync_key),'동기화 내부 상태':rich('state','synthetic')}};
  const previous=globalThis.fetch;let reads=0;
  try {
    globalThis.fetch=async(url,options)=>{
      reads++;assert.equal(url,`https://api.notion.com/v1/pages/${body.page_id}`);
      assert.equal(options.redirect,'manual');return new Response(JSON.stringify(page));};
    page.properties['동기화 키']=rich('key','gh:1392442366:issue:556');
    assert.equal((await signed(env,'/agent-prepare',body)).status,409);
    assert.equal(scalar(db,'SELECT COUNT(*) AS n FROM agent_operations').n,0);
    page.properties['동기화 키']=rich('key',body.sync_key);
    assert.equal((await signed(env,'/agent-prepare',body)).status,200);assert.equal(reads,2);
    globalThis.fetch=async()=>{throw Error('synthetic outage');};
    assert.equal((await signed(env,'/agent-prepare',body)).status,200);assert.equal(reads,2);
  } finally {globalThis.fetch=previous;}
});
test('streaming body bound and strict identity/schema reject without journal writes', async () => {
  const {env,db}=await fixture();
  assert.equal((await signed(env,'/agent-prepare',null,{raw:' '.repeat(65537)})).status,413);
  for (const data of [{...prepare(),repository_id:123}, {...prepare(),target:'完了'},
    {...prepare(),target:'취소'}, {...prepare(),sync_key:'gh:123:issue:555'},
    {...prepare(),url:'https://attacker.invalid'}, {...prepare(),operation_id:'not-uuid'}]) {
    assert.equal((await signed(env,'/agent-prepare',data)).status,400);
  }
  assert.equal((await signed(env,'/unknown',{})).status,404);
  assert.equal(scalar(db,'SELECT COUNT(*) AS n FROM agent_operations').n,0);
});
test('database error response never leaks raw error/body or token', async () => {
  const {env}=await fixture();env.DB={prepare(){throw new Error('private-api-token-payload');}};
  const result=await signed(env,'/agent-prepare',prepare());
  assert.equal(result.status,503);assert.deepEqual(await result.json(),{error:'JOURNAL_UNAVAILABLE'});
});
