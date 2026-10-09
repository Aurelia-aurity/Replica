-- Run as postgres in a development project with at least two existing Auth users.
-- All synthetic rows are rolled back. No Auth or Storage rows are modified.
-- Identity sequence values can advance even though the transaction rolls back.
begin;
set local statement_timeout = '25s';
do $$
declare users uuid[]; u uuid; p uuid; s uuid; r uuid; v uuid; rev bigint; i integer;
begin
  select array_agg(id order by id) into users from (select id from auth.users order by id limit 2) x;
  if cardinality(users) <> 2 then raise exception 'Two existing Auth users required'; end if;
  for i in 1..2 loop
    u := users[i]; p := gen_random_uuid(); s := gen_random_uuid(); r := gen_random_uuid(); v := gen_random_uuid();
    perform set_config('replica.test.u' || i, u::text, true);
    perform set_config('replica.test.p' || i, p::text, true);
    perform set_config('replica.test.s' || i, s::text, true);
    perform set_config('replica.test.r' || i, r::text, true);
    perform set_config('replica.test.v' || i, v::text, true);
    insert into public.personas(id,user_id,name) values(p,u,'rollback verification');
    insert into public.chat_sessions(id,user_id,persona_id,ai_notice_ack_at) values(s,u,p,now());
    insert into public.records(id,user_id,persona_id,kind,original_name,object_path,mime_type,
      status,consent_at,consent_version)
      values(r,u,p,'text','verification.txt',u::text || '/' || r::text || '/original',
        'text/plain','ready',now(),'test-v1');
    insert into public.record_text_versions(id,user_id,persona_id,record_id,revision,content)
      values(v,u,p,r,1,'reviewed test text');
    perform public.confirm_record_text(u,v);
    insert into public.record_chunks(user_id,persona_id,text_version_id,chunk_no,content,
      embedding,embedding_model,embedding_dimensions)
      values(u,p,v,0,'test chunk','[1,0,0]','test-model',3),
        (u,p,v,0,'different model and dimension','[1,0]','other-model',2);
    select data_revision into rev from public.personas where id=p;
    perform public.publish_style_profile(u,p,rev,'{"tone":"test"}','test-llm',array[v]);
    insert into public.deletion_jobs(user_id,target_type,target_id,remaining_steps)
      values(u,'session',s,array['database']);
    perform public.create_chat_turn(u,s,gen_random_uuid(),'text','hello');
  end loop;
  -- Composite FK prevents attaching another user's persona to a session.
  begin
    insert into public.chat_sessions(user_id,persona_id)
      values(users[1],current_setting('replica.test.p2')::uuid);
    raise exception 'FAIL: cross-owner FK accepted';
  exception when foreign_key_violation then null; end;
end $$;
set constraints all immediate;
set constraints all deferred;

-- Exercise RLS as both users, including every application table.
set local role authenticated;
do $$
declare i integer; n integer; t text; u uuid; other_user uuid;
begin
  for i in 1..2 loop
    u := current_setting('replica.test.u' || i)::uuid;
    other_user := current_setting('replica.test.u' || (3-i))::uuid;
    perform set_config('request.jwt.claim.sub',u::text,true);
    perform set_config('request.jwt.claims',jsonb_build_object('sub',u,'role','authenticated')::text,true);
    foreach t in array array['personas','chat_sessions','chat_turns','records',
      'record_text_versions','style_profiles','profile_sources','record_chunks','deletion_jobs'] loop
      execute format('select count(*) from public.%I where user_id=$1',t) into n using other_user;
      if n <> 0 then raise exception 'FAIL: cross-user read of %',t; end if;
      execute format('select count(*) from public.%I where user_id=$1',t) into n using u;
      if n < 1 then raise exception 'FAIL: owner cannot read %',t; end if;
      if has_table_privilege(current_user,'public.' || t,'INSERT')
        or has_table_privilege(current_user,'public.' || t,'UPDATE')
        or has_table_privilege(current_user,'public.' || t,'DELETE')
      then raise exception 'FAIL: client has write privilege on %',t; end if;
    end loop;
    begin
      insert into public.personas(user_id,name) values(u,'forged');
      raise exception 'FAIL: client write accepted';
    exception when insufficient_privilege then null; end;
    begin
      perform public.create_chat_turn(u,current_setting('replica.test.s' || i)::uuid,gen_random_uuid(),'text','forged');
      raise exception 'FAIL: client can invoke server RPC';
    exception when insufficient_privilege then null; end;
  end loop;
