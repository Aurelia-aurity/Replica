-- Run after weekend_integration.sql as postgres. Existing Auth identities are
-- only used as FK owners. No Auth/Storage data is changed. All rows roll back.
begin;
set local statement_timeout = '20s';
do $$
declare u uuid; other_user uuid; p uuid := gen_random_uuid(); s uuid := gen_random_uuid();
  tid uuid := gen_random_uuid(); retry_id uuid := gen_random_uuid(); request_id uuid := gen_random_uuid();
  t public.chat_turns; claimed public.chat_turns; f record;
begin
  select id into u from auth.users order by id limit 1;
  select id into other_user from auth.users where id <> u order by id limit 1;
  if u is null or other_user is null then raise exception 'Two Auth fixtures required'; end if;
  insert into public.personas(id,user_id,name) values(p,u,'synthetic integration rollback');
  insert into public.chat_sessions(id,user_id,persona_id,ai_notice_ack_at) values(s,u,p,now());
  perform public.create_chat_turn(u,s,tid,'text','synthetic question');
  select * into claimed from public.start_chat_stage_v2(u,tid,'llm','synthetic-model',false,request_id);
  if claimed.llm_request_id <> request_id or claimed.llm_attempt <> 1 then raise exception 'Request not associated'; end if;
  begin
    perform public.finish_chat_stage_v2(u,tid,'llm',1,true,'synthetic answer',10,null,20,'length',gen_random_uuid());
    raise exception 'FAIL: wrong request ID accepted';
  exception when sqlstate '55000' then null; end;
  begin
    perform public.finish_chat_stage_v2(other_user,tid,'llm',1,true,'synthetic answer',10,null,20,'length',request_id);
    raise exception 'FAIL: foreign owner accepted';
  exception when sqlstate 'P0002' then null; end;
  select * into t from public.finish_chat_stage_v2(u,tid,'llm',1,true,'synthetic answer',10,null,20,'length',request_id);
  if t.finish_reason <> 'length' or t.total_ms <> 20 or t.answer_status <> 'done'
  then raise exception 'Generation metadata missing'; end if;
  perform public.record_turn_total_ms(u,tid,'llm',1,20);
  begin
    perform public.record_turn_total_ms(u,tid,'llm',1,21);
    raise exception 'FAIL: timing overwrite accepted';
  exception when sqlstate '55000' then null; end;
  begin
    update public.chat_turns set finish_reason='stop' where id=tid;
    raise exception 'FAIL: completed metadata overwrite accepted';
  exception when sqlstate '55000' then null; end;
  perform public.start_chat_stage(u,tid,'tts');
  select * into t from public.finish_chat_stage_v2(u,tid,'tts',1,false,null,3,'tts_failed',25);
  if t.answer <> 'synthetic answer' or t.finish_reason <> 'length' or t.tts_status <> 'failed'
  then raise exception 'TTS damaged completed answer'; end if;
  begin
    perform public.record_turn_total_ms(u,tid,'llm',1,25);
    raise exception 'FAIL: earlier stage timing accepted';
  exception when sqlstate '55000' then null; end;

  perform public.create_chat_turn(u,s,retry_id,'text','retry synthetic question');
  begin
    perform public.start_chat_stage_v2(u,retry_id,'llm','synthetic-model',false,request_id);
    raise exception 'FAIL: reused request ID accepted';
  exception when unique_violation then null; end;
  select * into t from public.chat_turns where id=retry_id;
  if t.llm_attempt <> 0 then raise exception 'Conflicting claim did not roll back'; end if;
  select * into claimed from public.start_chat_stage_v2(u,retry_id,'llm','synthetic-model',false,gen_random_uuid());
  perform public.finish_chat_stage_v2(u,retry_id,'llm',1,false,null,2,'llm_failed',3,null,claimed.llm_request_id);
  select * into claimed from public.start_chat_stage_v2(u,retry_id,'llm','synthetic-model',false,gen_random_uuid());
  begin
    perform public.finish_chat_stage_v2(u,retry_id,'llm',1,true,'late',2,null,3,'stop',claimed.llm_request_id);
    raise exception 'FAIL: stale attempt accepted';
  exception when sqlstate '55000' then null; end;
  perform public.finish_chat_stage_v2(u,retry_id,'llm',2,true,'retry answer',2,null,null,'stop',claimed.llm_request_id);
  perform public.record_turn_total_ms(u,retry_id,'llm',2,12);
  for f in select oid from pg_proc where pronamespace='public'::regnamespace
      and proname in ('start_chat_stage_v2','finish_chat_stage_v2','record_turn_total_ms') loop
    if has_function_privilege('anon',f.oid,'EXECUTE') or has_function_privilege('authenticated',f.oid,'EXECUTE')
      or not has_function_privilege('service_role',f.oid,'EXECUTE')
    then raise exception 'RPC privilege leak'; end if;
  end loop;
end;
$$;
rollback;
