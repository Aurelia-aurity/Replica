import test from 'node:test';
import assert from 'node:assert/strict';
import {GitHubActions} from '../src/github-actions.js';
import {runName,WORKFLOW_PATH} from '../src/run-results.js';
import {REPOSITORY_ID} from '../src/protocol.js';

const env={GITHUB_DISPATCH_TOKEN:'github_pat_synthetic_local_only_000000',
  GITHUB_DISPATCH_ACTOR_ID:'7',GITHUB_ACTOR_ID:'999',GITHUB_WORKFLOW_ID:'8'};
const request={request_id:'11111111-1111-4111-8111-111111111111',generation:1,expected_sha:'a'.repeat(40)};
function run(id=123){return {id,repository:{id:REPOSITORY_ID,full_name:'Aurelia-aurity/Replica'},
  head_repository:{id:REPOSITORY_ID},event:'workflow_dispatch',head_branch:'main',head_sha:request.expected_sha,
  run_attempt:1,workflow_id:8,path:WORKFLOW_PATH,display_title:runName(request.request_id,1),
  actor:{id:7},triggering_actor:{id:7},status:'queued',conclusion:null};}
function response(value,status=200){return new Response(JSON.stringify(value),{status});}
function transport(dispatchResponse,{identity={id:7,type:'User'},repository={},configuration={}}={}){const calls=[];
  const config={...env,...configuration};
  const fetcher=async(url,options)=>{
    calls.push({url,method:options.method,body:options.body});assert.equal(options.redirect,'manual');
    assert.ok(options.signal instanceof AbortSignal);
    assert.equal(options.headers.Authorization,`Bearer ${config.GITHUB_DISPATCH_TOKEN}`);
    if(url.endsWith('/user')){
      assert.equal(options.method,'GET');assert.equal(options.body,undefined);
      return response(identity);}
    if(url.endsWith('/dispatches')){
      assert.deepEqual(JSON.parse(options.body),{ref:'main',inputs:{request_id:request.request_id,generation:'1'}});
      return dispatchResponse();}
    if(url.endsWith('/actions/runs/123'))return response(run());
    if(url.endsWith('/Replica'))return response({id:REPOSITORY_ID,full_name:'Aurelia-aurity/Replica',
      default_branch:'main',private:false,permissions:{push:true},...repository});
    if(url.endsWith('/git/ref/heads/main')){
      return response({ref:'refs/heads/main',object:{type:'commit',sha:request.expected_sha}});}
    if(url.endsWith('/notion-sync-request.yml'))return response({id:8,path:WORKFLOW_PATH,state:'active'});
    if(url.includes('/runs?'))return response({workflow_runs:[run()]});
    throw Error('unexpected fixed URL');
  };
  return {app:new GitHubActions(config,fetcher),calls,fetcher};}
