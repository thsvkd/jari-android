# 자리났다 Android

Android 앱과 독립 예약 API 서버를 함께 관리합니다. 루트의 TypeScript/Vite 화면을 Capacitor로 패키징합니다.
`backend/`에는 서버와 필요한 기존 공통 패키지가 포함됩니다. 앱은 `/api/mobile` 계약을 소비합니다.
서버 진입점은 `python -m korail_bot.mobile` 하나입니다. 텔레그램 봇 진입점(`korail_bot.app`)은 이 저장소에 없습니다.

- 변경 전 관련 코드를 읽고 기존 변경을 보존합니다. 기능 작업은 별도 워크트리를 사용합니다.
- 커밋 메시지는 한국어 현재형 평서문을 사용합니다.
- 커밋 전 검증: 변경을 모두 `git add` 한 뒤 `npm run verify`. 유닛·모듈(vitest, pytest) → 통합(`backend/tests/e2e`, 실서버 + 가짜 코레일) → Playwright e2e·레이아웃(헤드리스 라이트·다크 + 실기기 `com.jari.app.e2e`)을 모두 통과해야 스테이징된 트리에 도장이 찍히고, pre-commit 훅은 도장 없는 커밋을 거부합니다. 실기기는 잠금을 풀고 화면을 켜 둡니다. 폰과 에뮬레이터처럼 여러 대가 붙어 있으면 `ANDROID_SERIAL=<serial>`로 하나를 고릅니다. 에뮬레이터는 폰이 없을 때의 보조이고, 화면 최종 확인은 실기기로 합니다(삼성 글꼴·WebView 버전 차이).
- 사용자가 보는 흐름·화면을 바꾸면 `e2e/`에 그 흐름을 추가하고, 새 화면·상태는 `expectCleanLayout` 으로 레이아웃을 잽니다. 가짜 객체를 손으로 짓지 말고 라이브러리의 실제 클래스로 만듭니다(`backend/tests/e2e/fake_korail`).
- 고치는 동안은 `npm run verify -- --only=<단계 이름 일부>`, `npm run test:e2e`, backend의 `uv run --frozen pytest tests/unit -q` 로 부분만 돌립니다. 부분 실행은 도장을 찍지 않습니다.
- Docker 구성은 `compose.yaml`을 사용합니다. 실제 서버 시작·재시작 전 검색 및 예약 상태를 확인하고 사용자 승인을 받습니다.
- Android 빌드: `scripts/build-android.ps1`; 데모 번들을 먼저 만들었다면 `-SkipWebBuild`. e2e 앱은 `-E2ePort 18281`(검증 스크립트가 알아서 만듭니다).
- 실제 예약·취소·계정 등록·서버 배포는 명시적 사용자 승인 없이 실행하지 않습니다.
- 데모 결과를 실서비스 검증으로 표현하지 않습니다. 결제는 사용자가 직접 합니다.
- 세션은 Android Keystore 기반 저장소를 사용합니다. 비밀번호·토큰·서명키·Firebase 설정을 커밋하지 않습니다.
- 조회 실패와 빈 좌석 없음은 구분합니다. 확인되지 않은 검색을 정상 작동으로 표시하지 않습니다.
- 초기 분리 작업의 원본 봇 저장소와 기존 워크트리는 삭제·변경하지 않습니다.
