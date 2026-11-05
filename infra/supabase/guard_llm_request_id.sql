-- Forward patch after weekend_integration.sql. The original deployment remains immutable.
-- Deploy only after #63's LLM failure caller passes the associated request_id.
create or replace function public.finish_chat_stage(p_user_id uuid, p_turn_id uuid, p_stage text,
  p_attempt integer, p_ok boolean, p_text text default null, p_ms integer default null,
  p_error_code text default null)
returns public.chat_turns language plpgsql security invoker set search_path = '' as $$
declare t public.chat_turns;
begin
  select * into t from public.chat_turns where id = p_turn_id and user_id = p_user_id for update;
  if not found then raise exception 'Turn unavailable' using errcode = 'P0002'; end if;
  if p_stage = 'llm' and t.llm_request_id is not null
  then raise exception 'Linked generation requires finish_chat_stage_v2 and request ID' using errcode = '55000'; end if;
  if p_attempt is null or p_attempt < 1 or p_ok is null or p_ms < 0
  then raise exception 'Invalid stage result' using errcode = '22023'; end if;
  if p_ok and p_stage in ('stt', 'llm') and (p_text is null or length(btrim(p_text)) = 0)
  then raise exception 'Successful text result must not be blank' using errcode = '22023'; end if;
  if p_stage = 'stt' and t.question_status = 'pending' and t.stt_attempt = p_attempt then
    update public.chat_turns set question_status = case when p_ok then 'done' else 'failed' end,
      question = case when p_ok then p_text end, stt_ms = p_ms,
      error_message = case when p_ok then null else left(coalesce(p_error_code, 'stt_failed'), 256) end
      where id = t.id returning * into t;
  elsif p_stage = 'llm' and t.answer_status = 'pending' and t.llm_attempt = p_attempt then
    update public.chat_turns set answer_status = case when p_ok then 'done' else 'failed' end,
      answer = case when p_ok then p_text end, llm_ms = p_ms,
      error_message = case when p_ok then null else left(coalesce(p_error_code, 'llm_failed'), 256) end
      where id = t.id returning * into t;
  elsif p_stage = 'tts' and t.tts_status = 'pending' and t.tts_attempt = p_attempt then
    update public.chat_turns set tts_status = case when p_ok then 'ready' else 'failed' end,
      tts_ms = p_ms,
      error_message = case when p_ok then null else left(coalesce(p_error_code, 'tts_failed'), 256) end
      where id = t.id returning * into t;
  else raise exception 'Stale result or invalid stage' using errcode = '55000'; end if;
  return t;
end;
$$;

revoke all on function public.finish_chat_stage(uuid,uuid,text,integer,boolean,text,integer,text)
  from public, anon, authenticated;
grant execute on function public.finish_chat_stage(uuid,uuid,text,integer,boolean,text,integer,text)
  to service_role;
notify pgrst, 'reload schema';
