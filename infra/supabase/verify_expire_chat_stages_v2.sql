-- Run after expire_chat_stages_v2.sql. All synthetic rows and changes roll back.
begin;
set local statement_timeout = '20s';
do $$
declare u uuid; foreign_owner uuid; p uuid := gen_random_uuid(); s uuid := gen_random_uuid();
  tts_turn uuid := gen_random_uuid(); llm_turn uuid := gen_random_uuid(); old_tts_turn uuid := gen_random_uuid();
  t public.chat_turns; meta record;
begin
  select id into u from auth.users order by id limit 1;
  select id into foreign_owner from auth.users where id <> u order by id limit 1;
  if u is null or foreign_owner is null then raise exception 'Two Auth fixtures required'; end if;
  insert into public.personas(id,user_id,name) values(p,u,'synthetic TTS cutoff rollback');
  insert into public.chat_sessions(id,user_id,persona_id,ai_notice_ack_at) values(s,u,p,now());

  -- A TTS stage started 40 s ago, a TTS stage started 200 s ago and an LLM stage started 40 s ago.
  perform public.create_chat_turn(u,s,tts_turn,'text','synthetic tts question');
  perform public.start_chat_stage(u,tts_turn,'llm','synthetic-model');
  perform public.finish_chat_stage(u,tts_turn,'llm',1,true,'answer',1);
  perform public.start_chat_stage(u,tts_turn,'tts');
  update public.chat_turns set tts_started_at = clock_timestamp() - interval '40 seconds' where id = tts_turn;

  perform public.create_chat_turn(u,s,old_tts_turn,'text','synthetic old tts question');
  perform public.start_chat_stage(u,old_tts_turn,'llm','synthetic-model');
  perform public.finish_chat_stage(u,old_tts_turn,'llm',1,true,'answer',1);
  perform public.start_chat_stage(u,old_tts_turn,'tts');
  update public.chat_turns set tts_started_at = clock_timestamp() - interval '200 seconds' where id = old_tts_turn;

  perform public.create_chat_turn(u,s,llm_turn,'text','synthetic llm question');
  perform public.start_chat_stage(u,llm_turn,'llm','synthetic-model');
  update public.chat_turns set llm_started_at = clock_timestamp() - interval '40 seconds' where id = llm_turn;

  -- Separate TTS cutoff: the 30 s STT/LLM cutoff closes the LLM stage but not the 40 s TTS stage.
  perform public.expire_chat_stages_v2(u, clock_timestamp() - interval '30 seconds', clock_timestamp() - interval '180 seconds');
  select * into t from public.chat_turns where id = tts_turn;
  if t.tts_status is distinct from 'pending' then raise exception 'TTS stage closed by the short cutoff'; end if;
  select * into t from public.chat_turns where id = llm_turn;
  if t.answer_status is distinct from 'failed' or t.error_message is distinct from 'llm_timeout'
  then raise exception 'LLM stage was not closed by its cutoff'; end if;
  select * into t from public.chat_turns where id = old_tts_turn;
  if t.tts_status is distinct from 'failed' or t.error_message is distinct from 'tts_timeout'
  then raise exception 'TTS stage older than the TTS cutoff was not closed'; end if;

  -- The existing two-argument function is untouched: its TTS stage still uses the single cutoff.
  perform public.expire_chat_stages(u, clock_timestamp() - interval '30 seconds');
  select * into t from public.chat_turns where id = tts_turn;
  if t.tts_status is distinct from 'failed' or t.error_message is distinct from 'tts_timeout'
  then raise exception 'Two-argument function changed'; end if;

  -- A late result for the closed attempt is still rejected.
  begin
    perform public.finish_chat_stage(u,tts_turn,'tts',1,true,null,1);
    raise exception 'FAIL: late TTS result accepted';
  exception when sqlstate '55000' then null; end;

  -- Another user cannot close this user's stages.
  update public.chat_turns set tts_status = 'pending', tts_started_at = clock_timestamp() - interval '300 seconds'
    where id = tts_turn;
  perform public.expire_chat_stages_v2(foreign_owner, clock_timestamp(), clock_timestamp());
  select * into t from public.chat_turns where id = tts_turn;
  if t.tts_status is distinct from 'pending' then raise exception 'Foreign owner closed the stage'; end if;

  -- Invalid cutoffs: future or missing.
  begin
    perform public.expire_chat_stages_v2(u, clock_timestamp() - interval '30 seconds', clock_timestamp() + interval '1 day');
    raise exception 'FAIL: future TTS cutoff accepted';
  exception when sqlstate '22023' then null; end;
  begin
    perform public.expire_chat_stages_v2(u, clock_timestamp() + interval '1 day', clock_timestamp() - interval '180 seconds');
    raise exception 'FAIL: future cutoff accepted';
  exception when sqlstate '22023' then null; end;
  begin
    perform public.expire_chat_stages_v2(u, clock_timestamp() - interval '30 seconds', null);
    raise exception 'FAIL: missing TTS cutoff accepted';
  exception when sqlstate '22023' then null; end;

  -- Both functions exist, v2 has no default arguments (no overload ambiguity) and only service_role may run it.
  if to_regprocedure('public.expire_chat_stages(uuid,timestamptz)') is null
  then raise exception 'Two-argument function missing'; end if;
  select oid, prosecdef, pronargdefaults into meta from pg_proc
    where oid = 'public.expire_chat_stages_v2(uuid,timestamptz,timestamptz)'::regprocedure;
  if meta.pronargdefaults <> 0 then raise exception 'v2 has default arguments'; end if;
  if meta.prosecdef or has_function_privilege('anon',meta.oid,'EXECUTE')
    or has_function_privilege('authenticated',meta.oid,'EXECUTE')
    or not has_function_privilege('service_role',meta.oid,'EXECUTE')
  then raise exception 'Privileges changed'; end if;
end;
$$;
rollback;
