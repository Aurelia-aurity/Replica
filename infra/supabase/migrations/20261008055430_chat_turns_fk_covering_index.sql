-- chat_turns의 복합 FK (session_id, user_id)를 덮는 인덱스로 교체한다.
-- Supabase 성능 권고(unindexed_foreign_keys) 대응.
-- 백엔드 조회(get_context, list_turns, get_turn_results)도 session_id + user_id로 거르고
-- created_at으로 정렬하므로 이 인덱스 하나로 처리된다.
drop index if exists public.chat_turns_session_created_idx;

create index chat_turns_session_user_created_idx
  on public.chat_turns (session_id, user_id, created_at);
