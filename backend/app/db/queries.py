"""대화방·턴 저장소가 실행하는 SQL.

모든 쿼리는 `user_id` 조건을 포함한다. 백엔드는 RLS를 우회하는 postgres 역할로
접속하므로, 다른 사용자의 행이 조회·수정되지 않게 하는 책임이 이 쿼리들에 있다.
다른 사용자의 id를 넘기면 결과는 0행이다.

자리표시자는 psycopg의 `%(name)s` 형식을 쓴다. SQL 안에 퍼센트 문자를 쓰지 않는다.
"""

# 대화방 ---------------------------------------------------------------------

# 페르소나가 같은 사용자 소유일 때만 대화방을 만든다.
CREATE_SESSION = """
insert into public.chat_sessions (user_id, persona_id)
select p.user_id, p.id
from public.personas p
where p.id = %(persona_id)s and p.user_id = %(user_id)s
returning *
"""

# 처음 확인한 시각을 유지한다.
ACK_AI_NOTICE = """
update public.chat_sessions
set ai_notice_ack_at = coalesce(ai_notice_ack_at, now())
where id = %(session_id)s and user_id = %(user_id)s
returning *
"""

# 턴 생성 ---------------------------------------------------------------------

# 대화방이 같은 사용자 소유일 때만 넣는다. 같은 id가 이미 있으면 아무것도 하지 않는다.
INSERT_TURN = """
insert into public.chat_turns
  (id, session_id, user_id, input_mode, question, question_status)
select %(turn_id)s, s.id, s.user_id, %(input_mode)s, %(question)s, %(question_status)s
from public.chat_sessions s
where s.id = %(session_id)s and s.user_id = %(user_id)s
on conflict (id) do nothing
"""

GET_TURN = """
select * from public.chat_turns
where id = %(turn_id)s and user_id = %(user_id)s
"""

# 단계 갱신 -------------------------------------------------------------------

# 음성 턴의 STT 결과. 응답을 시작한 뒤에는 질문을 바꾸지 않는다.
SET_QUESTION = """
update public.chat_turns
set question = %(text)s, question_status = 'done', stt_ms = %(stt_ms)s, error_message = null
where id = %(turn_id)s and user_id = %(user_id)s
  and input_mode = 'voice' and answer_status is null
returning *
"""

# 응답 시작과 LLM 재시도. 진행 중(pending)이거나 이미 끝난(done) 응답은 다시 시작하지 않는다.
START_ANSWER = """
update public.chat_turns
set answer_status = 'pending', answer = null, error_message = null,
    model_name = %(model_name)s, llm_ms = null, tts_ms = null,
    tts_status = case when %(with_audio)s then 'pending' end
where id = %(turn_id)s and user_id = %(user_id)s
  and question_status = 'done'
  and (answer_status is null or answer_status = 'failed')
returning *
"""

COMPLETE_ANSWER = """
update public.chat_turns
set answer = %(text)s, answer_status = 'done', llm_ms = %(llm_ms)s
where id = %(turn_id)s and user_id = %(user_id)s
  and answer_status = 'pending'
returning *
"""

# 음성 합성 시작과 재시도. 텍스트 턴에서 나중에 음성을 요청할 때도 쓴다.
START_TTS = """
update public.chat_turns
set tts_status = 'pending', tts_ms = null, error_message = null
where id = %(turn_id)s and user_id = %(user_id)s
  and answer_status = 'done'
  and (tts_status is null or tts_status = 'failed')
returning *
"""

SET_TTS_RESULT = """
update public.chat_turns
set tts_status = case when %(ok)s then 'ready' else 'failed' end,
    tts_ms = %(tts_ms)s,
    error_message = case when %(ok)s then error_message else %(error)s end
where id = %(turn_id)s and user_id = %(user_id)s
  and tts_status = 'pending'
returning *
"""

FAIL_STT = """
update public.chat_turns
set question_status = 'failed', error_message = %(error)s
where id = %(turn_id)s and user_id = %(user_id)s
  and question_status = 'pending'
returning *
"""

# LLM이 실패하면 합성할 응답이 없으므로 음성 상태도 비운다.
FAIL_LLM = """
update public.chat_turns
set answer_status = 'failed', tts_status = null, error_message = %(error)s
where id = %(turn_id)s and user_id = %(user_id)s
  and answer_status = 'pending'
returning *
"""

RECORD_TOTAL_MS = """
update public.chat_turns
set total_ms = %(total_ms)s
where id = %(turn_id)s and user_id = %(user_id)s
returning *
"""

# 조회 ------------------------------------------------------------------------

# 응답이 끝난 최근 턴을 오래된 순으로 돌려준다.
GET_CONTEXT = """
select * from (
  select * from public.chat_turns
  where session_id = %(session_id)s and user_id = %(user_id)s
    and answer_status = 'done'
  order by created_at desc
  limit %(limit)s
) recent
order by created_at
"""

LIST_TURNS = """
select * from public.chat_turns
where session_id = %(session_id)s and user_id = %(user_id)s
order by created_at
"""

# 대표 시나리오 실행 결과 요약 (수행계획서 2.7절, NFR-01).
# 정상 완료: 질문·응답이 done이고, 음성을 요청했다면 음성도 ready.
SUMMARIZE_TURNS = """
select
  count(*)::int as total,
  (count(*) filter (
     where question_status = 'done' and answer_status = 'done'
       and coalesce(tts_status, 'ready') = 'ready'))::int as completed,
  round(avg(stt_ms))::int   as avg_stt_ms,
  round(avg(llm_ms))::int   as avg_llm_ms,
  round(avg(tts_ms))::int   as avg_tts_ms,
  round(avg(total_ms))::int as avg_total_ms
from public.chat_turns
where session_id = %(session_id)s and user_id = %(user_id)s
"""
