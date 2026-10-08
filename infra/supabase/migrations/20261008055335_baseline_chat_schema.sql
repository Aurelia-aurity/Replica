-- baseline 대화 스키마
-- 범위: FR-05 텍스트 대화, FR-06 음성 대화, FR-08 AI 생성 표시
-- 소유 구조: auth.users -> personas -> chat_sessions -> chat_turns
-- 모든 테이블에 user_id를 두고, 복합 FK로 다른 사용자 소유 행과의 연결을 막는다.
-- 앱은 DB에 직접 접근하지 않고 FastAPI(postgres 역할 연결)만 접근한다.
-- 사용자별 RLS 정책과 anon/authenticated 권한은 로그인이 생기는 고도화 1에서 추가한다.

-- 대화 상대 -------------------------------------------------------------------
create table public.personas (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references auth.users (id) on delete cascade,
  name       text not null,
  created_at timestamptz not null default now(),
  unique (id, user_id)                    -- 복합 FK의 대상
);
comment on table public.personas is '대화 상대. baseline은 사용자당 기본 페르소나 1개';

create index personas_user_id_idx on public.personas (user_id);

-- 대화방 ---------------------------------------------------------------------
create table public.chat_sessions (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references auth.users (id) on delete cascade,
  persona_id       uuid not null,
  ai_notice_ack_at timestamptz,           -- 대화 시작 전 AI 생성 안내 확인 시각 (FR-08)
  created_at       timestamptz not null default now(),
  unique (id, user_id),                   -- 복합 FK의 대상
  -- 다른 사용자의 페르소나와 연결되지 않도록
  foreign key (persona_id, user_id)
    references public.personas (id, user_id) on delete cascade
);
comment on table public.chat_sessions is '사용자와 페르소나의 대화방';

create index chat_sessions_user_id_idx on public.chat_sessions (user_id);
create index chat_sessions_persona_idx on public.chat_sessions (persona_id, user_id);

-- 대화 턴: 질문 하나와 응답 하나 -------------------------------------------------
create table public.chat_turns (
  id              uuid primary key default gen_random_uuid(),  -- 보통 앱이 만든 uuid (재전송 중복 방지)
  session_id      uuid not null,
  user_id         uuid not null references auth.users (id) on delete cascade,
  input_mode      text not null check (input_mode in ('text', 'voice')),
  question        text,                   -- 입력한 질문 또는 STT 전사문
  question_status text not null default 'pending'
                  check (question_status in ('pending', 'done', 'failed')),
  answer          text,                   -- LLM 응답. 앱은 항상 "AI 생성"으로 표시
  answer_status   text check (answer_status in ('pending', 'done', 'failed')),
  tts_status      text check (tts_status in ('pending', 'ready', 'failed')),
  model_name      text,
  stt_ms          integer check (stt_ms >= 0),
  llm_ms          integer check (llm_ms >= 0),
  tts_ms          integer check (tts_ms >= 0),
  total_ms        integer check (total_ms >= 0),
  error_message   text,
  created_at      timestamptz not null default now(),

  -- 다른 사용자의 대화방에 턴이 들어가지 않도록
  foreign key (session_id, user_id)
    references public.chat_sessions (id, user_id) on delete cascade,
  -- 완료된 단계는 내용이 있어야 함
  constraint chat_turns_question_done check (question_status <> 'done' or question is not null),
  constraint chat_turns_answer_done   check (answer_status is distinct from 'done' or answer is not null),
  -- 단계 순서: 질문 완료 -> 응답 시작 -> 음성
  constraint chat_turns_answer_after_question check (answer_status is null or question_status = 'done'),
  constraint chat_turns_tts_after_answer      check (tts_status is null or answer_status is not null),
  -- STT 시간은 음성 턴만
  constraint chat_turns_stt_voice_only check (stt_ms is null or input_mode = 'voice')
);
comment on table public.chat_turns is '질문 하나와 응답 하나. 단계마다 같은 행을 갱신';

-- 대화방별 턴 조회 (get_context, list_turns, get_turn_results)
create index chat_turns_session_created_idx on public.chat_turns (session_id, created_at);
-- 계정 삭제 시 CASCADE 조회용
create index chat_turns_user_id_idx on public.chat_turns (user_id);

-- 접근 제어 -------------------------------------------------------------------
-- RLS를 켜고 baseline에서는 정책을 만들지 않는다.
-- Data API 역할의 테이블 권한도 회수해 앱 키로는 테이블 자체에 접근할 수 없게 한다.
alter table public.personas      enable row level security;
alter table public.chat_sessions enable row level security;
alter table public.chat_turns    enable row level security;

revoke all on table public.personas, public.chat_sessions, public.chat_turns
  from anon, authenticated;
