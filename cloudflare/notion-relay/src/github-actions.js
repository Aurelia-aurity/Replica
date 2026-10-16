import {ProtocolError,requireValue,REPOSITORY_ID,uuid} from './protocol.js';
import {WORKFLOW_PATH,runName,validateRun} from './run-results.js';

const REPO='/repos/Aurelia-aurity/Replica';
const API='https://api.github.com';
function positive(value){requireValue(Number.isSafeInteger(Number(value))&&Number(value)>0,'GITHUB_CONFIGURATION',503);}
async function json(response){
  const reader=response.body?.getReader();requireValue(reader,'GITHUB_RESPONSE_BODY',503);
  const parts=[];let length=0;
  try {for(;;){const {done,value}=await reader.read();if(done)break;length+=value.length;
    if(length>262144){await reader.cancel();throw new ProtocolError('GITHUB_RESPONSE_LIMIT',503);}parts.push(value);}}
  finally{reader.releaseLock();}
  const bytes=new Uint8Array(length);let offset=0;for(const part of parts){bytes.set(part,offset);offset+=part.length;}
  try{return JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));}
  catch{throw new ProtocolError('GITHUB_RESPONSE_SHAPE',503);}
}
export class GitHubActions {
  constructor(env,fetcher=(...args)=>fetch(...args)){
    this.env=env;this.fetcher=fetcher;this.cached=null;
  }
  async http(method,path,token,body){
    requireValue(path.startsWith('/')&&!path.includes('..'),'GITHUB_FIXED_PATH',503);
    try{return await this.fetcher(`${API}${path}`,{method,redirect:'manual',signal:AbortSignal.timeout(10000),
      headers:{'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2026-03-10',
        'User-Agent':'replica-notion-relay','Content-Type':'application/json',
        ...(token?{'Authorization':`Bearer ${token}`}:{})},
      ...(body===undefined?{}:{body:JSON.stringify(body)})});}
    catch{throw new ProtocolError('GITHUB_TRANSPORT_UNKNOWN',503);}
  }
  async token(){
    const token=this.env.GITHUB_DISPATCH_TOKEN,actor=this.env.GITHUB_DISPATCH_ACTOR_ID;
    requireValue(typeof token==='string'&&/^[A-Za-z0-9_]{20,255}$/.test(token),
      'DISPATCH_TOKEN_CONFIGURATION',503);
    requireValue(/^[1-9][0-9]*$/.test(String(actor))&&Number.isSafeInteger(Number(actor)),
      'DISPATCH_ACTOR_CONFIGURATION',503);
    if(this.cached?.token===token&&this.cached.actor===Number(actor))return token;
    const response=await this.http('GET','/user',token);
    requireValue(response.status===200,'DISPATCH_IDENTITY_UNAVAILABLE',503);
    const user=await json(response);
    requireValue(user?.id===Number(actor)&&user.type==='User','DISPATCH_IDENTITY_BINDING',503);
    this.cached={token,actor:user.id};return token;
  }
  async get(path){
    const response=await this.http('GET',path,await this.token());
    requireValue(response.status===200,'GITHUB_READ_UNAVAILABLE',503);return json(response);
  }
  async repository(){
    const repository=await this.get(REPO);
    requireValue(repository.id===REPOSITORY_ID&&repository.full_name==='Aurelia-aurity/Replica'&&
      repository.default_branch==='main'&&repository.private===false&&
      repository.permissions?.push===true,'REPOSITORY_BINDING',503);
  }
  async mainSHA(){
    await this.repository();
    // Public ref read deliberately carries no token.
    // Private-repository support is outside this scope.
    const response=await this.http('GET',`${REPO}/git/ref/heads/main`,null);
    requireValue(response.status===200,'MAIN_READ_UNAVAILABLE',503);
    const ref=await json(response);
    requireValue(ref.ref==='refs/heads/main'&&ref.object?.type==='commit'&&
      /^[0-9a-f]{40}$/.test(ref.object.sha),'MAIN_BINDING',503);
    return ref.object.sha;
  }
  async workflow(){
    const workflow=await this.get(`${REPO}/actions/workflows/notion-sync-request.yml`);
    requireValue(workflow.id===Number(this.env.GITHUB_WORKFLOW_ID)&&workflow.path===WORKFLOW_PATH&&
      workflow.state==='active','WORKFLOW_BINDING',503);
  }
  async getRun(id){positive(id);return this.get(`${REPO}/actions/runs/${id}`);}
  async dispatch(request){
    uuid(request.request_id);
    requireValue(Number.isSafeInteger(request.generation)&&request.generation>0,'DISPATCH_GENERATION');
    // Callers persist POSTING before entering this method. Never retry this POST.
    let response;
    try {
      // User identity and repository push access do not prove Actions write scope.
      await this.repository();
      response=await this.http('POST',`${REPO}/actions/workflows/notion-sync-request.yml/dispatches`,
      await this.token(),{ref:'main',inputs:{request_id:request.request_id,generation:String(request.generation)}});}
    catch{return {state:'DISPATCH_UNKNOWN',run_id:null,reason:'TRANSPORT_UNKNOWN'};}
    if(response.status===204)return {state:'DISPATCH_UNKNOWN',run_id:null,reason:'ACK_WITHOUT_RUN'};
    if(response.status===200){
      try{
        const body=await json(response);positive(body.workflow_run_id);
        requireValue(body.run_url===`${API}${REPO}/actions/runs/${body.workflow_run_id}`&&
          body.html_url===`https://github.com/Aurelia-aurity/Replica/actions/runs/${body.workflow_run_id}`,
          'DISPATCH_URL_BINDING');
        validateRun(await this.getRun(body.workflow_run_id),request,this.env,body.workflow_run_id);
        return {state:'DISPATCHED',run_id:body.workflow_run_id,reason:'RUN_VERIFIED'};
      }catch{return {state:'DISPATCH_UNKNOWN',run_id:null,reason:'RUN_UNVERIFIED'};}
    }
    if([401,403,404,422,429].includes(response.status))return {state:'DISPATCH_REJECTED',
      run_id:null,reason:'DEFINITIVE_REJECTION'};
    return {state:'DISPATCH_UNKNOWN',run_id:null,reason:'RESPONSE_UNKNOWN'};
  }
  async correlate(request){
    const matches=[];
    // Bounded exact-title search; no match within this window is inconclusive.
    for(let page=1;page<=2;page++){
      const value=await this.get(`${REPO}/actions/workflows/notion-sync-request.yml/runs?branch=main&event=workflow_dispatch&per_page=20&page=${page}`);
      requireValue(Array.isArray(value.workflow_runs)&&value.workflow_runs.length<=20,'RUN_LIST_SHAPE',503);
      for(const run of value.workflow_runs)if(run.display_title===runName(request.request_id,request.generation))
        matches.push(validateRun(run,request,this.env,run.id));
      if(value.workflow_runs.length<20)break;
    }
    if(matches.length!==1)return {state:'DISPATCH_UNKNOWN',run_id:null,reason:matches.length?'RUN_AMBIGUOUS':'RUN_NOT_FOUND'};
    const run=validateRun(await this.getRun(matches[0].id),request,this.env,matches[0].id);
    return {state:'DISPATCHED',run_id:run.id,reason:'RUN_CORRELATED'};
  }
}
