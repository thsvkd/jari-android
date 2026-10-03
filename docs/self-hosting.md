# 서버 운영 가이드

자리났다 서버를 직접 띄우고, 지인을 초대하고, 휴대폰 앱을 그 서버에 연결하는 방법입니다.
서버 환경 변수와 API의 자세한 동작은 [서버 설정 레퍼런스](../backend/README.md)를, 내부 구조는 [기술 명세](SPEC.md)를 봅니다.

- [준비물](#준비물)
- [1. 서버 키 준비](#1-서버-키-준비)
- [2. 서버 실행](#2-서버-실행)
- [3. 관리자 계정과 초대 코드](#3-관리자-계정과-초대-코드)
- [4. HTTPS로 공개하기](#4-https로-공개하기)
- [5. 앱을 내 서버에 연결하기](#5-앱을-내-서버에-연결하기)
- [6. 휴대폰 알림 켜기](#6-휴대폰-알림-켜기)
- [7. 원격 호스트에 배포하기](#7-원격-호스트에-배포하기)
- [8. 백업과 데이터](#8-백업과-데이터)
- [9. 운영 명령 모음](#9-운영-명령-모음)
- [10. 외부 감시(선택)](#10-외부-감시선택)

## 준비물

- Docker Engine 또는 Docker Desktop(Compose v2 포함)
- 휴대폰에서 쓰려면 서버를 HTTPS로 공개할 도메인 또는 터널
- (선택) 휴대폰 알림을 쓰려면 Firebase 프로젝트

이 스택은 **텔레그램 봇을 실행하지 않습니다.** API와 Redis만 띄웁니다.

## 1. 서버 키 준비

`compose.yaml`은 두 파일을 시크릿으로 마운트합니다. 둘 중 하나라도 없으면 compose가 시작하지 않습니다.

| 파일 | 내용 |
|---|---|
| `backend/.secrets/mobile.key` | 32자 이상의 무작위 암호화 키. 저장된 코레일 로그인과 기기 토큰을 이 키로 암호화합니다. |
| `backend/.secrets/firebase-admin.json` | Firebase 서비스 계정 키(JSON). **빈 파일이면 휴대폰 알림만 꺼진 채로** 서버가 뜹니다. |

> [!WARNING]
> `mobile.key`는 따로 백업합니다. 키를 잃거나 바꾸면 저장된 코레일 로그인을 되살릴 수 없습니다. 두 파일은 Git에서 제외되어 있으니 커밋하지 않습니다.

**Windows(PowerShell)** — 저장소 루트에서:

```powershell
.\scripts\init-backend.ps1
```

키를 새로 만들고(이미 있으면 그대로 둡니다), 빈 `firebase-admin.json`과 `backend/.env`(CLI용 설정, `backend/.env.example` 복사)를 만듭니다. 키는 화면에 출력하지 않습니다.

**macOS·Linux** — 아직 준비 스크립트가 없어 위 두 파일을 직접 만듭니다. 이때 권한에 주의합니다.

- 컨테이너는 uid `10001`(`app`) 사용자로 실행되고, compose는 파일 시크릿을 bind mount로 그대로 붙입니다. 시크릿에 `uid`·`gid`·`mode`를 적어도 파일 시크릿에서는 무시됩니다([Docker Compose 레퍼런스: secrets](https://docs.docker.com/reference/compose-file/services/#secrets)).
- 그래서 Linux에서 소유자만 읽을 수 있는 파일(`0600`)은 컨테이너가 읽지 못합니다(Linux의 Docker Engine 29에서 확인). 키 파일을 컨테이너 사용자가 읽을 수 있게 하는 방법은 운영 환경에 맞게 정합니다.

Docker 없이 바로 띄워 보려면 아래 [Docker 없이 실행하기](#docker-없이-실행하기)를 따릅니다. 이 경우 서버가 내 계정으로 돌기 때문에 키를 소유자만 읽게 두어도 됩니다.

## 2. 서버 실행

```bash
docker compose up --build -d
curl http://127.0.0.1:8081/api/mobile/health
# {"booking":true,"lease":true,"ok":true,"redis":true} 이면 정상
```

- API는 호스트의 `127.0.0.1:8081`에만 열립니다. Redis 포트는 밖으로 열지 않습니다.
- 데이터는 Docker 볼륨 `identity`(SQLite: 앱 계정·초대·알림함)와 `redis`(검색 조건·즐겨찾기·실행 기록 등)에 남습니다.
- 로그: `docker compose logs -f api`

### Docker 없이 실행하기

API 서버를 내 계정의 프로세스로 띄웁니다. Python 3.13 이상, [uv](https://docs.astral.sh/uv/), Redis가 필요합니다. 저장소 루트에서:

```bash
cd backend
uv sync --frozen
mkdir -p .secrets && (umask 077 && openssl rand -base64 48 > .secrets/mobile.key)
cp .env.example .env    # MOBILE_SECRET_FILE·MOBILE_REDIS_URL 등 기본값

# Redis가 없다면 컨테이너로 띄웁니다(또는 brew install redis 등)
docker run -d --name jari-redis -p 127.0.0.1:6379:6379 redis:7-alpine redis-server --appendonly yes

uv run --env-file .env --frozen python -m korail_bot.mobile serve --host 127.0.0.1 --port 8081
```

철도 기능 없이 앱 로그인·초대만 확인하려면 Redis 없이 `serve --auth-only`로 띄웁니다. 이때 `health`는 `{"booking":false,"ok":true}`를 돌려주고 예약 관련 요청은 503입니다.

## 3. 관리자 계정과 초대 코드

가입은 초대제입니다. 먼저 운영자 본인의 관리자 계정을 만듭니다. 아이디는 영문·숫자·밑줄 3~32자, 비밀번호는 12~128자입니다.

```bash
docker compose exec -T api python -m korail_bot.mobile admin --username <아이디>
# 실행한 뒤 비밀번호를 한 줄 입력하고 Enter
```

`--password`로 명령줄에 적으면 셸 기록에 남으니 표준 입력으로 넘깁니다. macOS·Linux 셸에서 입력을 화면에 숨기려면 `stty -echo; docker compose exec -T api python -m korail_bot.mobile admin --username <아이디>; stty echo`처럼 감쌉니다. Docker 없이 실행했다면 `backend/`에서 `uv run --env-file .env --frozen python -m korail_bot.mobile admin --username <아이디>`입니다.

지인을 초대하는 방법은 두 가지입니다. 초대 코드는 한 번만 쓸 수 있습니다.

- **앱에서**: 로그인 화면의 **관리자**로 로그인 → **설정 → 회원 관리 → 회원 초대 코드**. 코드는 그 화면에서만 볼 수 있습니다.
- **서버에서**: `docker compose exec api python -m korail_bot.mobile invite --ttl-hours 24`

초대받은 사람은 앱의 **초대 회원 → 처음 가입**에서 코드를 넣습니다. 초대는 예약 이용 권한일 뿐 관리자 권한을 주지 않습니다.

## 4. HTTPS로 공개하기

휴대폰 앱은 HTTPS 주소로만 서버에 연결합니다(릴리스 빌드는 평문 HTTP를 막습니다). `127.0.0.1:8081` 앞에 HTTPS 리버스 프록시를 둡니다.

[Caddy](https://caddyserver.com/docs/quick-starts/reverse-proxy)는 도메인만 적으면 인증서를 자동으로 받습니다.

```caddy
api.example.com {
	reverse_proxy 127.0.0.1:8081
}
```

공인 IP나 포트 개방이 어렵다면 Cloudflare Tunnel 같은 터널을 앞에 둘 수 있습니다. 운영 서버는 Cloudflare → 터널 → Caddy → API 순서로 연결합니다(→ [SPEC §6](SPEC.md#6-연결-상태)).

- 프록시가 이미 쓰는 Docker 네트워크에 API를 붙이거나 포트·컨테이너 이름을 바꾸려면 [`compose.overlay.example.yaml`](../compose.overlay.example.yaml)을 복사해 고친 뒤 `docker compose -f compose.yaml -f <내 오버레이>.yaml up -d`로 겹쳐 씁니다.
- Android 앱의 출처(Origin)는 `https://localhost`이고 서버 기본값(`MOBILE_ORIGINS`)에 이미 들어 있습니다. 다른 웹 출처에서 부를 때만 더합니다.
- 같은 호스트의 `/privacy`(개인정보처리방침)와 `/delete-account`(계정 삭제 안내)도 서버가 제공합니다. 페이지에 보일 문의 주소는 `compose.yaml`의 `MOBILE_PRIVACY_CONTACT`입니다. **내 서버라면 내 주소로 바꿉니다.**

## 5. 앱을 내 서버에 연결하기

API 주소는 앱을 빌드할 때 웹 번들에 들어갑니다(`VITE_API_BASE_URL`). 공개 값이니 비밀값을 넣지 않습니다.
JDK 21과 Android SDK가 필요합니다(→ [Android 빌드·배포](android.md#준비물)).

```bash
VITE_API_BASE_URL=https://api.example.com npm run build
npx cap sync android
(cd android && ./gradlew assembleDebug)
# → android/app/build/outputs/apk/debug/app-debug.apk
```

- 매번 적기 번거롭다면 `.env.example`을 `.env.local`로 복사해 `VITE_API_BASE_URL`을 적어 둡니다.
- `android/app/google-services.json`이 없으면 휴대폰 알림만 꺼진 앱이 됩니다. 앱 안의 알림함은 그대로 동작합니다.
- 디버그 빌드는 에뮬레이터 확인용으로 `localhost`·`10.0.2.2`에 한해 HTTP를 허용합니다. 에뮬레이터에서 로컬 서버를 시험할 때는 `VITE_API_BASE_URL=http://10.0.2.2:8081`로 빌드합니다.
- 내 키로 서명한 APK를 내려면 [Android 빌드·배포](android.md#릴리스)를 봅니다.

## 6. 휴대폰 알림 켜기

서버와 앱이 **같은 Firebase 프로젝트**를 써야 합니다. 다르면 알림함에는 쌓여도 휴대폰으로는 가지 않습니다.

1. Firebase 콘솔에서 Android 앱(패키지 `dev.thsvkd.jari`)을 등록하고 `google-services.json`을 받아 앱을 빌드합니다(→ [Android 빌드·배포](android.md#휴대폰-알림-firebase)).
2. 같은 프로젝트의 서비스 계정 키로 `backend/.secrets/firebase-admin.json`을 덮어씁니다.
3. API를 다시 시작합니다. 서버는 시작할 때 한 번만 키를 읽습니다.
4. 앱의 **설정 → 휴대폰 알림**을 켭니다.

Docker 이미지에는 알림 의존성이 이미 들어 있습니다. Docker 없이 실행한다면 `uv sync --frozen --extra mobile-push`로 설치하고 `MOBILE_FCM_CREDENTIALS`에 키 경로를 적습니다. 전체 경로는 [SPEC §11](SPEC.md#11-android-빌드배포와-휴대폰-알림)에 있습니다.

## 7. 원격 호스트에 배포하기

[`scripts/deploy-backend.sh`](../scripts/deploy-backend.sh)는 SSH로 접속할 수 있는 Docker 호스트에 `backend/`를 배포합니다. 호스트 정보는 하드코딩하지 않고 플래그나 환경 변수로 받습니다.

```bash
./scripts/deploy-backend.sh \
  --host pi@example --root /srv/app \
  --compose-file compose.yaml --compose-file compose.overlay.example.yaml \
  --after "docker compose -f compose.overlay.example.yaml up -d proxy" \
  --dry-run   # 실제로 바꾸지 않고 계획만 출력합니다. 실행 전에 항상 먼저 확인합니다.
```

- 환경 변수로도 같은 값을 줄 수 있습니다: `DEPLOY_HOST`, `DEPLOY_ROOT`, `DEPLOY_REF`, `DEPLOY_COMPOSE_FILES`(쉼표 구분), `DEPLOY_SERVICE`, `DEPLOY_WORKER_PATTERN`.
- 게이트웨이·프록시 등록처럼 호스트마다 다른 후속 작업은 `--after "<원격 명령>"`으로 넘깁니다. 공백이 든 값도 원격에서 한 인자로 그대로 전달됩니다.
- 배포는 API를 다시 시작합니다. 스크립트는 재시작 전에 진행 중인 자리 찾기를 확인하고, 이어서 찾지 못할 검색이 있으면 거부합니다. 재시작 뒤에는 모든 검색이 새 컨테이너에서 다시 로그인해 찾는지 확인합니다(→ [SPEC §1.1](SPEC.md#11-재시작-중-검색-보존)).
- 스크립트 자체의 회귀 검사: `bash scripts/test-deploy-backend.sh`(가짜 `ssh`를 써서 실제로 접속하지 않습니다).

## 8. 백업과 데이터

- SQLite 볼륨(`identity`)과 Redis 볼륨(`redis`)을 **함께** 백업하고, `mobile.key`는 따로 보관합니다.
- `docker compose down --volumes`는 데이터를 지우므로 쓰지 않습니다.
- Redis는 compose에서 AOF(`--appendonly yes`)로 지속 저장합니다. 직접 띄운 Redis도 지속 저장을 켭니다.

## 9. 운영 명령 모음

모두 저장소 루트에서 실행합니다.

| 하려는 일 | 명령 |
|---|---|
| 상태 확인 | `curl http://127.0.0.1:8081/api/mobile/health` |
| 진행 중인 자리 찾기 목록(읽기만) | `docker compose exec -T api python -m korail_bot.mobile running` |
| 초대 코드 만들기 | `docker compose exec api python -m korail_bot.mobile invite --ttl-hours 24` |
| 메일로 온 탈퇴 요청 처리 | `docker compose exec -T api python -m korail_bot.mobile delete-account --username <앱 아이디>` |
| 앱이 겪은 연결 실패 보기 | `docker compose logs api 2>&1 \| grep "Client connection miss"` |

## 10. 외부 감시(선택)

[`monitor/`](../monitor/)는 1분마다 `/api/mobile/health`를 앱과 같은 경로로 불러 보고, 2분 연속 실패하면 텔레그램으로 장애를, 풀리면 복구를 알리는 Cloudflare Worker입니다. 배포는 `monitor/deploy.sh`(비밀값은 Doppler에서 꺼냄), 시험 알림은 `monitor/deploy.sh --test-message`입니다. 자세한 판정 기준은 [SPEC §6](SPEC.md#6-연결-상태)에 있습니다.
