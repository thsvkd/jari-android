# 회원 승인 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 신규 회원의 가입과 실제 이용 권한을 관리자 승인으로 분리한다.

**Architecture:** 기존 SQLite identity에 승인 상태를 추가하고 API 인증 경계에서 검증한다. 기존 bootstrap으로 제한된 회원 상태를 전달해 앱 시작 경로를 유지한다. 서버와 앱을 정해진 계약으로 병렬 구현하고 리더가 E2E·문서·전체 검증을 통합한다.

**Tech Stack:** Flask, SQLite, Python/pytest, TypeScript, Vitest, Playwright/Capacitor.

**Spec:** `docs/superpowers/specs/2026-10-17-member-approval-design.md`

## Global Constraints

- 기존 회원은 승인 상태를 유지한다. 새 가입은 `pending`이다.
- 초대 코드 입력·발급을 제거한다. 새 의존성을 추가하지 않는다.
- 운영 데이터 변경·서버 재시작·배포는 범위 밖이다.
- 새 화면·상태를 `expectCleanLayout`으로 확인한다.

## Review Focus

- 구버전 DB의 기존 토큰으로 이용할 수 있고, 새 가입이 자동 승인되지 않는다.
- 유효한 토큰이어도 대기·거절은 OAuth/MCP를 포함한 철도 기능을 호출하지 못한다.
- 승인 응답·새로고침이 로그아웃 이후 도착해도 다른 세션을 바꾸지 않는다.
- 관리자가 목록 조회에 실패하면 빈 목록 대신 오류와 재시도를 보여준다.
- 승인 결정 재전송·동시 결정으로 승인 계정이 다시 거절되거나 관리자가 잠기지 않는다.

### Task 1: 서버의 승인 상태와 권한 경계

**Files:** `backend/src/korail_bot/mobile/{identity,api,oauth,mcp,__main__}.py`, 관련 `backend/tests/`.

**Interfaces:** 설계의 AppUser 필드, `/admin/users` 및 `/admin/users/<id>/approval`, 제한된 `/bootstrap`.

- [x] 새 가입·마이그레이션·권한 차단·관리자 결정을 테스트하고 실패를 확인한다.
- [x] 최소 구현과 기존 테스트/실서버 테스트용 승인된 회원 fixture를 갱신한다.
- [x] 서버 유닛·통합 테스트, ruff 검사를 통과시킨다.

### Task 2: 앱의 대기 화면과 관리자 회원 관리

**Files:** `src/{types,api,app,demo}.ts`, 관련 테스트와 CSS.

**Interfaces:** Task 1의 API 계약을 사용한다. 구서버에서 status가 생략되면 기존 승인 회원으로 취급한다.

- [x] 가입·대기/거절·상태 새로고침·관리자 목록과 실패 테스트를 추가하고 실패를 확인한다.
- [x] 초대 UI를 제거하고 승인 흐름을 구현한다. 토큰은 기존 저장 경로를 유지한다.
- [x] 타입 검사와 Vitest를 통과시킨다.

### Task 3: 실사용 흐름 검증과 문서

**Files:** `e2e/`, `README.md`, `README.en.md`, `backend/README.md`, `docs/{SPEC,PRD,self-hosting,development}.md`.

**Interfaces:** 실서버의 대기 회원과 관리자 로그인으로 가입→승인→같은 세션 진입을 검증한다.

- [x] 가입·승인·거절·탈퇴·레이아웃 E2E를 갱신/추가한다.
- [x] 한국어·영어 안내와 운영/API 문서를 새 동작에 맞춘다.
- [x] `npm run verify -- --ci`의 각 단계를 통과시키고 독립 리뷰의 유효한 지적을 해결한다.
- [x] 변경·검증·운영 반영 전제와 작업 트리 경로를 완료 보고에 정리한다.

## 실행 중 결정

- 다른 릴리스 워크트리의 로컬 E2E 서버를 보존하기 위해 `JARI_E2E_PORT_OFFSET=1000`으로 검증 포트를 분리한다.
- 독립 리뷰의 응답 순서 경쟁과 응답 없는 승인 갱신 문제를 회귀 테스트로 재현하고 목록 요청 순번·20초 bootstrap 읽기 제한으로 해결한다.
- 검증은 독립된 프런트·서버·통합·헤드리스 단계로 나눠 실행한다. Android 에뮬레이터 검증·원격 CI·운영 배포는 이번 로컬 구현 검증에 포함하지 않는다.

## 검증 결과

- 프런트 전체 298개, 서버 유닛 전체와 마지막 추가 권한·동시성·기존 세션 유지 회귀 검사, 서버 통합 19개 통과.
- 헤드리스 E2E 152개 통과: 밝은 테마·어두운 테마·344px 화면에서 신규 승인 흐름과 기존 기능·레이아웃을 확인했다.
- 앱·E2E·모니터 TypeScript, 일반·데모 빌드, ruff lint·format, auth-only 실서버 smoke, 배포 스크립트 회귀 검사 통과.
- 독립 코드 리뷰: 두 응답 처리 문제를 수정한 뒤 남은 지적 없음.
- 작업 공간: `/Users/thsvkd/.codex/worktrees/ae5f/jari-android`, 브랜치 `feat/member-approval`. 원본 체크아웃은 변경하지 않았다.
