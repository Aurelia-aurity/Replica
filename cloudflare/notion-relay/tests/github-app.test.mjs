import test from 'node:test';
import assert from 'node:assert/strict';
import {generateKeyPairSync,verify} from 'node:crypto';
import {GitHubApp,appJWT} from '../src/github-app.js';
import {runName,WORKFLOW_PATH} from '../src/run-results.js';
import {REPOSITORY_ID} from '../src/protocol.js';

// Newly generated local synthetic key; no key is loaded from disk or printed.
const pair=generateKeyPairSync('rsa',{modulusLength:2048});
const now=1700000000;
const env={GITHUB_APP_CLIENT_ID:'Iv1.synthetic',GITHUB_INSTALLATION_ID:'1',
  GITHUB_APP_PRIVATE_KEY_PKCS8:pair.privateKey.export({type:'pkcs8',format:'pem'}),
  GITHUB_BOT_ACTOR_ID:'7',GITHUB_WORKFLOW_ID:'8'};
const request={request_id:'11111111-1111-4111-8111-111111111111',generation:1,expected_sha:'a'.repeat(40)};
function run(id=123){return {id,repository:{id:REPOSITORY_ID,full_name:'Aurelia-aurity/Replica'},
  head_repository:{id:REPOSITORY_ID},event:'workflow_dispatch',head_branch:'main',head_sha:request.expected_sha,
  run_attempt:1,workflow_id:8,path:WORKFLOW_PATH,display_title:runName(request.request_id,1),
  actor:{id:7},triggering_actor:{id:7},status:'queued',conclusion:null};}
function response(value,status=200){return new Response(JSON.stringify(value),{status});}
function transport(dispatchResponse){const calls=[];
  const fetcher=async(url,options)=>{
    calls.push({url,method:options.method,body:options.body});assert.equal(options.redirect,'manual');
    if(url.endsWith('/access_tokens')){
      assert.deepEqual(JSON.parse(options.body),{repository_ids:[REPOSITORY_ID],permissions:{actions:'write',metadata:'read'}});
      return response({token:'synthetic-installation-token-000',expires_at:new Date((now+3600)*1000).toISOString(),
        permissions:{actions:'write',metadata:'read'},repositories:[{id:REPOSITORY_ID}]},201);}
    if(url.endsWith('/dispatches')){
      assert.deepEqual(JSON.parse(options.body),{ref:'main',inputs:{request_id:request.request_id,generation:'1'}});
      return dispatchResponse();}
    if(url.endsWith('/actions/runs/123'))return response(run());
    if(url.endsWith('/Replica'))return response({id:REPOSITORY_ID,full_name:'Aurelia-aurity/Replica',default_branch:'main',private:false});
    if(url.endsWith('/git/ref/heads/main')){
      assert.equal(options.headers.Authorization,undefined);
      return response({ref:'refs/heads/main',object:{type:'commit',sha:request.expected_sha}});}
    if(url.endsWith('/notion-sync-request.yml'))return response({id:8,path:WORKFLOW_PATH,state:'active'});
    if(url.includes('/runs?'))return response({workflow_runs:[run()]});
    throw Error('unexpected fixed URL');
  };
  return {app:new GitHubApp(env,fetcher,()=>now),calls,fetcher};}
test('RS256 JWT has explicit issuer/clock window and valid local signature',async()=>{
  const token=await appJWT(env,now);const [header,body,signature]=token.split('.');
  assert.deepEqual(JSON.parse(Buffer.from(header,'base64url')),{alg:'RS256',typ:'JWT'});
  assert.deepEqual(JSON.parse(Buffer.from(body,'base64url')),{iat:now-60,exp:now+540,iss:'Iv1.synthetic'});
  assert.equal(verify('RSA-SHA256',Buffer.from(`${header}.${body}`),pair.publicKey,Buffer.from(signature,'base64url')),true);
});
test('exact repository main/workflow pin and token narrowed to repository Actions/Metadata only',async()=>{
  const {app,calls}=transport(()=>new Response(null,{status:204}));
  assert.equal(await app.mainSHA(),request.expected_sha);await app.workflow();
  assert.equal(calls.filter(value=>value.url.endsWith('/access_tokens')).length,1);
});
test('200 dispatch binds independent exact run and never fetches returned arbitrary URLs',async()=>{
  const {app,calls}=transport(()=>response({workflow_run_id:123,
    run_url:'https://api.github.com/repos/Aurelia-aurity/Replica/actions/runs/123',
    html_url:'https://github.com/Aurelia-aurity/Replica/actions/runs/123'}));
  assert.equal((await app.dispatch(request)).state,'DISPATCHED');
  assert.equal(calls.filter(value=>value.url.endsWith('/dispatches')).length,1);
  const bad=transport(()=>response({workflow_run_id:123,run_url:'https://evil.invalid/steal',html_url:'wrong'}));
  assert.equal((await bad.app.dispatch(request)).state,'DISPATCH_UNKNOWN');
  assert.ok(bad.calls.every(value=>value.url.startsWith('https://api.github.com/')));
});
test('204, response loss, 5xx and malformed success stay ambiguous with no automatic POST retry',async()=>{
  for(const send of [()=>new Response(null,{status:204}),()=>{throw Error('private body');},
    ()=>new Response('private body',{status:502}),()=>new Response('malformed',{status:200})]){
    const {app,calls}=transport(send);assert.equal((await app.dispatch(request)).state,'DISPATCH_UNKNOWN');
    assert.equal(calls.filter(value=>value.url.endsWith('/dispatches')).length,1);}
});
test('definitive unaccepted statuses do not become success',async()=>{
  for(const status of [401,403,404,422,429]){
    const {app}=transport(()=>new Response('unlogged API body',{status}));
    assert.equal((await app.dispatch(request)).state,'DISPATCH_REJECTED');}
});
test('redirect response is never followed or treated as dispatch acceptance',async()=>{
  const {app,calls}=transport(()=>new Response(null,{status:302,
    headers:{location:'https://untrusted.invalid'}}));
  assert.equal((await app.dispatch(request)).state,'DISPATCH_UNKNOWN');
  assert.equal(calls.filter(value=>value.url.endsWith('/dispatches')).length,1);
  assert.ok(calls.every(value=>value.url.startsWith('https://api.github.com/')));
});
test('exact bounded correlation selects no ambiguous duplicate and never reposts',async()=>{
  const {app,calls}=transport(()=>new Response(null,{status:204}));
  assert.equal((await app.correlate(request)).run_id,123);
  assert.equal(calls.filter(value=>value.method==='POST'&&value.url.endsWith('/dispatches')).length,0);
  const original=app.fetcher;
  app.fetcher=(url,options)=>url.includes('/runs?')?response({workflow_runs:[run(123),run(124)]}):original(url,options);
  assert.equal((await app.correlate(request)).state,'DISPATCH_UNKNOWN');
});
test('tokens with excessive permissions or wrong repository fail before dispatch',async()=>{
  for(const change of [value=>value.permissions.contents='read',value=>value.repositories[0].id=99]){
    const value={token:'synthetic-installation-token-000',expires_at:new Date((now+3600)*1000).toISOString(),
      permissions:{actions:'write',metadata:'read'},repositories:[{id:REPOSITORY_ID}]};change(value);
    let posts=0;const app=new GitHubApp(env,async(url)=>{posts++;return response(value,201);},()=>now);
    await assert.rejects(app.token());assert.equal(posts,1);}
});