end $$;
reset role;
set local role anon;
do $$ begin
  begin
    perform 1 from public.personas;
    raise exception 'FAIL: anonymous table read accepted';
  exception when insufficient_privilege then null; end;
end $$;
reset role;

-- Server role exercises actual database helper functions and their triggers.
set local role service_role;
do $$
declare u uuid := current_setting('replica.test.u1')::uuid;
  s uuid := current_setting('replica.test.s1')::uuid;
  p uuid := current_setting('replica.test.p1')::uuid;
  r uuid := current_setting('replica.test.r1')::uuid;
  v uuid := current_setting('replica.test.v1')::uuid;
  turn_id uuid := gen_random_uuid(); voice_id uuid := gen_random_uuid(); timeout_id uuid := gen_random_uuid();
  draft_id uuid := gen_random_uuid(); t public.chat_turns; rev bigint; n integer;
begin
  t := public.create_chat_turn(u,s,turn_id,'text','same payload');
  perform public.create_chat_turn(u,s,turn_id,'text','same payload');
  if (select count(*) from public.chat_turns where id=turn_id) <> 1
    then raise exception 'FAIL: duplicate request'; end if;
  begin
    perform public.create_chat_turn(u,s,turn_id,'text','different payload');
    raise exception 'FAIL: changed request payload accepted';
  exception when invalid_parameter_value then null; end;
  begin
    perform public.create_chat_turn(current_setting('replica.test.u2')::uuid,s,gen_random_uuid(),'text','foreign session');
    raise exception 'FAIL: foreign session accepted';
  exception when no_data_found then null; end;
  begin
    perform public.start_chat_stage(u,turn_id,'tts');
    raise exception 'FAIL: TTS before LLM accepted';
  exception when object_not_in_prerequisite_state then null; end;
  t := public.start_chat_stage(u,turn_id,'llm','test-llm',true);
  if t.llm_attempt <> 1 or t.tts_status is not null then raise exception 'FAIL: initial LLM claim'; end if;
  begin
    perform public.start_chat_stage(u,turn_id,'llm','test-llm');
    raise exception 'FAIL: duplicate LLM claim accepted';
  exception when object_not_in_prerequisite_state then null; end;
  perform public.finish_chat_stage(u,turn_id,'llm',1,false,null,10,'test_failure');
  t := public.start_chat_stage(u,turn_id,'llm','test-llm',true);
  if t.llm_attempt <> 2 then raise exception 'FAIL: retry counter'; end if;
  begin
    perform public.finish_chat_stage(u,turn_id,'llm',1,true,'late stale answer',10);
    raise exception 'FAIL: stale result accepted';
  exception when object_not_in_prerequisite_state then null; end;
  perform public.finish_chat_stage(u,turn_id,'llm',2,true,'valid answer',20);
  perform public.start_chat_stage(u,turn_id,'tts');
  t := public.finish_chat_stage(u,turn_id,'tts',1,false,null,30,'test_tts_failure');
  if t.answer <> 'valid answer' or t.answer_status <> 'done' or t.tts_status <> 'failed'
    then raise exception 'FAIL: TTS failure lost answer'; end if;
  perform public.start_chat_stage(u,turn_id,'tts');
  t := public.finish_chat_stage(u,turn_id,'tts',2,true,null,25);
  if t.tts_status <> 'ready' then raise exception 'FAIL: TTS retry'; end if;
  begin
    perform public.start_chat_stage(u,turn_id,'llm','test-llm');
    raise exception 'FAIL: completed LLM restarted';
  exception when object_not_in_prerequisite_state then null; end;
  begin
    update public.chat_turns set answer='overwritten' where id=turn_id;
    raise exception 'FAIL: completed answer overwritten';
  exception when check_violation then null; end;

  perform public.create_chat_turn(u,s,voice_id,'voice',null,repeat('a',64));
  begin
    perform public.start_chat_stage(u,voice_id,'llm','test-llm');
    raise exception 'FAIL: LLM before STT accepted';
  exception when object_not_in_prerequisite_state then null; end;
  perform public.start_chat_stage(u,voice_id,'stt');
  perform public.finish_chat_stage(u,voice_id,'stt',1,false,null,10,'test_stt_failure');
  perform public.start_chat_stage(u,voice_id,'stt');
  begin
    perform public.finish_chat_stage(u,voice_id,'stt',1,true,'late transcript',10);
    raise exception 'FAIL: stale STT accepted';
  exception when object_not_in_prerequisite_state then null; end;
  t := public.finish_chat_stage(u,voice_id,'stt',2,true,'reviewed question',10);
  if t.question_status <> 'done' then raise exception 'FAIL: STT retry'; end if;
  perform public.create_chat_turn(u,s,timeout_id,'text','timeout fixture');
  perform public.start_chat_stage(u,timeout_id,'llm','test-llm');
  update public.chat_turns set llm_started_at='1900-01-01Z' where id=timeout_id;
  n := public.expire_chat_stages(u,'1901-01-01Z');
  if n <> 1 then raise exception 'FAIL: timeout count'; end if;
  begin
    perform public.finish_chat_stage(u,timeout_id,'llm',1,true,'late result',10);
    raise exception 'FAIL: timed-out result accepted';
  exception when object_not_in_prerequisite_state then null; end;

  -- Version approval, provenance and vector model/dimension isolation.
  insert into public.record_text_versions(id,user_id,persona_id,record_id,revision,content)
    values(draft_id,u,p,r,2,'new reviewed content');
  begin
    insert into public.record_chunks(user_id,persona_id,text_version_id,chunk_no,content,
      embedding,embedding_model,embedding_dimensions)
      values(u,p,draft_id,0,'unconfirmed','[1,0,0]','test-model',3);
    raise exception 'FAIL: unconfirmed chunk accepted';
  exception when check_violation then null; end;
  begin
    update public.record_text_versions set content='silent edit' where id=v;
    raise exception 'FAIL: confirmed content editable';
  exception when check_violation then null; end;
  select count(*) into n from public.search_record_chunks(u,p,'test-model','[1,0,0]');
  if n <> 1 then raise exception 'FAIL: vector filter'; end if;
  select count(*) into n from public.search_record_chunks(u,p,'test-model','[1,0]');
  if n <> 0 then raise exception 'FAIL: dimension filter'; end if;
  select count(*) into n from public.search_record_chunks(current_setting('replica.test.u2')::uuid,p,'test-model','[1,0,0]');
  if n <> 0 then raise exception 'FAIL: foreign-owner search'; end if;
  select data_revision into rev from public.personas where id=p;
  begin
    update public.style_profiles set status='stale' where persona_id=p and status='ready';
    insert into public.style_profiles(user_id,persona_id,version,source_revision,content,model_name)
      values(u,p,999,rev,'{"tone":"missing provenance"}','test-llm');
    set constraints all immediate;
    raise exception 'FAIL: ready profile without provenance accepted';
  exception when check_violation then null; end;
  set constraints all deferred;
  begin
    perform public.publish_style_profile(u,p,rev,'{"tone":"bad"}','test-llm',array[draft_id]);
    raise exception 'FAIL: unconfirmed profile source accepted';
  exception when object_not_in_prerequisite_state then null; end;
  perform public.confirm_record_text(u,draft_id);
  perform public.confirm_record_text(u,draft_id); -- Idempotent.
  if (select count(*) from public.record_text_versions where record_id=r and status='confirmed') <> 1
    then raise exception 'FAIL: current text uniqueness'; end if;
  if exists(select 1 from public.style_profiles where persona_id=p and status='ready')
    then raise exception 'FAIL: previous profile still ready'; end if;
  if exists(select 1 from public.search_record_chunks(u,p,'test-model','[1,0,0]'))
    then raise exception 'FAIL: superseded text searchable'; end if;
  begin
    perform public.publish_style_profile(u,p,rev,'{"tone":"stale"}','test-llm',array[draft_id]);
    raise exception 'FAIL: old profile revision accepted';
  exception when object_not_in_prerequisite_state then null; end;
  select data_revision into rev from public.personas where id=p;
  perform public.publish_style_profile(u,p,rev,'{"tone":"new"}','test-llm',array[draft_id]);
  insert into public.record_chunks(user_id,persona_id,text_version_id,chunk_no,content,
    embedding,embedding_model,embedding_dimensions)
    values(u,p,draft_id,0,'confirmed new chunk','[1,0,0]','test-model',3);
  perform set_config('replica.test.draft',draft_id::text,true);
