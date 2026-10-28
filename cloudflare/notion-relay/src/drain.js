import {ProtocolError,requireValue,mode,canonical} from './protocol.js';
import {readCurrentPage,matchesCheckpoint} from './notion-reader.js';
import {GitHubActions} from './github-actions.js';
import {RunResults,validateRun} from './run-results.js';

export class Drain {
  constructor(env,{github=new GitHubActions(env),readPage=id=>readCurrentPage(id,env),
    now=()=>Math.floor(Date.now()/1000),newID=()=>crypto.randomUUID()}={}){
    this.env=env;this.db=env.DB;this.github=github;this.readPage=readPage;
    this.now=now;this.newID=newID;this.results=new RunResults(this.db,env,github,now);
  }
  async state(receipt,state,reason,key=null,activatedRequest=null){
    const changed=await this.db.prepare(`UPDATE receipts SET state=?,reason_code=?,sync_key=COALESCE(?,sync_key)
      WHERE receipt_id=? AND state=? AND (? IS NULL OR EXISTS
      (SELECT 1 FROM sync_requests WHERE request_id=? AND phase='ACTIVATED'))`)
      .bind(state,reason,key,receipt.receipt_id,receipt.state,activatedRequest,activatedRequest).run();
    return changed.meta.changes===1&&state==='actionable';
  }
  async classify(receipt){
    let latest;
    try{latest=await this.readPage(receipt.page_id);}
    catch(error){
      if(error instanceof ProtocolError&&error.status<500)
        await this.state(receipt,'blocked',error.code);
      return;
    }
    if(receipt.source==='agent'&&latest.key!==receipt.sync_key)
      return this.state(receipt,'blocked','HELPER_PAGE_KEY_MISMATCH');
    const observation=await this.db.prepare(`SELECT o.entry,o.request_id FROM run_observations o JOIN sync_requests r
      ON r.request_id=o.request_id WHERE o.page_id=? AND r.phase='ACTIVATED'
      ORDER BY r.updated_at DESC,r.request_id DESC LIMIT 1`).bind(receipt.page_id).first();
    const checkpoint=observation?JSON.parse(observation.entry):null;
    if(receipt.state==='scanned'){
      const own=await this.db.prepare(`SELECT o.entry FROM run_observations o JOIN sync_requests r
        ON r.request_id=o.request_id WHERE o.request_id=? AND o.page_id=? AND r.phase='ACTIVATED'`)
        .bind(receipt.request_id,receipt.page_id).first();
      if(!own)return this.state(receipt,'blocked','NO_HANDLED_CHECKPOINT',latest.key);
      const handled=JSON.parse(own.entry);
      if(matchesCheckpoint(latest,handled))return this.state(receipt,
        handled.outcome==='blocked'?'blocked':handled.reflection?'reflected':'completed',
        handled.outcome==='blocked'?'ENGINE_HELD':handled.reflection?'STRICT_REFLECTION':'HANDLED_ECHO_ONLY',
        latest.key,receipt.request_id);
      return this.state(receipt,receipt.dispatch_count>=3?'fenced':'actionable',
        receipt.dispatch_count>=3?'RECONCILIATION_LIMIT':'LATE_CURRENT_CHANGE',latest.key,receipt.request_id);
    }
    if(receipt.source==='notion'&&matchesCheckpoint(latest,checkpoint))
      return this.state(receipt,'ignored_echo',checkpoint.outcome==='blocked'?'HELD_ECHO':'HANDLED_ECHO',
        latest.key,observation.request_id);
    return this.state(receipt,receipt.dispatch_count>=3?'fenced':'actionable',
      receipt.dispatch_count>=3?'RECONCILIATION_LIMIT':'LATEST_RECONCILIATION',latest.key);
  }
  async active(){
    return this.db.prepare(`SELECT r.* FROM request_slot s JOIN sync_requests r
      ON r.request_id=s.request_id WHERE s.singleton=1`).first();
  }
  async correlate(request){
    const found=await this.github.correlate(request);
    if(found.state==='DISPATCHED')await this.db.prepare(`UPDATE sync_requests SET phase='DISPATCHED',
      run_id=?,updated_at=? WHERE request_id=? AND claimed_at IS NULL AND phase IN ('POSTING','DISPATCH_UNKNOWN')`)
      .bind(found.run_id,this.now(),request.request_id).run();
  }
  async send(request){
    if(mode(this.env)!=='ACTIVE')return;
    const changed=await this.db.prepare(`UPDATE sync_requests SET phase='POSTING',updated_at=?
      WHERE request_id=? AND phase='PREPARING' AND EXISTS
      (SELECT 1 FROM request_slot WHERE singleton=1 AND request_id=?)`)
      .bind(this.now(),request.request_id,request.request_id).run();
    if(changed.meta.changes!==1)return;
    const outcome=await this.github.dispatch(request);
    await this.db.prepare(`UPDATE sync_requests SET phase=?,run_id=?,updated_at=? WHERE request_id=?
      AND phase='POSTING' AND claimed_at IS NULL`).bind(outcome.state,outcome.run_id,this.now(),request.request_id).run();
  }
  async tick(){
    // Existing terminal evidence is handled even when receipt/dispatch is stopped.
    const active=await this.active();
    if(active){
      if(active.phase==='SEALED')return this.results.activate(active);
      if(['POSTING','DISPATCH_UNKNOWN'].includes(active.phase)){
        await this.correlate(active);return {pending:true};}
      if(active.phase==='PREPARING'){
        if(mode(this.env)==='ACTIVE')await this.send(active);
        return {pending:true};}
      if(active.phase==='DISPATCHED'){
        const run=validateRun(await this.github.getRun(active.run_id),active,this.env,active.run_id);
        if(run.status==='completed'&&active.claimed_at===null){
          // The reviewed ordinary workflow cannot write without the one-time claim.
          await this.db.batch([
            this.db.prepare(`UPDATE sync_requests SET phase='UNCLAIMED_TERMINAL',updated_at=?
              WHERE request_id=? AND phase='DISPATCHED' AND claimed_at IS NULL`)
              .bind(this.now(),active.request_id),
            this.db.prepare(`UPDATE receipts SET state='queued',request_id=NULL,reason_code='UNCLAIMED_TERMINAL'
              WHERE request_id=? AND state='assigned' AND EXISTS(SELECT 1 FROM sync_requests
              WHERE request_id=? AND phase='UNCLAIMED_TERMINAL')`).bind(active.request_id,active.request_id),
            this.db.prepare(`UPDATE request_slot SET request_id=NULL WHERE singleton=1 AND request_id=?
              AND EXISTS(SELECT 1 FROM sync_requests WHERE request_id=? AND phase='UNCLAIMED_TERMINAL')`)
              .bind(active.request_id,active.request_id),
          ]);
        }
      }
      return {pending:true};
    }
    const currentMode=mode(this.env);
    // At most three fresh Notion reads and bounded indexed SQL per invocation.
    const queued=await this.db.prepare(`SELECT * FROM receipts WHERE state IN ('queued','scanned','actionable')
      ORDER BY generation LIMIT 3`).all();
    const inspectedIDs=[];
    for(const receipt of queued.results){
      if(await this.classify(receipt))inspectedIDs.push(receipt.receipt_id);
    }
    if(currentMode!=='ACTIVE')return {dispatch_disabled:true};
    if(!inspectedIDs.length)return {idle:true};
    const candidates=await this.db.prepare(`SELECT * FROM receipts WHERE state='actionable'
      AND dispatch_count<3 AND receipt_id IN (${inspectedIDs.map(()=>'?').join(',')})
      ORDER BY generation LIMIT 10`).bind(...inspectedIDs).all();
    if(!candidates.results.length)return {idle:true};
    requireValue((await this.db.prepare('SELECT COUNT(*) AS n FROM sync_requests').first()).n<1000,
      'REQUEST_JOURNAL_CAPACITY',503);
    // No POST can occur before repository/main/workflow reads and durable intent.
    const expectedSHA=await this.github.mainSHA();await this.github.workflow();
    const id=this.newID();const ids=candidates.results.map(row=>row.receipt_id);
    const generation=Math.max(...candidates.results.map(row=>row.generation));
    const placeholders=ids.map(()=>'?').join(',');
    await this.db.batch([
      this.db.prepare(`INSERT INTO sync_requests(request_id,generation,receipt_ids,expected_sha,phase,created_at,updated_at)
        SELECT ?,?,?,?,'PREPARING',?,? WHERE EXISTS(SELECT 1 FROM request_slot WHERE singleton=1 AND request_id IS NULL)
        AND (SELECT COUNT(*) FROM receipts WHERE receipt_id IN (${placeholders}) AND state='actionable')=?`)
        .bind(id,generation,canonical(ids),expectedSHA,this.now(),this.now(),...ids,ids.length),
      this.db.prepare(`UPDATE request_slot SET request_id=? WHERE singleton=1 AND request_id IS NULL
        AND EXISTS(SELECT 1 FROM sync_requests WHERE request_id=?)`).bind(id,id),
      this.db.prepare(`UPDATE receipts SET state='assigned',request_id=?,dispatch_count=dispatch_count+1
        WHERE receipt_id IN (${placeholders}) AND state='actionable' AND EXISTS
        (SELECT 1 FROM request_slot WHERE singleton=1 AND request_id=?)`).bind(id,...ids,id),
    ]);
    const request=await this.active();
    if(request?.request_id===id)await this.send(request);
    return {pending:Boolean(request)};
  }
}
