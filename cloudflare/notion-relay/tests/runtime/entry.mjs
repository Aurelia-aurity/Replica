import worker from '../../src/worker.js';
export default {
  async fetch(request,env) {
    if(new URL(request.url).pathname==='/__local_outbound') {
      try {
        return await fetch('https://api.github.com/repos/Aurelia-aurity/Replica/actions/runs/123',
          {method:'GET',redirect:'manual',signal:AbortSignal.timeout(10000)});
      } catch(error) {return new Response(JSON.stringify({type:error.name,message:error.message}),{status:500});}
    }
    if(new URL(request.url).pathname==='/__local_tick') {
      await worker.scheduled({},env);
      return new Response('local tick finished');
    }
    return worker.fetch(request,env);
  }
};