end $$;
set constraints all immediate;
set constraints all deferred;

do $$
declare u uuid := current_setting('replica.test.u1')::uuid;
  r uuid := current_setting('replica.test.r1')::uuid;
  j public.deletion_jobs; j2 public.deletion_jobs;
begin
  j := public.request_record_deletion(u,r);
  j2 := public.request_record_deletion(u,r);
  if j.id <> j2.id then raise exception 'FAIL: duplicate deletion job'; end if;
  if j.manifest->>'object_path' <> u::text || '/' || r::text || '/original'
    then raise exception 'FAIL: deletion manifest'; end if;
  perform set_config('replica.test.job',j.id::text,true);
  begin
    delete from public.records where id=r;
    raise exception 'FAIL: metadata deleted before Storage acknowledgement';
  exception when object_not_in_prerequisite_state then null; end;
  if exists(select 1 from public.search_record_chunks(u,current_setting('replica.test.p1')::uuid,'test-model','[1,0,0]'))
    then raise exception 'FAIL: deleting record searchable'; end if;
end $$;
reset role;
set local role authenticated;
select set_config('request.jwt.claim.sub',current_setting('replica.test.u1'),true);
do $$ begin
  if exists(select 1 from public.records where id=current_setting('replica.test.r1')::uuid)
    or exists(select 1 from public.record_text_versions where record_id=current_setting('replica.test.r1')::uuid)
    or exists(select 1 from public.record_chunks where persona_id=current_setting('replica.test.p1')::uuid)
    or exists(select 1 from public.style_profiles where persona_id=current_setting('replica.test.p1')::uuid)
  then raise exception 'FAIL: deleting source or stale profile exposed'; end if;
  if not exists(select 1 from public.deletion_jobs where id=current_setting('replica.test.job')::uuid)
    then raise exception 'FAIL: owner cannot read deletion progress'; end if;
end $$;
reset role;
set local role service_role;
-- This fixture has no Storage object. Simulate worker acknowledgement only for it.
update public.deletion_jobs set remaining_steps=array['database']
  where id=current_setting('replica.test.job')::uuid;
delete from public.records where id=current_setting('replica.test.r1')::uuid;
do $$ begin
  if exists(select 1 from public.record_text_versions where record_id=current_setting('replica.test.r1')::uuid)
    or exists(select 1 from public.record_chunks where persona_id=current_setting('replica.test.p1')::uuid)
  then raise exception 'FAIL: derived data not cascaded'; end if;
  if not exists(select 1 from public.deletion_jobs where id=current_setting('replica.test.job')::uuid)
    then raise exception 'FAIL: deletion manifest lost'; end if;
end $$;
update public.deletion_jobs set status='completed',remaining_steps=array[]::text[],finished_at=now()
  where id=current_setting('replica.test.job')::uuid;
set constraints all immediate;
reset role;
rollback;
select 'PASS: RLS, server-only writes, stage retries, timeouts, approval, provenance, RAG filters, deletion lifecycle; test rows rolled back' as verification;
