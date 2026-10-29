-- Run after guard_llm_request_id.sql. All synthetic rows and changes roll back.
begin;
set local statement_timeout = '20s';
do $$
declare u uuid; foreign_owner uuid; p uuid := gen_random_uuid(); s uuid := gen_random_uuid();
  tid uuid; rid uuid; previous_rid uuid; t public.chat_turns; before_result public.chat_turns;
  success boolean; meta record;
begin
  select id into u from auth.users order by id limit 1;
  select id into foreign_owner from auth.users where id <> u order by id limit 1;
  if u is null or foreign_owner is null then raise exception 'Two Auth fixtures required'; end if;
  insert into public.personas(id,user_id,name) values(p,u,'synthetic ID guard rollback');
  insert into public.chat_sessions(id,user_id,persona_id,ai_notice_ack_at) values(s,u,p,now());

  foreach success in array array[true,false] loop
    tid := gen_random_uuid(); rid := gen_random_uuid();
    perform public.create_chat_turn(u,s,tid,'text','synthetic linked question');
    select * into before_result from public.start_chat_stage_v2(u,tid,'llm','synthetic-model',false,rid);
    begin
      perform public.finish_chat_stage(u,tid,'llm',1,success,case when success then 'unverified' end,1,'llm_failed');
      raise exception 'FAIL: linked legacy completion accepted';
    exception when sqlstate '55000' then null; end;
    select * into t from public.chat_turns where id=tid;
    if t is distinct from before_result then raise exception 'Rejected completion changed the row'; end if;

    begin
      perform public.finish_chat_stage_v2(u,tid,'llm',1,success,'unverified',1,null,null,null,null);
      raise exception 'FAIL: missing ID accepted by v2';
    exception when sqlstate '55000' then null; end;
    begin
      perform public.finish_chat_stage_v2(u,tid,'llm',1,success,'unverified',1,null,null,null,gen_random_uuid());
      raise exception 'FAIL: wrong ID accepted';
    exception when sqlstate '55000' then null; end;
    begin
      perform public.finish_chat_stage(foreign_owner,tid,'llm',1,success,'unverified',1);
      raise exception 'FAIL: foreign owner accepted';
    exception when sqlstate 'P0002' then null; end;

    select * into t from public.finish_chat_stage_v2(u,tid,'llm',1,success,
      case when success then 'verified answer' end,1,'llm_failed',2,case when success then 'stop' end,rid);
    if t.answer_status is distinct from (case when success then 'done' else 'failed' end)
       or t.llm_request_id is distinct from rid then raise exception 'Linked completion failed'; end if;
    if not success then
      previous_rid := rid; rid := gen_random_uuid();
      perform public.start_chat_stage_v2(u,tid,'llm','synthetic-model',false,rid);
      begin
        perform public.finish_chat_stage_v2(u,tid,'llm',2,true,'late',1,null,null,'stop',previous_rid);
        raise exception 'FAIL: previous ID accepted for new attempt';
      exception when sqlstate '55000' then null; end;
      perform public.finish_chat_stage_v2(u,tid,'llm',2,true,'retry answer',1,null,null,'stop',rid);
    end if;

    -- Old callers without an associated ID still complete success and failure.
    tid := gen_random_uuid();
    perform public.create_chat_turn(u,s,tid,'text','synthetic legacy question');
    perform public.start_chat_stage(u,tid,'llm','synthetic-model');
    select * into t from public.finish_chat_stage(u,tid,'llm',1,success,
      case when success then 'legacy answer' end,1,'llm_failed');
    if t.answer_status is distinct from (case when success then 'done' else 'failed' end)
       or t.llm_request_id is not null then raise exception 'Legacy compatibility broken'; end if;
    if success then
      perform public.start_chat_stage(u,tid,'tts');
      perform public.finish_chat_stage(u,tid,'tts',1,false,null,1,'tts_failed');
      perform public.start_chat_stage(u,tid,'tts');
      select * into t from public.finish_chat_stage(u,tid,'tts',2,true,null,1);
      if t.answer <> 'legacy answer' or t.tts_status <> 'ready' then raise exception 'TTS compatibility broken'; end if;
    end if;
  end loop;

  tid := gen_random_uuid();
  perform public.create_chat_turn(u,s,tid,'voice',null,repeat('a',64));
  perform public.start_chat_stage(u,tid,'stt');
  select * into t from public.finish_chat_stage(u,tid,'stt',1,true,'synthetic transcript',1);
  if t.question_status <> 'done' then raise exception 'STT compatibility broken'; end if;
  select oid, prosecdef into meta from pg_proc
    where oid='public.finish_chat_stage(uuid,uuid,text,integer,boolean,text,integer,text)'::regprocedure;
  if meta.prosecdef or has_function_privilege('anon',meta.oid,'EXECUTE')
    or has_function_privilege('authenticated',meta.oid,'EXECUTE')
    or not has_function_privilege('service_role',meta.oid,'EXECUTE')
  then raise exception 'Completion privileges changed'; end if;
end;
$$;
rollback;
