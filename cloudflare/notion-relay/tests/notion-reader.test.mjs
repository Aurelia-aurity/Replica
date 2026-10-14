import test from 'node:test';
import assert from 'node:assert/strict';
import {currentPage,readCurrentPage,matchesCheckpoint} from '../src/notion-reader.js';
import {SOURCE_ID,sha256} from '../src/protocol.js';

const id='11111111-1111-4111-8111-111111111111';
const env={NOTION_READ_TOKEN:'synthetic-read-only-token-000',NOTION_KIND_PROPERTY_ID:'kind',
  NOTION_KEY_PROPERTY_ID:'key',NOTION_TASK_PROPERTY_ID:'t%3Ask',NOTION_STATE_PROPERTY_ID:'state'};
const rich=(id,raw)=>({id,type:'rich_text',rich_text:[{type:'text',text:{content:raw},plain_text:raw}]});
const select=(id,name)=>({id,type:'select',select:name===null?null:{name}});
function page(){return {object:'page',id,parent:{type:'data_source_id',data_source_id:SOURCE_ID},
  archived:false,in_trash:false,properties:{'종류':select('kind','Issue'),
    '동기화 키':rich('key','gh:1392442366:issue:555'),'작업 상태':select('t:sk','진행 중'),
    '동기화 내부 상태':rich('state','synthetic exact state text'),'메모':rich('memo','private-note')}};}
test('fixed current binding and exact stored-text digest without content leakage',async()=>{
  const value=await currentPage(page(),id,env);
  assert.equal(value.state_sha256,await sha256('synthetic exact state text'));
  assert.equal(value.issue_id,555);assert.equal(value.status,'진행 중');
  assert.ok(!JSON.stringify(value).includes('private-note'));
  assert.equal(matchesCheckpoint(value,value),true);
  assert.equal(matchesCheckpoint(value,{...value,status:'백로그'}),false);
  assert.equal(matchesCheckpoint(value,undefined),undefined);
});
test('latest arbitrary state cannot create a checkpoint',async()=>{
  const value=await currentPage(page(),id,env);
  assert.ok(!value.reflected);assert.ok(!value.outcome);
  assert.equal(matchesCheckpoint(value,{...value,state_sha256:'a'.repeat(64)}),false);
});
test('source, identity, archive, kind, key, property and shape failures stay untrusted',async()=>{
  const changes=[p=>p.parent.data_source_id='22222222-2222-4222-8222-222222222222',
    p=>p.id='22222222-2222-4222-8222-222222222222',p=>p.archived=true,p=>p.in_trash=true,
    p=>p.properties['종류'].select.name='PR',p=>p.properties['동기화 키']=rich('key','gh:1:issue:555'),
    p=>p.properties['작업 상태'].id='other',p=>p.properties['작업 상태'].select.name='unknown',
    p=>p.properties['동기화 내부 상태'].rich_text[0].type='mention',
    p=>p.properties['동기화 내부 상태'].rich_text[0].plain_text='different'];
  for(const change of changes){const p=page();change(p);await assert.rejects(currentPage(p,id,env));}
});
test('terminal/null display values supported without expanding general helper targets',async()=>{
  for(const value of ['취소','완료',null]){const p=page();p.properties['작업 상태']=select('t:sk',value);
    assert.equal((await currentPage(p,id,env)).status,value);}
});
test('reader uses fixed GET/source and blocks redirects, network/API/body errors',async()=>{
  let calls=0;
  const latest=await readCurrentPage(id,env,async(url,options)=>{
    calls++;assert.equal(url,`https://api.notion.com/v1/pages/${id}`);
    assert.equal(options.method,'GET');assert.equal(options.redirect,'manual');
    return new Response(JSON.stringify(page()),{status:200});});
  assert.equal(calls,1);assert.equal(latest.page_id,id);
  await assert.rejects(readCurrentPage(id,env,async()=>{throw Error('private-api-body');}),
    {message:'NOTION_READ_UNKNOWN'});
  await assert.rejects(readCurrentPage(id,env,async()=>new Response('private-api-body',{status:404})),
    {message:'NOTION_READ_UNAVAILABLE'});
  await assert.rejects(readCurrentPage(id,env,async(_url,options)=>{
    assert.equal(options.redirect,'manual');
    return new Response(null,{status:302,headers:{location:'https://untrusted.invalid'}});
  }),{message:'NOTION_READ_UNAVAILABLE'});
  await assert.rejects(readCurrentPage(id,env,async()=>new Response('x'.repeat(262145))),
    {message:'NOTION_RESPONSE_LIMIT'});
});
