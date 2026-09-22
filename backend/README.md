# 자리났다 독립 Python 백엔드

이 폴더는 자체 `pyproject.toml`, `uv.lock`, `src`, 앱 전용 테스트를 포함합니다.
아래 CLI 명령은 모두 이 `backend/` 폴더에서 실행합니다.
원본 봇과의 연관 설명은 공통 코드의 출처를 설명하며, 원래 저장소가 설치되어 있어야 한다는 뜻이 아닙니다.
Docker 실행과 환경 파일 준비는 상위 [README](../README.md)를 따르세요.

`python -m korail_bot.mobile`은 텔레그램 봇 토큰·폴링·공개 콜백 없이 실행됩니다.
텔레그램 봇 진입점(`korail_bot.app`)은 이 저장소에 없습니다. 앱 서버는 **한 프로세스**로 실행합니다.

## 실행

Python 3.13 이상과 Redis가 필요합니다. Windows에는 시간대 데이터가 기본 제공되지 않아
`tzdata`를 명시적 의존성에 추가했습니다. `uv sync --frozen`으로 설치합니다.

필수 설정:

| 환경 변수 | 용도 |
|---|---|
| `MOBILE_SECRET_FILE` 또는 `MOBILE_SECRET` | 운영자가 보관하는 32자 이상의 무작위 암호화 키; 파일 설정 우선 |
| `MOBILE_REDIS_URL` | 모바일 전용 Redis URL, 예: `redis://127.0.0.1:6379/0`; `redis://` 또는 `rediss://`만 허용 |
| `MOBILE_DATA_DIR` | SQLite 계정·알림 저장 디렉터리; 기본 `.data/jari` |
| `MOBILE_ORIGINS` | 허용할 앱 출처를 쉼표로 나열; 기본 `https://localhost,capacitor://localhost` |
| `MOBILE_FCM_CREDENTIALS` | 선택 사항; 서버에 보관한 Firebase 서비스 계정 JSON 경로 |

기존 봇 `.env`는 자동으로 읽지 않습니다. 봇의 계정·비밀 키를 모바일 설정으로 복사하지 않습니다.
모바일 Redis 키에는 `jari:mobile:v1:` 접두어가 붙고, 앱 계정은 `mobile_...` ID 및
서버가 배정한 별도 정수 키를 사용합니다. 조회·스캔·삭제·런타임 잠금 모두 이 접두어 안에서 실행합니다.
기존 전화번호·등록 세션·검색 기록과 섞이지 않습니다. 앱 초대는 예약 이용 권한이며 개발자 권한을 주지 않습니다.

```text
uv run --frozen python -m korail_bot.mobile invite --ttl-hours 24
uv run --frozen python -m korail_bot.mobile admin --username <운영자_아이디>
uv run --frozen python -m korail_bot.mobile serve --host 127.0.0.1 --port 8081
```

`admin`은 `--password`를 생략하면 `MOBILE_ADMIN_PASSWORD` 또는 표준 입력에서 비밀번호를 읽습니다.
명령줄에 비밀번호를 적으면 셸 기록에 남으므로 생략하는 쪽을 씁니다.

`invite`가 출력한 코드는 한 번만 사용됩니다. 별도 전달은 운영자가 합니다.
앱 아이디는 영문·숫자·밑줄 3~32자, 비밀번호는 12~128자입니다.
초대 발급·API 실행에는 같은 `MOBILE_DATA_DIR`를 사용해야 합니다.
앱 계정 암호, 초대 코드, Bearer 토큰은 평문 저장하지 않습니다.
계정 비밀번호는 scrypt `N=32768,r=8,p=3`으로, 초대와 무작위 256비트 세션 토큰은 SHA-256으로 저장합니다.
세션은 30일 뒤 만료되고 `/auth/logout`에서 즉시 폐기됩니다.

외부에 제공할 때는 HTTPS 리버스 프록시가 위 loopback 포트로 연결해야 합니다.
API는 쿠키 대신 `Authorization: Bearer ...`를 사용합니다. 프록시의 전달 IP 헤더를 무조건 신뢰하지 않으므로
프록시 뒤에서는 인증 IP 제한이 공유될 수 있습니다. 기본 인증 제한은 IP·앱 아이디별 5분당 10회,
일반 API는 사용자별 분당 120회, 철도 요청은 분당 10회입니다.

Redis에는 AOF 등 지속 저장을 설정하고 실제 데이터 디렉터리를 마운트해야 합니다.
SQLite 파일과 Redis 데이터를 함께 백업하고 암호화 키는 분리 보관합니다.
SQLite 신규 디렉터리·파일은 POSIX 환경에서 각각 0700·0600으로 생성합니다.
Windows에서는 운영 계정만 접근하도록 해당 디렉터리 ACL을 설정합니다.
키를 잃거나 바꾸면 저장된 철도 자격증명을 복구할 수 없으므로 같은 키를 유지해야 합니다.

## 철도 연결 없는 검토 실행

```text
uv run --frozen python -m korail_bot.mobile serve --auth-only --host 127.0.0.1 --port 8081
```

이 모드는 실제 SQLite 앱 인증과 알림함을 사용하지만 Redis 연결 및 철도 호출을 하지 않습니다.
`bootstrap.capabilities`의 `korail`, `waitlist`, `scheduledSearch`, `favourites`, `notificationSettings`가
`false`이고 예약 관련 요청에는 503을 반환합니다. 앱의 명시적 데모 모드와도 별개입니다.
인증만 검토할 때도 영속 키 설정이 필요합니다.

## API와 기능 범위

