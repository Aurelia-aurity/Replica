-- Forward patch after redesign.sql. The original deployment remains immutable. Do not re-run redesign.sql.
-- expire_chat_stages used one cutoff for STT, LLM and TTS. A TTS stage that retries for up to the app wait
-- time (about 170 s) was therefore closed by the shorter STT/LLM cutoff (LLM_TIMEOUT_SECONDS, 30 s fallback)
-- when any other request of the same user arrived.
-- This adds an optional TTS-only cutoff. Callers that send only p_user_id and p_started_before keep the
-- previous behaviour because the TTS cutoff then defaults to p_started_before.
-- Deploy this file before the backend that sends p_tts_started_before, and keep the old backend off the
-- long TTS path until it is updated (an old backend keeps closing TTS stages at its own cutoff).
drop function if exists public.expire_chat_stages(uuid, timestamptz);

create or replace function public.expire_chat_stages(p_user_id uuid, p_started_before timestamptz,
  p_tts_started_before timestamptz default null)
returns integer language plpgsql security invoker set search_path = '' as $$
declare n integer; total integer := 0; tts_before timestamptz;
begin
  tts_before := coalesce(p_tts_started_before, p_started_before);
  if p_started_before is null or p_started_before > clock_timestamp() or tts_before > clock_timestamp()
  then raise exception 'Invalid timeout cutoff' using errcode = '22023'; end if;
  update public.chat_turns set question_status = 'failed', error_message = 'stt_timeout'
    where user_id = p_user_id and question_status = 'pending' and stt_started_at < p_started_before;
  get diagnostics n = row_count; total := total + n;
  update public.chat_turns set answer_status = 'failed', error_message = 'llm_timeout'
    where user_id = p_user_id and answer_status = 'pending' and llm_started_at < p_started_before;
  get diagnostics n = row_count; total := total + n;
  update public.chat_turns set tts_status = 'failed', error_message = 'tts_timeout'
    where user_id = p_user_id and tts_status = 'pending' and tts_started_at < tts_before;
  get diagnostics n = row_count; return total + n;
end;
$$;

revoke all on function public.expire_chat_stages(uuid,timestamptz,timestamptz)
  from public, anon, authenticated;
grant execute on function public.expire_chat_stages(uuid,timestamptz,timestamptz)
  to service_role;
notify pgrst, 'reload schema';
