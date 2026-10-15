# Replica

종합설계프로젝트1 13팀 레플리카입니다.

사용자가 제공한 음성·텍스트 기록을 바탕으로 개인화된 텍스트·음성 대화를 제공하는 앱을 개발합니다.

## 디렉토리 구조

| 경로 | 용도 |
| --- | --- |
| `mobile/` | Flutter 앱, 화면, 녹음·재생, API 연동 |
| `backend/` | 사용자·기록·대화 API, 외부 STT·TTS 연동 |
| `ai/` | 학교 GPU의 LLM 추론 및 개인화 코드 |
| `contracts/` | 앱↔백엔드↔AI 요청·응답 명세 |
| `experiments/` | 모델·개인화·음성 처리 비교 실험 |
| `infra/` | Docker·Nginx 배포 설정, Supabase 스키마·정책 |
| `docs/` | 요구사항, 구조도, 기술 결정, 회의록, 협업·자료 안내 |
| `scripts/` | 공통 실행·점검 보조 스크립트 |

## 현재 상태

현재는 디렉토리 골격과 안내 문서만 있으며 실행 가능한 앱·서버는 없습니다.
프로젝트 계획의 기준은 팀 Drive에 보관한 최종 수행계획서 PDF입니다.
합의한 도구·버전은 [개발 환경](docs/development-environment.md)에 기록합니다.
실제 실행 명령과 의존성은 각 파트의 구현과 함께 해당 README에 추가합니다.

## 개발 시작 순서

1. `docs/requirements/`에 baseline 범위를 기록합니다.
2. `contracts/`에서 요청·응답과 오류 형식을 합의합니다.
3. 각 파트의 README에 개발 환경·실행·검증 방법을 추가합니다.
4. 통합 가능한 단위로 개발하고 PR에서 변경 범위와 확인 결과를 공유합니다.

협업 안내는 [CONTRIBUTING.md](CONTRIBUTING.md)를 참고하세요.
실제 개인 기록·녹음, 비밀 값, 모델 가중치·체크포인트는 커밋하지 않습니다.

## 팀 작업 안내

- [Notion 프로젝트 허브](https://app.notion.com/p/3ee54b33957f808f879fc8b85e37e5bc): 작업 보드, 캘린더·간트, 마일스톤, 자료실
- [팀 Drive](https://drive.google.com/drive/folders/1mWkROL5Lj1PcriPdHs3MBy--TSxXpOhg): 수업 자료, 계획서·보고서, 발표 파일, 제출 증빙, 팀·멘토 회의 자료
- [자료 보관 기준](docs/materials.md): 원본 위치·최종본·갱신 및 제출 기준
- [문서 모음](docs/README.md): 역할·협업, 개발 환경, AI 활용, 일정, 회의록
- [이슈 등록](https://github.com/Aurelia-aurity/Replica/issues/new/choose): 작업 또는 버그 템플릿 선택

개발·문서 작업은 이슈 → 작업 브랜치 → PR → 리뷰 → Merge commit 순서로 진행합니다. 수업 제출은 Drive 보관과 별도로 수행합니다.
