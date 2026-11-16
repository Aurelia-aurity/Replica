-- Forward patch after redesign.sql. The original deployment remains immutable. Do not re-run redesign.sql.
-- expire_chat_stages(uuid, timestamptz) used one cutoff for STT, LLM and TTS. A TTS stage that retries for up to
-- the app wait time (about 170 s) was therefore closed by the shorter STT/LLM cutoff (LLM_TIMEOUT_SECONDS, 30 s
-- fallback) when any other request of the same user arrived.
-- This adds a NEW function with its own name so the existing two-argument function keeps working unchanged for
-- old backend instances and no overloaded function with default arguments appears in PostgREST.
-- Both cutoffs are required: the caller states the TTS cutoff explicitly. Ownership, pending-state conditions and
-- the attempt checks of finish_chat_stage are the same as before; only service_role may execute it.
-- Deploy order: apply this file, check that PostgREST lists the RPC and the privileges, then switch the backend.
create or replace function public.expire_chat_stages_v2(p_user_id uuid, p_started_before timestamptz,
  p_tts_started_before timestamptz)
returns integer language plpgsql security invoker set search_path = '' as $$
declare n integer; total integer := 0;
begin
  if p_started_before is null or p_tts_started_before is null
    or p_started_before > clock_timestamp() or p_tts_started_before > clock_timestamp()
  then raise exception 'Invalid timeout cutoff' using errcode = '22023'; end if;
  update public.chat_turns set question_status = 'failed', error_message = 'stt_timeout'
    where user_id = p_user_id and question_status = 'pending' and stt_started_at < p_started_before;
  get diagnostics n = row_count; total := total + n;
  update public.chat_turns set answer_status = 'failed', error_message = 'llm_timeout'
    where user_id = p_user_id and answer_status = 'pending' and llm_started_at < p_started_before;
  get diagnostics n = row_count; total := total + n;
  update public.chat_turns set tts_status = 'failed', error_message = 'tts_timeout'
    where user_id = p_user_id and tts_status = 'pending' and tts_started_at < p_tts_started_before;
  get diagnostics n = row_count; return total + n;
end;
$$;

revoke all on function public.expire_chat_stages_v2(uuid,timestamptz,timestamptz)
  from public, anon, authenticated;
grant execute on function public.expire_chat_stages_v2(uuid,timestamptz,timestamptz)
  to service_role;
notify pgrst, 'reload schema';
