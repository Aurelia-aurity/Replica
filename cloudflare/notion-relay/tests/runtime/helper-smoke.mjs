import {Miniflare,convertV4MiniflareOptions,Response} from 'miniflare';
import {readFile} from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const root=fileURLToPath(new URL('../..',import.meta.url));
const state=join(tmpdir(),`replica-helper-runtime-${randomUUID()}`);
let pageReads=0;
const mf=new Miniflare(convertV4MiniflareOptions({name:'replica-helper-runtime',host:'127.0.0.1',port:0,
  cf:false,telemetry:{enabled:false},modulesRoot:root,compatibilityDate:'2026-10-01',
  modules:['worker','protocol','agent-journal','notion-receiver','notion-reader','run-results','github-actions','drain']
    .map(name=>({type:'ESModule',path:`${root}/src/${name}.js`})),
  resourcePersistencePath:state,d1Databases:{DB:'replica-helper-runtime'},bindings:{
    RELAY_MODE:'ACTIVE',HELPER_REQUEST_KEY:'synthetic-helper-key-not-a-real-key-0000',
    NOTION_READ_TOKEN:'synthetic-read-only-token',NOTION_KIND_PROPERTY_ID:'kind',
    NOTION_KEY_PROPERTY_ID:'key',NOTION_TASK_PROPERTY_ID:'task',NOTION_STATE_PROPERTY_ID:'state'},
  outboundService:async request=>{
    const url=new URL(request.url);assert.equal(url.origin,'https://api.notion.com');
    assert.equal(request.method,'GET');assert.ok(url.pathname.startsWith('/v1/pages/'));
    const id=url.pathname.slice('/v1/pages/'.length),n=Number(id.slice(-12));
    assert.ok(n>=1&&n<=7);pageReads++;
    const rich=(id,raw)=>({id,type:'rich_text',rich_text:[{type:'text',text:{content:raw}}]});
    return new Response(JSON.stringify({object:'page',id,parent:{type:'data_source_id',
      data_source_id:'398c7ff6-9c66-43de-ba4d-3f2670b25a87'},in_trash:false,
      properties:{'종류':{id:'kind',type:'select',select:{name:'Issue'}},
        '작업 상태':{id:'task',type:'select',select:{name:'백로그'}},
        '동기화 키':rich('key',`gh:1392442366:issue:${555+n}`),
        '동기화 내부 상태':rich('state','synthetic')} }));
  }
}));
try {
  const db=await mf.getD1Database('DB');
  for(const name of ['0001_journal','0002_bootstrap','0003_requests']){
    const sql=await readFile(`${root}/migrations/${name}.sql`,'utf8');
    for(const statement of sql.replace(/^--.*$/gm,'').split(';').map(x=>x.trim()).filter(Boolean))
      await db.prepare(statement).run();
  }
  const address=(await mf.ready).toString();
  const output=await new Promise((resolve,reject)=>{
    const child=spawn('python3',['-B',`${root}/tests/runtime/helper-client.py`,address],
      {env:{PATH:process.env.PATH},stdio:['ignore','pipe','pipe']});
    let stdout='',stderr='';child.stdout.on('data',chunk=>stdout+=chunk);child.stderr.on('data',chunk=>stderr+=chunk);
    child.on('error',reject);child.on('exit',code=>code===0?resolve(JSON.parse(stdout)):
      reject(new Error(`SYNTHETIC_HELPER_FAILED ${stderr}`)));
  });
  assert.equal(output.results.length,7);assert.equal(pageReads,7);
  const expected={confirmed:'CONFIRMED',attempt_ack_lost:'UNKNOWN_FENCED',result_ack_lost:'CONFIRMED',
    readback_mismatch:'UNKNOWN_FENCED',known_rejection:'REJECTED_FENCED',stale_prewrite:'ABORTED',noop:'CONFIRMED_NOOP'};
  for(const result of output.results){
    const stored=await db.prepare('SELECT state FROM agent_operations WHERE operation_id=?').bind(result.operation_id).first('state');
    assert.equal(stored,expected[result.case]);
  }
  assert.equal(await db.prepare('SELECT COUNT(*) AS n FROM receipts').first('n'),3);
  assert.equal(await db.prepare('SELECT COUNT(*) AS n FROM agent_reservations WHERE active=1').first('n'),3);
  console.log(JSON.stringify({...output,pageReads,receipts:3,preservedUnknownOrRejectedReservations:3,
    statePath:state,upstream:'strict synthetic Notion GET; synthetic Project adapter; no external network/accounts'}));
}catch(error){console.error(JSON.stringify({code:'LOCAL_HELPER_PROBE_FAILED',type:error.name,message:error.message}));process.exitCode=1;}
finally{await mf.dispose();}