명세의 `/api/mobile` 경로를 그대로 제공합니다. 오류는 `{error:string}`입니다.
앱 인증 오류에만 401을 사용하고, 철도 계정 재연결이 필요하면 428을 반환합니다.
요청 최대 크기는 2 MiB이며 JSON 객체만 허용합니다. 좌석 계획 전체가 한 요청에 담기므로
`korail_bot.mobile.config.MAX_REQUEST_BYTES` 한 값이 HTTP 본문 한도와 조건 파싱 한도를 함께 정합니다.
`chatId`, `userId` 등 클라이언트의 사용자 지정은 거부합니다.
앱 로그아웃은 세션을 폐기하며, `/logout`은 철도 계정을 해제합니다.
철도 해제 시 진행 중 검색을 먼저 중지하고 예약 시각·복구 자격증명도 지웁니다.

```json
{
  "capabilities": {
    "korail": true,
    "srt": false,
    "waitlist": true,
    "scheduledSearch": true,
    "favourites": true,
    "notificationSettings": true,
    "durableNotifications": true,
    "push": false,
    "lastChecked": false
  },
  "notifications": {"pushAvailable": false}
}
```

`running`은 실제 프로세스 소유권·생존 여부를 확인하고 반환합니다.
철도에 마지막으로 조회한 시각은 측정하지 않으므로 `lastChecked` 값을 만들어 주지 않습니다.
역 목록은 기존 저장소의 정적 대체 목록과 같은 스냅샷이며 부트스트랩은 철도 서버를 호출하지 않습니다.
실제 열차 조회·예약·선호 좌석 검사·예약 취소는 기존 Korail 서비스에 위임합니다.
코레일이 아닌 사업자 요청은 422로 거부합니다. 코레일 예약 대기는 일반실에만 신청할 수 있고
좌석 위치를 함께 지정하면 422로 거부합니다. 결제는 철도 앱에서 사람이 합니다.
좌석 조건은 기존 형식 `A,D:3-5`, `A,D:`, `:3-5`, 빈 문자열을 사용합니다.
잘못된 좌석 조건을 임의로 무시하지 않습니다.

## 백그라운드 작업과 알림

검색은 `korail_bot.mobile.worker` 자식 프로세스로 실행하며 철도 비밀번호는 stdin으로 전달합니다.
프로세스 명령줄에는 비밀번호·토큰을 넣지 않습니다. 자식은 독립 저장소와 알림 전송기를 생성하며
`/reservation-callback`, `/check_payment` HTTP 경로가 필요하지 않습니다. 외부 앱 서버에도 해당 경로가 없습니다.
검색 결과와 진행 알림은 SQLite 알림함에 기록됩니다. 예약된 검색은 기존 스케줄러가 실행하고,
결제 상태는 기존 watchdog이 철도에 확인합니다. 결제 대기는 영속 기록에서 재구성하여 분당 한 번 안내합니다.

프로세스 재시작 시 기존 복구 로직을 실행합니다. Redis의 런타임 임대 잠금은 여러 앱 프로세스가
같은 검색을 중복 실행하지 못하게 하며 강제 종료 직후에는 최대 120초 동안 남을 수 있습니다.
앱 계정 로그아웃은 서버에서 계속 진행하는 검색을 중지하지 않습니다. 검색 중지는 별도 버튼입니다.

`GET /notifications`는 본인의 최근 100개 이벤트를 최신순으로 반환합니다.
푸시 전송 여부와 관계없이 알림함은 유지됩니다. 휴대전화 번호·알려진 철도 비밀번호는 알림에서 가립니다.

FCM을 사용하려면 `uv sync --frozen --extra mobile-push`로 선택 의존성을 설치하고
`MOBILE_FCM_CREDENTIALS`에 기존 서비스 계정 파일을 지정합니다. 계정/프로젝트/키를 코드가 생성하지 않습니다.
앱에도 같은 Firebase 프로젝트의 Android 설정과 알림 권한, 실제 기기 토큰이 필요합니다.
미설정 상태에서는 `pushAvailable=false`, 기기 등록은 503입니다.
기기 토큰은 암호화 저장하고 타 사용자에게 재할당하거나 타 사용자 토큰을 삭제하지 않습니다.
푸시는 예약 상세 없이 새 알림이 있다는 문구와 이벤트 ID만 보냅니다.
실패한 전송은 영속 대기열에 남아 최대 10회 재시도하며, 성공 응답은 휴대전화 수신 확인을 뜻하지 않습니다.
10회 뒤에도 앱 알림함은 그대로 남습니다.

## 검증

```text
uv run --frozen pytest tests/unit -q
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
```

이 저장소의 테스트는 `tests/unit` 하나뿐이며 Docker 없이 실행됩니다. `pyproject.toml`의
`testpaths`가 같은 경로를 가리키므로 인수 없는 `pytest`도 같은 142개를 수집합니다.
보안 테스트는 실제 SQLite 트랜잭션과 fakeredis를 사용합니다. 철도와 프로세스 실행 경계만 대체하며
실제 예약·외부 메시지·Firebase 발송은 수행하지 않습니다.

설계 근거: [OWASP 비밀번호 저장 지침](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html),
[Flask 요청 크기와 보안](https://flask.palletsprojects.com/en/stable/web-security/),
[Firebase Admin SDK 메시지 전송](https://firebase.google.com/docs/cloud-messaging/send/admin-sdk),
[Redis 지속 저장](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/).

실제 철도 로그인·예약·취소, 실제 FCM 발송, 운영 재시작·배포는 위 검사에 포함되지 않습니다.
Firebase 설정과 휴대전화 푸시 수신은 외부 구성과 별도 기기 검증이 필요합니다.
