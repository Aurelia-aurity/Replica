-- 로컬 개발용 시드. `supabase db reset` 때만 실행되며 클라우드 프로젝트에는 적용하지 않는다.
-- 고정 id를 쓰므로 팀원 모두 backend/.env.example의 값을 그대로 사용할 수 있다.
-- 계정 B는 교차 열람 시험(NFR-02)용이다. 로그인 기능은 baseline 범위가 아니라 비밀번호는 넣지 않는다.

insert into auth.users (instance_id, id, aud, role, email, created_at, updated_at)
values
  ('00000000-0000-0000-0000-000000000000', 'a0000000-0000-4000-8000-000000000001',
   'authenticated', 'authenticated', 'test-a@replica.local', now(), now()),
  ('00000000-0000-0000-0000-000000000000', 'a0000000-0000-4000-8000-000000000002',
   'authenticated', 'authenticated', 'test-b@replica.local', now(), now());

insert into public.personas (id, user_id, name)
values
  ('b0000000-0000-4000-8000-000000000001', 'a0000000-0000-4000-8000-000000000001', '기본 페르소나'),
  ('b0000000-0000-4000-8000-000000000002', 'a0000000-0000-4000-8000-000000000002', '기본 페르소나');
