# 자리났다 Android

- 앱 API: `/api/mobile`. 서버 진입점: `python -m korail_bot.mobile`.
- 기존 변경·원본 봇 저장소·기존 워크트리를 보존합니다. 기능 작업은 별도 워크트리에서 합니다.
- 실제 예약·취소·계정 등록·배포는 명시적 승인을 받습니다. 운영 서버 시작·재시작은 검색·예약 상태 확인 후 승인받습니다. 결제는 사용자가 합니다.
- 세션은 Android Keystore 기반 저장소를 사용합니다. 비밀번호·토큰·서명키·Firebase 설정은 커밋하지 않습니다.
- 조회 실패와 좌석 없음을 구분하고, 미확인 검색·데모를 실서비스 성공으로 보고하지 않습니다.
- 화면·흐름 변경은 `e2e/`에 추가하고 새 화면·상태는 `expectCleanLayout`으로 검사합니다. 코레일 테스트 객체는 실제 라이브러리 클래스로 만듭니다(`backend/tests/e2e/fake_korail`).
- 커밋 훅은 빠른 검사(타입·vitest·ruff)만 합니다. 푸시하면 GitHub Actions가 서버 유닛·통합·헤드리스 E2E(레이아웃 측정)와 Android 에뮬레이터 기기 단계를 돌립니다. 결과는 `gh run watch`로 확인하고 실패를 남긴 채 다음 일로 넘어가지 않습니다.
- main은 CI(`checks`·`device`)를 통과한 커밋만 받습니다(저장소 규칙). 작업 브랜치를 푸시해 초록불을 본 뒤 같은 커밋을 main에 fast-forward로 올립니다. 강제 푸시·삭제도 막혀 있습니다.
- CI `device`는 기기에서만 의미 있는 장면을 돕니다. 시간 휠을 넘기는 장면은 에뮬레이터를 죽이므로 기기에서는 `wheelHere`로 건너뛰고(헤드리스는 그대로 잼), 휠까지 전부는 `device-full`(야간·수동·릴리스 태그, 필수 아님)이 봅니다. `device`가 실패했는데 실패 기록에 에뮬레이터가 없으면(사라짐) 그 실행만 한 번 다시 돌리고, 그 밖의 실패는 진짜 실패이니 고칩니다. 같은 사라짐이 다시 나오면 사용자에게 알립니다. 야간 `device-full` 실패는 다음에 시작하는 세션이 첫 일로 맡습니다.
- 여러 세션이 일할 때 `main` 반영은 한 번에 한 세션이 하고, 같은 브랜치에는 한 세션만 푸시합니다(새 푸시가 앞 CI를 취소합니다). 세션을 시작하면 `gh run list -b main -L 3`으로 상태를 봅니다. 자세한 것은 `docs/development.md`.
- 화면은 스크린샷이 아니라 실제 렌더링을 재서 규칙으로 검사합니다(`e2e/layout.ts`: 겹침·넘침·잘린 글자·48px 터치·화면 여백선·붙박이 버튼 간격). 새 디자인 규칙이 생기면 여기에 규칙을 더합니다.
- 기기가 여러 대면 `ANDROID_SERIAL=<serial>`로 고릅니다. 기기 단계(`npm run verify`, CI)는 에뮬레이터에서만 돌고, 평소 앱(`dev.thsvkd.jari` 디버그 빌드)을 덮어쓰고 앱 데이터를 비웁니다. 실폰은 거부합니다(`JARI_ALLOW_PHYSICAL_DEVICE=1`이면 허용). 릴리스 검증은 이 Mac의 에뮬레이터로 충분합니다.
- 커밋은 빠른 검사만 통과하면 됩니다. 릴리스 태그(`v*`)는 기기 단계(에뮬레이터 가능)까지 도는 `npm run verify`를 통과한 커밋에만 푸시됩니다(`.githooks/pre-push`). 모두 이 Mac에서 끝냅니다(Windows 불필요): APK는 `scripts/release-android.sh`로 만들고(서명키는 Doppler `dev/dev`의 `JARI_ANDROID_DEBUG_*`·`JARI_ANDROID_RELEASE_*`에서 빌드 때만 꺼내며 인증서 지문을 확인합니다. 키를 올리는 것은 `scripts/doppler-store-android-keys.sh`), 태그를 푸시한 뒤 `gh release create --verify-tag`로 올립니다. Google Play(내부 테스트)용 AAB는 `scripts/release-play.sh`로 만들고(업로드 키 `JARI_ANDROID_RELEASE_*`, Firebase 설정 `JARI_ANDROID_GOOGLE_SERVICES_B64`) Play Console 업로드는 `docs/android.md`의 "Play Console 업로드" 절차를 따릅니다(Aside 브라우저가 있으면 그것으로 자율 시도하고, 없거나 막히면 사람이 올립니다).
- 문서: `README.md`(한국어, 메인)와 `README.en.md`(영어)는 함께 고칩니다. README는 소개·빠른 시작·문서 안내만 두고, 절차는 `docs/self-hosting.md`(서버 운영)·`docs/android.md`(빌드·릴리스)·`docs/development.md`(개발·검증)에, 서버 설정은 `backend/README.md`에 둡니다.
- 커밋 메시지는 한국어 현재형 평서문으로 씁니다.
