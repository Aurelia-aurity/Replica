-- Additive deployment SQL after replica_db_redesign. Not a CLI migration filename.
-- Preserve the original RPC signatures for PR #45 callers.
alter table public.chat_turns
  add column llm_request_id uuid,
  add column finish_reason text,
  add constraint chat_turns_finish_reason_valid check
    (finish_reason is null or (finish_reason in ('stop', 'length') and answer_status is not distinct from 'done'));
create unique index chat_turns_llm_request_id_unique on public.chat_turns(llm_request_id)
  where llm_request_id is not null;

create function replica_private.guard_integration_metadata() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if new.llm_attempt <> old.llm_attempt then
    new.llm_request_id := null;
    new.finish_reason := null;
  elsif old.answer_status = 'done' and
    (new.finish_reason is distinct from old.finish_reason or new.llm_request_id is distinct from old.llm_request_id)
  then raise exception 'Completed generation metadata is immutable' using errcode = '55000'; end if;
  return new;
end;
$$;
create trigger guard_integration_metadata before update on public.chat_turns
  for each row execute function replica_private.guard_integration_metadata();

create function public.start_chat_stage_v2(p_user_id uuid, p_turn_id uuid, p_stage text,
  p_model_name text default null, p_with_audio boolean default false, p_request_id uuid default null)
returns public.chat_turns language plpgsql security invoker set search_path = '' as $$
declare t public.chat_turns;
begin
  if p_stage <> 'llm' or p_request_id is null
  then raise exception 'LLM request ID required' using errcode = '22023'; end if;
  -- Claim and association are one transaction; a duplicate UUID rolls back the claim.
  select * into t from public.start_chat_stage(p_user_id, p_turn_id, p_stage, p_model_name, p_with_audio);
  update public.chat_turns set llm_request_id = p_request_id where id = t.id returning * into t;
  return t;
end;
$$;

create function public.finish_chat_stage_v2(p_user_id uuid, p_turn_id uuid, p_stage text,
  p_attempt integer, p_ok boolean, p_text text default null, p_ms integer default null,
  p_error_code text default null, p_total_ms integer default null, p_finish_reason text default null,
  p_request_id uuid default null)
returns public.chat_turns language plpgsql security invoker set search_path = '' as $$
declare t public.chat_turns;
begin
  if p_total_ms < 0 or (p_finish_reason is not null and
    (p_stage is distinct from 'llm' or p_ok is distinct from true or p_finish_reason not in ('stop', 'length')))
    or (p_request_id is not null and p_stage is distinct from 'llm')
  then raise exception 'Invalid generation metadata' using errcode = '22023'; end if;
  select c.* into t from public.chat_turns c join public.chat_sessions s on s.id = c.session_id
    join public.personas p on p.id = s.persona_id
    where c.id = p_turn_id and c.user_id = p_user_id and p.status = 'active' for update of c;
  if not found then raise exception 'Turn unavailable' using errcode = 'P0002'; end if;
  if p_stage = 'llm' and p_request_id is distinct from t.llm_request_id
  then raise exception 'Request association changed' using errcode = '55000'; end if;
  if p_attempt is null or p_attempt < 1 or p_ok is null or p_ms < 0
    or (p_ok and p_stage in ('stt','llm') and (p_text is null or length(btrim(p_text)) = 0))
  then raise exception 'Invalid stage result' using errcode = '22023'; end if;
  if p_stage = 'stt' and t.question_status = 'pending' and t.stt_attempt = p_attempt then
    update public.chat_turns set question_status = case when p_ok then 'done' else 'failed' end,
      question = case when p_ok then p_text end, stt_ms = p_ms, total_ms = p_total_ms,
      error_message = case when p_ok then null else left(coalesce(p_error_code,'stt_failed'),256) end
      where id = t.id returning * into t;
  elsif p_stage = 'llm' and t.answer_status = 'pending' and t.llm_attempt = p_attempt then
    update public.chat_turns set answer_status = case when p_ok then 'done' else 'failed' end,
      answer = case when p_ok then p_text end, llm_ms = p_ms, total_ms = p_total_ms,
      finish_reason = p_finish_reason,
      error_message = case when p_ok then null else left(coalesce(p_error_code,'llm_failed'),256) end
      where id = t.id returning * into t;
  elsif p_stage = 'tts' and t.tts_status = 'pending' and t.tts_attempt = p_attempt then
    update public.chat_turns set tts_status = case when p_ok then 'ready' else 'failed' end,
      tts_ms = p_ms, total_ms = p_total_ms,
      error_message = case when p_ok then null else left(coalesce(p_error_code,'tts_failed'),256) end
      where id = t.id returning * into t;
  else raise exception 'Stale result or invalid stage' using errcode = '55000'; end if;
  return t;
end;
$$;

create function public.record_turn_total_ms(p_user_id uuid, p_turn_id uuid, p_stage text,
  p_attempt integer, p_total_ms integer)
returns public.chat_turns language plpgsql security invoker set search_path = '' as $$
declare t public.chat_turns; actual_attempt integer; actual_status text;
begin
  if p_stage is null or p_stage not in ('stt','llm','tts') or p_attempt is null or p_attempt < 1
    or p_total_ms is null or p_total_ms < 0
  then raise exception 'Invalid timing' using errcode = '22023'; end if;
  select c.* into t from public.chat_turns c join public.chat_sessions s on s.id = c.session_id
    join public.personas p on p.id = s.persona_id
    where c.id = p_turn_id and c.user_id = p_user_id and p.status = 'active' for update of c;
  if not found then raise exception 'Turn unavailable' using errcode = 'P0002'; end if;
  actual_attempt := case p_stage when 'stt' then t.stt_attempt when 'llm' then t.llm_attempt else t.tts_attempt end;
  actual_status := case p_stage when 'stt' then t.question_status when 'llm' then t.answer_status else t.tts_status end;
  if actual_attempt <> p_attempt or actual_status is null or actual_status not in ('done','ready','failed')
    or t.question_status = 'pending' or t.answer_status = 'pending' or t.tts_status = 'pending'
    or (p_stage = 'stt' and t.llm_attempt > 0) or (p_stage = 'llm' and t.tts_attempt > 0)
    or (t.total_ms is not null and t.total_ms <> p_total_ms)
  then raise exception 'Timing belongs to a stale stage' using errcode = '55000'; end if;
  update public.chat_turns set total_ms = p_total_ms where id = t.id returning * into t;
  return t;
end;
$$;

revoke all on function replica_private.guard_integration_metadata() from public, anon, authenticated;
grant execute on function replica_private.guard_integration_metadata() to service_role;
revoke all on function public.start_chat_stage_v2(uuid,uuid,text,text,boolean,uuid) from public, anon, authenticated;
revoke all on function public.finish_chat_stage_v2(uuid,uuid,text,integer,boolean,text,integer,text,integer,text,uuid) from public, anon, authenticated;
revoke all on function public.record_turn_total_ms(uuid,uuid,text,integer,integer) from public, anon, authenticated;
grant execute on function public.start_chat_stage_v2(uuid,uuid,text,text,boolean,uuid) to service_role;
grant execute on function public.finish_chat_stage_v2(uuid,uuid,text,integer,boolean,text,integer,text,integer,text,uuid) to service_role;
grant execute on function public.record_turn_total_ms(uuid,uuid,text,integer,integer) to service_role;
notify pgrst, 'reload schema';
