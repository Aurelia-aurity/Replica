import {ProtocolError, boundedBody, verifyHelper, verifyWorkflow, parseJSON,
  prepareBody,requireValue,mode} from './protocol.js';
import {AgentJournal} from './agent-journal.js';
import {receiveNotion, bootstrap} from './notion-receiver.js';
import {GitHubActions} from './github-actions.js';
import {RunResults} from './run-results.js';
import {Drain} from './drain.js';
import {readCurrentPage} from './notion-reader.js';

const endpoints = Object.freeze({
  '/agent-prepare':'prepare', '/agent-attempt':'attempt', '/agent-abort':'abort',
  '/agent-outcome':'outcome', '/agent-request':'request',
});
const callbacks=Object.freeze({'/claim':'claim','/result/chunk':'chunk','/result':'result'});
function response(body, status = 200) {
  return new Response(JSON.stringify(body), {status, headers:{
    'content-type':'application/json', 'cache-control':'no-store'}});
}
export default {
  async fetch(request, env) {
    try {
      const path = new URL(request.url).pathname;
      if (request.method !== 'POST' || !Object.hasOwn(endpoints, path) &&
          !Object.hasOwn(callbacks,path) && path !== '/notion')
        throw new ProtocolError('ENDPOINT_UNAVAILABLE', 404);
      const bytes = await boundedBody(request);
      if (path === '/notion') {
        if (!env.DB) throw new ProtocolError('DATABASE_UNCONFIGURED', 503);
        return response(await (env.BOOTSTRAP_ENABLED === 'true' ? bootstrap : receiveNotion)(request,bytes,env));
      }
      if(Object.hasOwn(callbacks,path)){
        await verifyWorkflow(request,bytes,env);
        if(!env.DB)throw new ProtocolError('DATABASE_UNCONFIGURED',503);
        const results=new RunResults(env.DB,env,new GitHubActions(env));
        return response(await results[callbacks[path]](parseJSON(bytes)));
      }
      await verifyHelper(request, bytes, env.HELPER_REQUEST_KEY);
      if (!env.DB) throw new ProtocolError('DATABASE_UNCONFIGURED', 503);
      const journal = new AgentJournal(env.DB, env);
      const value=parseJSON(bytes);
      if(path==='/agent-prepare'){
        prepareBody(value);
        // Existing operation recovery must not depend on a later page edit or outage.
        // A new operation cannot reserve/authorize a foreign or unbound page.
        if(!await journal.operation(value.operation_id)){
          requireValue(mode(env)==='ACTIVE','PREPARE_DISABLED',503);
          const page=await readCurrentPage(value.page_id,env);
          requireValue(page.key===value.sync_key&&page.issue_id===value.issue_id,
            'HELPER_PAGE_KEY_MISMATCH',409);
        }
      }
      return response(await journal[endpoints[path]](value));
    } catch (error) {
      // Never emit exception messages, request bodies/URLs, signatures or API payloads.
      return response({error:error instanceof ProtocolError ? error.code : 'JOURNAL_UNAVAILABLE'},
        error instanceof ProtocolError ? error.status : 503);
    }
  },
  async scheduled(_event,env){
    try{
      if(!env.DB)throw new ProtocolError('DATABASE_UNCONFIGURED',503);
      await new Drain(env).tick();
    }catch(error){
      console.error(error instanceof ProtocolError?error.code:'DRAIN_UNAVAILABLE');
    }
  },
};
