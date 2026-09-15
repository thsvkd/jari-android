# 자리났다 Android

Android 앱과 독립 예약 API 서버를 함께 관리합니다. 루트의 TypeScript/Vite 화면을 Capacitor로 패키징합니다.
`backend/`에는 서버와 필요한 기존 공통 패키지가 포함됩니다. 앱은 `/api/mobile` 계약을 소비합니다.
서버 진입점은 `python -m korail_bot.mobile`입니다. 기존 `korail_bot.app`은 실행하지 않습니다.

- 변경 전 관련 코드를 읽고 기존 변경을 보존합니다. 기능 작업은 별도 워크트리를 사용합니다.
- 커밋 메시지는 한국어 현재형 평서문을 사용합니다.
- 검증: `npm test`, `npm run build`, `npm run build:demo`.
- 서버 검증: backend에서 `uv run --frozen pytest tests/unit -q`, `uv run --frozen ruff check src tests`.
- Docker 구성은 `compose.yaml`을 사용합니다. 실제 서버 시작·재시작 전 검색 및 예약 상태를 확인하고 사용자 승인을 받습니다.
- Android 검증: `scripts/build-android.ps1`; 데모 번들을 먼저 만들었다면 `-SkipWebBuild`.
- 실제 예약·취소·계정 등록·서버 배포는 명시적 사용자 승인 없이 실행하지 않습니다.
- 데모 결과를 실서비스 검증으로 표현하지 않습니다. 결제는 사용자가 직접 합니다.
- 세션은 Android Keystore 기반 저장소를 사용합니다. 비밀번호·토큰·서명키·Firebase 설정을 커밋하지 않습니다.
- 조회 실패와 빈 좌석 없음은 구분합니다. 확인되지 않은 검색을 정상 작동으로 표시하지 않습니다.
- 초기 분리 작업의 원본 봇 저장소와 기존 워크트리는 삭제·변경하지 않습니다.