test('PAT verifies designated User once per instance with exact public main/workflow and push access',async()=>{
  const {app,calls}=transport(()=>new Response(null,{status:204}));
  assert.equal(await app.mainSHA(),request.expected_sha);await app.workflow();
  assert.equal(await app.token(),env.GITHUB_DISPATCH_TOKEN);
  assert.equal(calls.filter(value=>value.url.endsWith('/user')).length,1);
  const second=new GitHubActions(env,app.fetcher);await second.token();
  assert.equal(calls.filter(value=>value.url.endsWith('/user')).length,2);
  assert.ok(calls.every(value=>value.method==='GET'));
});
test('200 dispatch binds designated user actor and triggering_actor and never fetches arbitrary URLs',async()=>{
  const {app,calls}=transport(()=>response({workflow_run_id:123,
    run_url:'https://api.github.com/repos/Aurelia-aurity/Replica/actions/runs/123',
    html_url:'https://github.com/Aurelia-aurity/Replica/actions/runs/123'}));
  assert.equal((await app.dispatch(request)).state,'DISPATCHED');
  assert.equal(calls.filter(value=>value.url.endsWith('/dispatches')).length,1);
  assert.deepEqual(calls.slice(0,3).map(value=>[value.method,new URL(value.url).pathname]),[
    ['GET','/user'],['GET','/repos/Aurelia-aurity/Replica'],
    ['POST','/repos/Aurelia-aurity/Replica/actions/workflows/notion-sync-request.yml/dispatches']]);
  assert.ok(calls.every(value=>!value.url.includes('/app/')&&!value.url.includes('/access_tokens')));
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
test('wrong PAT identity or non-User identity cannot dispatch and failed verification is not cached',async()=>{
  for(const identity of [{id:9,type:'User'},{id:7,type:'Bot'},{id:'7',type:'User'},null]){
    const {app,calls}=transport(()=>assert.fail('dispatch forbidden'),{identity});
    await assert.rejects(app.token(),{message:'DISPATCH_IDENTITY_BINDING'});
    assert.equal(app.cached,null);
    assert.equal((await app.dispatch(request)).state,'DISPATCH_UNKNOWN');
    assert.equal(calls.length,2);assert.ok(calls.every(value=>value.url.endsWith('/user')));
    const fetcher=app.fetcher;
    app.fetcher=(url,options)=>url.endsWith('/user')?response({id:7,type:'User'}):fetcher(url,options);
    assert.equal(await app.token(),env.GITHUB_DISPATCH_TOKEN);
  }
});
test('missing or malformed PAT and actor configuration fail without HTTP or credential disclosure',async()=>{
  for(const token of [undefined,null,'','short','a'.repeat(256),'github_pat_invalid value',
    'github_pat_bad\\r\\nAuthorization: secret']){
    const {app,calls}=transport(()=>assert.fail('dispatch forbidden'),{configuration:{GITHUB_DISPATCH_TOKEN:token}});
    await assert.rejects(app.token(),{message:'DISPATCH_TOKEN_CONFIGURATION'});
    assert.deepEqual(await app.dispatch(request),{state:'DISPATCH_UNKNOWN',run_id:null,reason:'TRANSPORT_UNKNOWN'});
    assert.equal(calls.length,0);
  }
  for(const actor of [undefined,'','0','-1','1.5','7e0','9007199254740992']){
    const {app,calls}=transport(()=>assert.fail('dispatch forbidden'),{configuration:{GITHUB_DISPATCH_ACTOR_ID:actor}});
    await assert.rejects(app.token(),{message:'DISPATCH_ACTOR_CONFIGURATION'});assert.equal(calls.length,0);
  }
});
test('wrong repository identity, private/default branch or absent push access prevent dispatch',async()=>{
  for(const repository of [{id:99},{full_name:'other/Replica'},{private:true},{default_branch:'feature'},
    {permissions:{push:false}},{permissions:{}},{permissions:{push:'true'}}]){
    const {app,calls}=transport(()=>assert.fail('dispatch forbidden'),{repository});
    await assert.rejects(app.mainSHA(),{message:'REPOSITORY_BINDING'});
    assert.equal((await app.dispatch(request)).state,'DISPATCH_UNKNOWN');
    assert.ok(calls.every(value=>value.method==='GET'));
    assert.equal(calls.filter(value=>value.url.endsWith('/user')).length,1);
  }
});
test('identity failure responses, redirects, oversized JSON and transport errors never leak PAT',async()=>{
  for(const reply of [()=>new Response(env.GITHUB_DISPATCH_TOKEN,{status:401}),
    ()=>new Response(null,{status:302,headers:{location:'https://untrusted.invalid'}}),
    ()=>response({id:7,type:'User',padding:'a'.repeat(262144)}),
    ()=>new Response('malformed',{status:200}),()=>{throw Error(env.GITHUB_DISPATCH_TOKEN);}]){
    const calls=[];const app=new GitHubActions(env,async(url,options)=>{
      calls.push(url);assert.equal(options.redirect,'manual');return reply();
    });
    await assert.rejects(app.token(),error=>{
      assert.ok(!error.message.includes(env.GITHUB_DISPATCH_TOKEN));return true;
    });
    assert.equal(app.cached,null);assert.deepEqual(calls,['https://api.github.com/user']);
  }
});
test('changed token or designated actor requires fresh identity verification',async()=>{
  const {app,calls}=transport(()=>new Response(null,{status:204}));await app.token();
  app.env={...env,GITHUB_DISPATCH_ACTOR_ID:'9'};
  await assert.rejects(app.token(),{message:'DISPATCH_IDENTITY_BINDING'});
  app.env={...env,GITHUB_DISPATCH_TOKEN:'github_pat_other_synthetic_token_000000'};
  app.fetcher=async(url)=>{calls.push({url});return response({id:7,type:'User'});};
  assert.equal(await app.token(),app.env.GITHUB_DISPATCH_TOKEN);
  assert.equal(calls.length,3);
});
