import {ProtocolError,object,requireValue,uuid,digest,nodeID,canonical,sha256,mode,
  REPOSITORY_ID,PROJECT_ID} from './protocol.js';

export const GENESIS = '0'.repeat(64);
export const WORKFLOW_PATH = '.github/workflows/notion-sync-request.yml';
export function runName(id,generation){return `replica-sync-request:${id}:${generation}`;}
function positive(value){requireValue(Number.isSafeInteger(value)&&value>0,'RUN_INTEGER');}
function binding(value){
  uuid(value.request_id);positive(value.generation);positive(value.run_id);
  requireValue(value.attempt===1,'RUN_ATTEMPT');
  requireValue(typeof value.sha==='string'&&/^[0-9a-f]{40}$/.test(value.sha),'RUN_SHA');
}
export function validateRun(run,request,env,runID){
  positive(Number(env.GITHUB_DISPATCH_ACTOR_ID));positive(Number(env.GITHUB_WORKFLOW_ID));
  requireValue(run?.id===runID&&run.repository?.id===REPOSITORY_ID&&
    run.repository.full_name==='Aurelia-aurity/Replica'&&run.head_repository?.id===REPOSITORY_ID&&
    run.event==='workflow_dispatch'&&run.head_branch==='main'&&
    run.head_sha===request.expected_sha&&run.run_attempt===1&&
    run.workflow_id===Number(env.GITHUB_WORKFLOW_ID)&&run.path===WORKFLOW_PATH&&
    run.display_title===runName(request.request_id,request.generation)&&
    run.actor?.id===Number(env.GITHUB_DISPATCH_ACTOR_ID)&&
    run.triggering_actor?.id===Number(env.GITHUB_DISPATCH_ACTOR_ID), 'RUN_BINDING');
  return run;
}
function observation(entry){
  object(entry,['page_id','key','repository_id','project_id','item_id','status',
    'state_sha256','outcome','kind','reflection']);
  uuid(entry.page_id);digest(entry.state_sha256);
  requireValue(entry.repository_id===REPOSITORY_ID&&entry.project_id===PROJECT_ID,'OBSERVATION_BINDING');
  requireValue(typeof entry.key==='string'&&/^gh:1392442366:issue:[1-9][0-9]*$/.test(entry.key),
    'OBSERVATION_KEY');
  if(entry.item_id!==null)nodeID(entry.item_id,'PVTI_');
  requireValue(entry.status===null||['백로그','준비 중','진행 중','검토 중','완료','취소'].includes(entry.status),
    'OBSERVATION_STATUS');
  requireValue(entry.kind==='echo_observation'&&['handled','blocked'].includes(entry.outcome)&&
    typeof entry.reflection==='boolean'&&(!entry.reflection||entry.outcome==='handled'),
    'OBSERVATION_OUTCOME');
}
export class RunResults {
  constructor(db,env,github,now=()=>Math.floor(Date.now()/1000)){
    this.db=db;this.env=env;this.github=github;this.now=now;
  }
  async checked(value){
    binding(value);
    const request=await this.db.prepare('SELECT * FROM sync_requests WHERE request_id=?')
      .bind(value.request_id).first();
    requireValue(request&&request.generation===value.generation&&request.expected_sha===value.sha&&
      (request.run_id===null||request.run_id===value.run_id),'REQUEST_BINDING');
    const run=validateRun(await this.github.getRun(value.run_id),request,this.env,value.run_id);
    return {request,run};
  }
  async claim(value){
    object(value,['request_id','generation','run_id','sha','attempt']);
    requireValue(mode(this.env)!=='STOPPED','CLAIM_DISABLED',503);
    const {request,run}=await this.checked(value);
    requireValue(['POSTING','DISPATCHED','DISPATCH_UNKNOWN','CLAIMED'].includes(request.phase)&&
      run.status!=='completed','CLAIM_PHASE');
    const receipts=JSON.parse(request.receipt_ids);
    requireValue(Array.isArray(receipts)&&receipts.length>0&&receipts.length<=10&&
      new Set(receipts).size===receipts.length,'CLAIM_RECEIPTS');
    const changed=await this.db.prepare(`UPDATE sync_requests SET phase='CLAIMED',run_id=?,
      claimed_at=?,updated_at=? WHERE request_id=? AND claimed_at IS NULL AND
      phase IN ('POSTING','DISPATCHED','DISPATCH_UNKNOWN') AND EXISTS
      (SELECT 1 FROM request_slot WHERE singleton=1 AND request_id=?)`)
      .bind(value.run_id,this.now(),this.now(),value.request_id,value.request_id).run();
    if(changed.meta.changes!==1){
      const current=await this.db.prepare('SELECT phase,run_id FROM sync_requests WHERE request_id=?')
        .bind(value.request_id).first();
      requireValue(current?.phase==='CLAIMED'&&current.run_id===value.run_id,'CLAIM_CONFLICT');
    }
    return {request_id:value.request_id,newly_authorized:changed.meta.changes===1,
      receipts,project_id:PROJECT_ID,repository_id:REPOSITORY_ID};
  }
  async fence(id,code){
    await this.db.prepare("UPDATE sync_requests SET phase='RESULT_FENCED',updated_at=? WHERE request_id=?")
      .bind(this.now(),id).run();
    throw new ProtocolError(code,409);
  }
  async chunk(value){
    object(value,['request_id','generation','run_id','sha','attempt','index','chunk_count',
      'previous_digest','chunk_digest','entries']);
    const {request}=await this.checked(value);
    requireValue(request.claimed_at!==null,'RUN_NOT_CLAIMED');
    requireValue(Number.isSafeInteger(value.index)&&value.index>=0&&
      Number.isSafeInteger(value.chunk_count)&&value.chunk_count>0&&value.chunk_count<=2000&&
      value.index<value.chunk_count,'CHUNK_INDEX');
    digest(value.previous_digest);digest(value.chunk_digest);
    requireValue(Array.isArray(value.entries)&&value.entries.length>0&&value.entries.length<=10,'CHUNK_SIZE');
    const ids=[];const keys=[];
    for(const entry of value.entries){observation(entry);ids.push(entry.page_id);keys.push(entry.key);}
    requireValue(new Set(ids).size===ids.length&&new Set(keys).size===keys.length&&
      ids.every((id,index)=>index===0||ids[index-1]<id),'CHUNK_ORDER');
    const {chunk_digest,...unsigned}=value;
    requireValue(await sha256(canonical(unsigned))===chunk_digest,'CHUNK_DIGEST');
    const prior=await this.db.prepare('SELECT * FROM result_chunks WHERE request_id=? AND chunk_index=?')
      .bind(value.request_id,value.index).first();
    if(prior){if(prior.chunk_digest!==value.chunk_digest)return this.fence(value.request_id,'CHUNK_CONTRADICTION');
      return {stored:true,duplicate:true};}
    requireValue(request.phase==='CLAIMED','CHUNK_PHASE');
    const existing=await this.db.prepare(`SELECT * FROM result_chunks WHERE request_id=? AND
      (chunk_index IN (?,?) OR chunk_count!=?)`).bind(value.request_id,value.index-1,
      value.index+1,value.chunk_count).all();
    const rows=existing.results;
    if(value.index===0&&value.previous_digest!==GENESIS)return this.fence(value.request_id,'CHUNK_CHAIN');
    for(const row of rows){
      if(row.chunk_count!==value.chunk_count||row.chunk_index===value.index-1&&
        (row.chunk_digest!==value.previous_digest||row.last_page>=ids[0])||
        row.chunk_index===value.index+1&&(row.previous_digest!==value.chunk_digest||
          ids.at(-1)>=row.first_page))return this.fence(value.request_id,'CHUNK_CHAIN');
    }
    try {
      await this.db.batch([
        this.db.prepare(`INSERT INTO result_chunks
          (request_id,chunk_index,chunk_count,entry_count,previous_digest,chunk_digest,first_page,last_page)
          SELECT ?,?,?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM sync_requests
          WHERE request_id=? AND phase='CLAIMED')`).bind(value.request_id,value.index,value.chunk_count,
          ids.length,value.previous_digest,value.chunk_digest,ids[0],ids.at(-1),value.request_id),
        ...value.entries.map(entry=>this.db.prepare(`INSERT INTO run_observations
          (request_id,page_id,sync_key,entry,chunk_index) SELECT ?,?,?,?,?
          WHERE EXISTS(SELECT 1 FROM result_chunks WHERE request_id=? AND chunk_index=? AND chunk_digest=?)`)
          .bind(value.request_id,entry.page_id,entry.key,canonical(entry),value.index,
            value.request_id,value.index,value.chunk_digest)),
      ]);
    } catch {
      // One SQL snapshot distinguishes an identical commit from conflicting
      // identities. Separate awaited reads could mistake an intervening exact
      // retry's observations for a cross-chunk collision.
      const recovery=await this.db.prepare(`SELECT
        (SELECT chunk_digest FROM result_chunks WHERE request_id=? AND chunk_index=?) AS chunk_digest,
        EXISTS(SELECT 1 FROM run_observations WHERE request_id=? AND
          (page_id IN (${ids.map(()=>'?').join(',')}) OR sync_key IN (${keys.map(()=>'?').join(',')}))) AS collision`)
        .bind(value.request_id,value.index,value.request_id,...ids,...keys).first();
      if(recovery.chunk_digest===value.chunk_digest)return {stored:true,duplicate:true};
      if(recovery.chunk_digest!==null||recovery.collision)
        return this.fence(value.request_id,'CHUNK_CONFLICT');
      throw new ProtocolError('CHUNK_STORAGE_UNAVAILABLE',503);
    }
    const stored=await this.db.prepare('SELECT chunk_digest FROM result_chunks WHERE request_id=? AND chunk_index=?')
      .bind(value.request_id,value.index).first();
    requireValue(stored?.chunk_digest===value.chunk_digest,'CHUNK_NOT_STORED',503);
    return {stored:true,duplicate:false};
  }
  async result(value){
    object(value,['request_id','generation','run_id','sha','attempt','chunk_count','entry_count',
      'final_digest','report_digest','scan_complete','failed']);
    const {request}=await this.checked(value);
    requireValue(request.claimed_at!==null,'RUN_NOT_CLAIMED');
    requireValue(Number.isSafeInteger(value.chunk_count)&&value.chunk_count>=0&&value.chunk_count<=2000&&
      Number.isSafeInteger(value.entry_count)&&value.entry_count>=0&&value.entry_count<=20000&&
      typeof value.scan_complete==='boolean'&&typeof value.failed==='boolean','RESULT_SHAPE');
    digest(value.final_digest);digest(value.report_digest);
    const seal=await sha256(canonical(value));
    if(request.seal_digest){if(request.seal_digest!==seal)return this.fence(value.request_id,'RESULT_CONTRADICTION');
      return {sealed:true,activated:request.phase==='ACTIVATED',duplicate:true};}
    requireValue(request.phase==='CLAIMED','RESULT_PHASE');
    const aggregate=await this.db.prepare(`SELECT COUNT(*) AS n,MIN(chunk_index) AS first,
      MAX(chunk_index) AS last,SUM(entry_count) AS entries,MIN(chunk_count) AS low,
      MAX(chunk_count) AS high FROM result_chunks WHERE request_id=?`).bind(value.request_id).first();
    requireValue(aggregate.n===value.chunk_count&&(value.chunk_count===0?
      value.entry_count===0&&value.final_digest===GENESIS:
      aggregate.first===0&&aggregate.last===value.chunk_count-1&&aggregate.entries===value.entry_count&&
      aggregate.low===value.chunk_count&&aggregate.high===value.chunk_count),'RESULT_INCOMPLETE');
    if(value.chunk_count){
      const last=await this.db.prepare('SELECT chunk_digest FROM result_chunks WHERE request_id=? AND chunk_index=?')
        .bind(value.request_id,value.chunk_count-1).first();
      requireValue(last.chunk_digest===value.final_digest,'RESULT_CHAIN');
      const invalid=await this.db.prepare(`SELECT a.chunk_index FROM result_chunks a LEFT JOIN result_chunks b
        ON a.request_id=b.request_id AND b.chunk_index=a.chunk_index-1 WHERE a.request_id=? AND
        ((a.chunk_index=0 AND a.previous_digest!=?) OR (a.chunk_index>0 AND
        (b.chunk_digest IS NULL OR a.previous_digest!=b.chunk_digest OR b.last_page>=a.first_page))) LIMIT 1`)
        .bind(value.request_id,GENESIS).first();
      requireValue(!invalid,'RESULT_CHAIN');
    }
    const changed=await this.db.prepare(`UPDATE sync_requests SET phase='SEALED',seal_digest=?,
      sealed_result=?,updated_at=? WHERE request_id=? AND phase='CLAIMED' AND seal_digest IS NULL
      AND (SELECT COUNT(*) FROM result_chunks WHERE request_id=sync_requests.request_id)=?
      AND COALESCE((SELECT SUM(entry_count) FROM result_chunks WHERE request_id=sync_requests.request_id),0)=?
      AND (SELECT COUNT(*) FROM run_observations WHERE request_id=sync_requests.request_id)=?
      AND NOT EXISTS(SELECT 1 FROM result_chunks a WHERE a.request_id=sync_requests.request_id AND
        (a.chunk_count!=? OR a.chunk_index<0 OR a.chunk_index>=? OR a.entry_count!=
        (SELECT COUNT(*) FROM run_observations o WHERE o.request_id=a.request_id AND o.chunk_index=a.chunk_index)))
      AND (?=0 OR (SELECT chunk_digest FROM result_chunks
        WHERE request_id=sync_requests.request_id AND chunk_index=?)=?)
      AND NOT EXISTS(SELECT 1 FROM result_chunks a LEFT JOIN result_chunks b
        ON a.request_id=b.request_id AND b.chunk_index=a.chunk_index-1
        WHERE a.request_id=sync_requests.request_id AND
        ((a.chunk_index=0 AND a.previous_digest!=?) OR (a.chunk_index>0 AND
        (b.chunk_digest IS NULL OR a.previous_digest!=b.chunk_digest OR b.last_page>=a.first_page))))`)
      .bind(seal,canonical(value),this.now(),value.request_id,value.chunk_count,value.entry_count,
        value.entry_count,value.chunk_count,value.chunk_count,value.chunk_count,value.chunk_count-1,
        value.final_digest,GENESIS).run();
    if(changed.meta.changes!==1){
      const current=await this.db.prepare('SELECT phase,seal_digest FROM sync_requests WHERE request_id=?')
        .bind(value.request_id).first();
      if(current?.seal_digest!==seal){
        if(current?.seal_digest)return this.fence(value.request_id,'RESULT_CONTRADICTION');
        requireValue(current?.phase==='CLAIMED','RESULT_PHASE');
        throw new ProtocolError('RESULT_INCOMPLETE',409);
      }
      return {sealed:true,activated:false,duplicate:true};
    }
    return {sealed:true,activated:false,duplicate:false};
  }
  async activate(request){
    requireValue(request.phase==='SEALED','ACTIVATE_PHASE');
    const run=validateRun(await this.github.getRun(request.run_id),request,this.env,request.run_id);
    if(run.status!=='completed')return {activated:false,pending:true};
    const result=JSON.parse(request.sealed_result);
    if(run.conclusion!=='success'||!result.scan_complete||result.failed){
      await this.db.prepare("UPDATE sync_requests SET phase='FAILED_FENCED',updated_at=? WHERE request_id=? AND phase='SEALED'")
        .bind(this.now(),request.request_id).run();
      return {activated:false,pending:false,fenced:true};
    }
    // Green alone is insufficient: complete seal + independent terminal binding.
    await this.db.batch([
      this.db.prepare("UPDATE sync_requests SET phase='ACTIVATED',updated_at=? WHERE request_id=? AND phase='SEALED'")
        .bind(this.now(),request.request_id),
      this.db.prepare(`UPDATE receipts SET state='scanned',reason_code='FULL_SCAN_COMPLETED'
        WHERE request_id=? AND state='assigned' AND EXISTS
        (SELECT 1 FROM sync_requests WHERE request_id=? AND phase='ACTIVATED')`)
        .bind(request.request_id,request.request_id),
      this.db.prepare(`UPDATE request_slot SET request_id=NULL WHERE singleton=1 AND request_id=?
        AND EXISTS(SELECT 1 FROM sync_requests WHERE request_id=? AND phase='ACTIVATED')`)
        .bind(request.request_id,request.request_id),
    ]);
    return {activated:true,pending:false};
  }
}
