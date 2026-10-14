import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHmac,randomUUID} from 'node:crypto';
import worker from '../src/worker.js';
import {fixture} from './local-d1.mjs';

const fakeToken='synthetic-notion-verification-token-0000000';
const pins={NOTION_WORKSPACE_ID:'22222222-2222-4222-8222-222222222222',
  NOTION_INTEGRATION_ID:'33333333-3333-4333-8333-333333333333',
  NOTION_SUBSCRIPTION_ID:'44444444-4444-4444-8444-444444444444',
  NOTION_TASK_PROPERTY_ID:'XGe%40',NOTION_VERIFICATION_TOKEN:fakeToken};
function event() {
  return {id:randomUUID(),timestamp:'2026-10-01T00:00:00.000Z',
    workspace_id:pins.NOTION_WORKSPACE_ID,workspace_name:'synthetic display name',
    integration_id:pins.NOTION_INTEGRATION_ID,subscription_id:pins.NOTION_SUBSCRIPTION_ID,
    type:'page.properties_updated',authors:[{id:randomUUID(),type:'person'}],
    accessible_by:[],attempt_number:1,entity:{id:'11111111-1111-4111-8111-111111111111',type:'page'},
    data:{parent:{id:randomUUID(),type:'space'},updated_properties:['XGe%40']}};
}
async function send(env,body,{signature,raw,path='/notion?setup=consumed-nonce'}={}) {
  raw??=JSON.stringify(body);
  signature??='sha256='+createHmac('sha256',fakeToken).update(raw).digest('hex');
  return worker.fetch(new Request('https://relay.invalid'+path,{method:'POST',body:raw,
    headers:{'content-type':'application/json','x-notion-signature':signature}}),{...env,...pins});
}
function count(db) {return db.sqlite.prepare('SELECT COUNT(*) AS n FROM receipts').get().n;}
function generation(db){return db.sqlite.prepare('SELECT generation FROM relay_meta').get().generation;}

test('receipt survives before acknowledgement and exact replay allocates one generation',async()=>{
  const {db,env}=await fixture();const body=event();
  const results=await Promise.all([send(env,body),send(env,body)]);
  assert.ok(results.every(x=>x.status===200));assert.equal(count(db),1);assert.equal(generation(db),1);
  const saved=db.sqlite.prepare('SELECT * FROM receipts').get();
  assert.equal(saved.state,'queued');assert.equal(saved.page_id,body.entity.id);
  assert.equal(saved.sync_key,null); // fresh authoritative page binding is a later drain step
  assert.ok(!saved.metadata.includes('synthetic display name'));
  assert.ok(!saved.metadata.includes(fakeToken));assert.ok(!saved.metadata.includes(body.authors[0].id));
});
test('legitimate retry delivery metadata/author changes are not semantic contradictions',async()=>{
  const {db,env}=await fixture();const body=event();assert.equal((await send(env,body)).status,200);
  const retry={...body,attempt_number:8,workspace_name:'new display name',
    authors:[{id:randomUUID(),type:'bot'},{id:randomUUID(),type:'person'}],
    data:{...body.data,updated_properties:['XGe@','XGe%40']}};
  const result=await send(env,retry);assert.equal(result.status,200);
  assert.equal((await result.json()).duplicate,true);assert.equal(count(db),1);assert.equal(generation(db),1);
});
test('stable event binding contradiction is fenced, never another receipt/generation',async()=>{
  const {db,env}=await fixture();const body=event();await send(env,body);
  const result=await send(env,{...body,timestamp:'2026-10-01T00:01:00Z'});
  assert.equal(result.status,409);assert.equal(count(db),1);assert.equal(generation(db),1);
  assert.equal(db.sqlite.prepare('SELECT state FROM receipts').get().state,'fenced');
});
test('raw signature is checked before malformed JSON and every delivery independently',async()=>{
  const {db,env}=await fixture();const body=event();const raw=JSON.stringify(body);
  const signature='sha256='+createHmac('sha256',fakeToken).update(raw).digest('hex');
  assert.equal((await send(env,null,{raw:'not-json',signature:'sha256='+'0'.repeat(64)})).status,401);
  assert.equal((await send(env,body,{raw:raw+' ',signature})).status,401);
  assert.equal(count(db),0);
});
test('configured event/property/UUID bindings reject untrusted signals without writes',async()=>{
  const {db,env}=await fixture();const body=event();
  for(const altered of [{...body,workspace_id:randomUUID()}, {...body,integration_id:randomUUID()},
    {...body,subscription_id:randomUUID()}, {...body,type:'page.content_updated'},
    {...body,entity:{...body.entity,type:'block'}}, {...body,entity:{id:'unbound',type:'page'}},
    {...body,data:{updated_properties:['other']}}, {...body,unexpected:'anything'}])
    assert.equal((await send(env,altered)).status,400);
  assert.equal(count(db),0);assert.equal(generation(db),0);
});
test('stopped/draining preserve existing receipt replay but reject new ingress',async()=>{
  const {db,env}=await fixture();const body=event();await send(env,body);
  for(const relayMode of ['STOPPED','DRAINING']) {
    env.RELAY_MODE=relayMode;
    assert.equal((await send(env,body)).status,200);
    assert.equal((await send(env,event())).status,503);
  }
  assert.equal(count(db),1);assert.equal(generation(db),1);
});
test('database failure never acknowledges undurable event',async()=>{
  const {env}=await fixture();env.DB={prepare(){throw new Error('private token response payload');}};
  const result=await send(env,event());assert.equal(result.status,503);
  assert.deepEqual(await result.json(),{error:'JOURNAL_UNAVAILABLE'});
});
test('bootstrap stores ciphertext only and never turns the first candidate into runtime trust',async()=>{
  const {db,env}=await fixture('STOPPED');
  const keys=await crypto.subtle.generateKey({name:'RSA-OAEP',modulusLength:2048,
    publicExponent:new Uint8Array([1,0,1]),hash:'SHA-256'},true,['encrypt','decrypt']);
  const publicJWK=await crypto.subtle.exportKey('jwk',keys.publicKey);
  Object.assign(env,{BOOTSTRAP_ENABLED:'true',BOOTSTRAP_NONCE:'a'.repeat(64),
    BOOTSTRAP_EXPIRES_AT:String(Math.floor(Date.now()/1000)+300),
    BOOTSTRAP_PUBLIC_JWK:JSON.stringify(publicJWK)});
  const body={verification_token:fakeToken};
  assert.equal((await send(env,body,{path:'/notion?setup='+'b'.repeat(64)})).status,401);
  const result=await send(env,body,{path:'/notion?setup='+'a'.repeat(64)});
  assert.equal(result.status,200);assert.equal((await result.json()).runtime_verified,false);
  const candidate=db.sqlite.prepare('SELECT * FROM bootstrap_candidate').get();
  assert.ok(!candidate.ciphertext.includes(fakeToken));
  const decrypted=await crypto.subtle.decrypt({name:'RSA-OAEP'},keys.privateKey,
    Uint8Array.from(Buffer.from(candidate.ciphertext,'base64')));
  assert.equal(new TextDecoder().decode(decrypted),fakeToken);
  assert.equal(count(db),0);assert.equal(generation(db),0);
  assert.equal((await send(env,body,{path:'/notion?setup='+'a'.repeat(64)})).status,409);
  env.RELAY_MODE='ACTIVE';assert.equal((await send(env,event())).status,503);
});
