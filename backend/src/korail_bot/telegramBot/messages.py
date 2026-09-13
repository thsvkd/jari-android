"""
텔레그램 봇 메시지 템플릿 관리

모든 봇 메시지를 중앙에서 관리하여 유지보수성을 향상시킵니다.
"""

from typing import ClassVar

from korail_bot.config.settings import settings


class Messages:
    """봇 메시지 템플릿 클래스"""

    # ========== 명령어 메뉴 ==========
    # Telegram 의 메뉴 버튼에 표시되는 목록. 명령어를 외워서 타이핑하는 대신
    # 골라서 쓰게 만드는 용도다.
    #
    # 두 벌인 이유는 메뉴가 방마다 다르게 붙기 때문이다. 예전에는 한 벌을
    # 모두에게 붙였고, 그래서 관리자 명령어를 넣는 순간 /flushredis 가 있다는
    # 사실을 전원에게 광고하는 셈이 되어 아예 뺄 수밖에 없었다. 운영자는
    # 자기 도구가 메뉴에 없는 채로 외워서 쳤다.
    #
    # 지금은 기본 목록을 모두에게 붙이고, 개발자 방에는 그 방만 범위로 하는
    # 전체 목록을 따로 붙인다. 각자 자기가 쓸 수 있는 것만 본다.
    PUBLIC_COMMANDS: ClassVar[list[dict[str, str]]] = [
        {"command": "start", "description": "🎫 예약 시작"},
        {"command": "fav", "description": "⭐ 즐겨찾기"},
        {"command": "status", "description": "📊 예약 상태 확인"},
        {"command": "tickets", "description": "🎫 잡아둔 표 확인·취소"},
        # 알림이 둘이라 이름만으로는 갈리지 않는다. /notify 가 둘 다
        # 보여주는 화면이고, /notify_off 는 그중 결제 쪽만 끄는 지름길이다.
        {"command": "notify", "description": "🔔 알림 설정 (검색·결제)"},
        {"command": "timezone", "description": "🌐 표시 시간대 확인·변경"},
        {"command": "notify_off", "description": "🔕 결제 재촉 알림만 끄기"},
        {"command": "onboarding", "description": "🔑 코레일 계정 등록"},
        {"command": "logout", "description": "🗑️ 등록된 계정 지우기"},
        # /cancel 은 메뉴에 없지만 계속 같은 곳으로 간다. 이미 보낸 안내
        # 문구와 사용자 채팅 기록에 /cancel 이 널려 있어서, 메뉴에서 뺀다고
        # 동작까지 빼면 그 문구들이 전부 거짓말이 된다.
        {"command": "stop", "description": "🛑 취소표 검색 중지"},
        {"command": "help", "description": "❓ 도움말"},
        {"command": "version", "description": "🤖 봇 버전과 가동 상태"},
        {"command": "about", "description": "ℹ️ 이 봇에 대해"},
    ]

    # 운영자 도구. 위험한 순서가 아니라 자주 쓰는 순서로 둔다. 메뉴에서
    # /flushredis 가 /approve 바로 옆에 있으면 잘못 누르기 좋다.
    ADMIN_COMMANDS: ClassVar[list[dict[str, str]]] = [
        {"command": "approve", "description": "🙋 사용 승인 요청 처리"},
        {"command": "users", "description": "👥 승인된 사용자 관리"},
        {"command": "allusers", "description": "📋 전체 사용자 확인"},
        {"command": "cancelall", "description": "🛑 전체 예약 취소"},
        {"command": "broadcast", "description": "📢 공지사항 전송"},
        {"command": "debug_on", "description": "🐛 상세 디버그 로그 켜기"},
        {"command": "debug_off", "description": "🐛 디버그 로그 끄기"},
        {"command": "devoff", "description": "🛠️ 개발자 모드 해제"},
        {"command": "flushredis", "description": "⚠️ Redis 초기화 (위험)"},
    ]

    #: 개발자 방에 붙는 목록. Telegram 은 방 범위 목록을 기본 목록과 합치지
    #: 않고 그것만 보여주므로, 공개 명령어까지 함께 담아야 한다.
    DEVELOPER_COMMANDS: ClassVar[list[dict[str, str]]] = PUBLIC_COMMANDS + ADMIN_COMMANDS

    # ========== 시작 및 안내 메시지 ==========
    MINI_APP_OFFER = """🚄 새 예약을 한 화면에서 설정할 수 있습니다.

날짜·구간·시간·좌석을 고른 뒤, 채팅에서 감시할 열차와 시작 여부만 확인합니다.
계정 정보와 결제 정보는 예약 화면에 입력하지 않습니다.

아래 버튼으로 열거나, 예전 방식이 편하면 채팅으로 진행하세요."""

    MINI_APP_RECEIVED = "✅ 예약 조건을 받았습니다. 감시할 열차를 확인합니다."
    MINI_APP_INVALID = """⚠️ 예약 화면에서 받은 정보를 사용할 수 없습니다.

{error}

/start 로 예약 화면을 다시 열거나 채팅으로 진행해주세요."""
    MINI_APP_ACCOUNT_REQUIRED = """🔑 코레일 계정이 등록되어 있지 않습니다.

선택한 예약 조건은 그대로 두었습니다. 계정을 등록하면 열차 선택으로 바로 이어집니다.

"""
    MINI_APP_LOGIN_EXPIRED = """🔑 등록된 계정으로 로그인할 수 없어 예약 조건을 적용하지 못했습니다.

선택한 예약 조건은 그대로 두었습니다. 아래에서 계정을 다시 등록해주세요."""
    MINI_APP_SEARCH_RUNNING = """🔍 이미 취소표를 찾고 있어 새 예약 조건은 적용하지 않았습니다.

/status 로 현재 검색을 확인하거나 /cancel 로 취소한 뒤 다시 시작해주세요."""

    WELCOME = """🚄 코레일 예매 봇을 이용해 주셔서 감사합니다.

본 프로그램은 매진 열차 자동 예약을 위한 서비스입니다.
예약 완료 시 결제는 10분 이내에 직접 진행해주셔야 합니다.

📋 예약 정보 입력 순서
━━━━━━━━━━━━━━━━
  1. 코레일 로그인 정보
  2. 출발 희망일
  3. 출발역
  4. 도착역
━━━━━━━━━━━━━━━━

계속 진행하시려면 "예" 또는 "Y"를 입력해주세요.
"""

    WELCOME_PRECONFIGURED = """🚄 코레일 예매 봇을 이용해 주셔서 감사합니다.

본 프로그램은 매진 열차 자동 예약을 위한 서비스입니다.
예약 완료 시 결제는 10분 이내에 직접 진행해주셔야 합니다.

🔑 코레일 계정이 서버에 설정되어 있어 로그인 정보는 묻지 않습니다.

📋 예약 정보 입력 순서
━━━━━━━━━━━━━━━━
  1. 출발 희망일
  2. 출발역
  3. 도착역
━━━━━━━━━━━━━━━━

계속 진행하시려면 "예" 또는 "Y"를 입력해주세요.
"""

    # ========== 온보딩 ==========

    # 등록을 마친 사람이 다시 /start 했을 때. 로그인 단계가 통째로 없어지는
    # 것이 이 기능의 전부이므로, 무엇이 생략됐고 어떻게 되돌리는지만 밝힌다.
    WELCOME_RETURNING = """🚄 코레일 예매 봇을 이용해 주셔서 감사합니다.

🔑 등록된 코레일 계정({korailId})으로 로그인했습니다.

📋 예약 정보 입력 순서
━━━━━━━━━━━━━━━━
  1. 출발 희망일
  2. 출발역
  3. 도착역
━━━━━━━━━━━━━━━━

다른 계정을 쓰시려면 /onboarding, 등록을 지우려면 /logout 을 입력하세요.
"""

    ONBOARDING_INTRO = """🔑 코레일 계정 등록

이 봇을 쓰려면 코레일 계정을 한 번 등록해야 합니다.
등록한 정보는 암호화해서 보관하며, 다음부터는 로그인 없이 바로 예약할 수 있습니다.

언제든 /logout 으로 지울 수 있고, 봇을 차단하거나 대화방을 삭제하면 자동으로 지워집니다.

계속하시려면 아래 버튼을 눌러주세요.
"""

    ONBOARDING_ALREADY = """⚠️ 이미 등록된 계정이 있습니다.

━━━━━━━━━━━━━━━━
등록된 계정: {korailId}
등록 시각: {onboardedAt}
━━━━━━━━━━━━━━━━

다시 등록하면 기존 정보를 덮어씁니다.
"""

    ONBOARDING_SAVED = """✅ 코레일 계정 등록이 끝났습니다.

다음부터는 /start 만 누르면 로그인 없이 바로 예약할 수 있습니다.
등록을 지우려면 /logout 을 입력하세요.
"""

    # 저장된 계정으로 로그인하지 못한 경우. 대개 코레일에서 비밀번호를 바꾼
    # 뒤이므로, 실패를 알리는 것으로 끝내지 않고 바로 재등록으로 잇는다.
    ONBOARDING_STALE = """⚠️ 등록된 계정으로 로그인하지 못했습니다.

코레일에서 비밀번호를 바꾸셨다면 다시 등록해야 합니다.
등록된 정보는 지웠으니, 아래 버튼으로 다시 등록해주세요.
"""

    LOGOUT_DONE = """🗑️ 등록된 코레일 계정을 지웠습니다.

다시 쓰시려면 /onboarding 으로 등록해주세요.
"""

    LOGOUT_NOTHING = """등록된 코레일 계정이 없습니다.

/onboarding 으로 등록할 수 있습니다.
"""

    # 서버에 계정이 박혀 있으면 개인 등록은 쓰이지 않는다. 등록을 받아두고
    # 무시하는 것보다 왜 필요 없는지 말해주는 편이 낫다.
    ONBOARDING_NOT_NEEDED = """ℹ️ 이 봇은 서버에 설정된 코레일 계정으로 동작합니다.

따로 계정을 등록하실 필요가 없습니다.
/start 로 바로 예약을 시작하세요.
"""

    # ========== SR 통합 (2026-09-01) ==========
    #
    # 등록해둔 SRT 계정이 더 이상 아무것도 못 하게 된 사람들에게 한 번 보낸다.
    # 그냥 두면 "검색해도 자리가 안 나온다" 로 보일 뿐이라, 왜 그런지 말해주지
    # 않으면 사용자가 알아낼 방법이 없다.

    SRT_RETIRED_KEEPS_KORAIL = """📢 SRT 예매가 코레일로 통합되었습니다

2026년 9월 1일부터 수서고속철도(SRT)는 별도 예매가 중단되고
코레일로 통합되었습니다. 수서·동탄·평택지제에서 출발하는 열차는
이제 KTX 로 코레일에서 예매합니다.

이에 따라 등록해두셨던 SRT 계정은 봇에서 삭제했습니다.
등록해두신 코레일 계정으로 그대로 이용하실 수 있고,
수서발 열차도 /start 에서 출발역에 수서를 입력하면 검색됩니다.

따로 하실 일은 없습니다."""

    SRT_RETIRED_NO_ACCOUNT = """📢 SRT 예매가 코레일로 통합되었습니다

2026년 9월 1일부터 수서고속철도(SRT)는 별도 예매가 중단되고
코레일로 통합되었습니다. 수서·동탄·평택지제에서 출발하는 열차는
이제 KTX 로 코레일에서 예매합니다.

이 봇에는 SRT 계정만 등록되어 있었습니다. 그 계정으로는 더 이상
예매할 수 없어 삭제했고, 지금은 예약을 진행할 수 없는 상태입니다.

▶️ 다시 이용하시려면
1. SR 에만 가입되어 있었다면 코레일 통합 회원가입이 필요합니다.
   (코레일과 SR 양쪽에 가입되어 있었다면 회원 통합 동의만 하면 됩니다.)
2. /onboarding 으로 코레일 계정을 등록해주세요.

등록이 끝나면 수서발 열차도 그대로 검색됩니다."""

    # ========== 체험과 사용 승인 ==========

    TRIAL_REMAINING = """🎟️ 체험 {used}/{limit} 회를 사용했습니다.

체험 횟수를 모두 쓰신 뒤에는 운영자 승인이 필요합니다.
"""

    TRIAL_EXHAUSTED = """🚫 체험 횟수를 모두 사용하셨습니다 ({used}/{limit})

이 봇은 개인이 운영하는 서버에서 돌아갑니다. 검색 하나가 코레일에 몇 초마다
요청을 보내기 때문에, 계속 쓰시려면 운영자의 승인이 필요합니다.

아래 버튼을 누르면 운영자에게 요청이 전달됩니다.
"""

    ACCESS_REQUEST_SENT = """✅ 승인 요청을 보냈습니다.

운영자가 승인하면 알려드리겠습니다.
"""

    ACCESS_REQUEST_ALREADY = """이미 승인 요청이 접수되어 있습니다.

운영자가 확인하면 알려드리겠습니다.
"""

    ACCESS_REQUEST_NO_ACCOUNT = """등록된 코레일 계정이 없습니다.

/onboarding 으로 먼저 계정을 등록해주세요.
"""

    ACCESS_REQUEST_NOTICE = """🙋 새 사용 승인 요청

번호: {maskedPhone}
요청 시각: {requestedAt}

/approve 로 처리할 수 있습니다.
"""

    ACCESS_APPROVED = """🎉 사용이 승인되었습니다.

이제 제한 없이 이용하실 수 있습니다. /start 로 예약을 시작하세요.
"""

    ACCESS_REJECTED = """죄송합니다. 이번에는 사용 승인이 이루어지지 않았습니다.

운영자에게 직접 문의해보실 수 있습니다.
"""

    # ========== 개발자 모드 ==========

    DEVELOPER_ON = """🛠️ 이 채팅방을 개발자 모드로 전환했습니다.

이제 이 방에서는:
  • 체험 횟수 제한이 없습니다
  • 관리자 명령을 비밀번호 없이 쓸 수 있습니다
  • 서버에 코레일 계정이 설정되어 있으면 그 계정으로 로그인합니다
  • 새 사용 승인 요청 알림을 받습니다

/approve 승인 요청 처리 · /users 승인된 사용자 관리
해제하려면 /devoff 를 입력하세요.
"""

    DEVELOPER_ALREADY = "이미 개발자 모드입니다.\n해제하려면 /devoff 를 입력하세요."

    DEVELOPER_OFF = """개발자 모드를 해제했습니다.

이제 이 방은 일반 사용자와 동일하게 동작합니다.
"""

    DEVELOPER_NOT_ON = "이 채팅방은 개발자 모드가 아닙니다."

    # 조용히 일어나서는 안 되는 일이다. 매직 문자열은 틀린 시도를 셀 수
    # 없으므로, 성공을 숨길 수 없게 만드는 것이 방어가 된다.
    DEVELOPER_NEW_NOTICE = """⚠️ 다른 채팅방이 개발자 모드로 전환되었습니다.

본인이 한 것이 아니라면 ADMIN_MAGIC_STRING 이 노출된 것입니다.
서버의 .env 에서 값을 바꾸고 재시작하세요.
"""

    # ========== 승인 관리 (운영자) ==========

    SERVER_BUSY = """⏳ 지금은 검색을 시작할 수 없습니다.

이 서버에서 동시에 돌 수 있는 검색이 {limit}개인데 모두 사용 중입니다.
코레일에 한꺼번에 너무 많은 요청을 보내면 서버 전체가 차단될 수 있어
걸어둔 제한입니다.

잠시 후 /start 로 다시 시도해주세요.
"""

    APPROVE_EMPTY = "대기 중인 승인 요청이 없습니다."

    APPROVE_LIST = """🙋 승인 대기 중인 요청 ({count}건)

처리할 요청을 선택하세요.
"""

    APPROVE_CONFIRM = """{maskedPhone} 을(를) 승인할까요?

요청 시각: {requestedAt}
"""

    APPROVE_DONE = "✅ {maskedPhone} 을(를) 승인했습니다."
    APPROVE_REJECTED = "🚫 {maskedPhone} 의 요청을 거절했습니다."
    APPROVE_GONE = "이미 처리된 요청입니다."

    USERS_EMPTY = """승인된 사용자가 없습니다.

.env 의 PREAPPROVED_USERS 로 미리 승인된 번호는 여기 표시되지 않습니다.
"""

    USERS_LIST = """👥 승인된 사용자 ({count}명)

승인을 취소하려면 선택하세요.
(.env 의 PREAPPROVED_USERS 로 미리 승인된 번호는 표시되지 않습니다)
"""

    USERS_REVOKE_CONFIRM = """{maskedPhone} 의 승인을 취소할까요?

취소해도 이미 쓴 체험 횟수는 복구되지 않으므로, 다시 쓰려면 승인이 필요합니다.
"""

    USERS_REVOKED = "🚫 {maskedPhone} 의 승인을 취소했습니다."
    USERS_REVOKE_GONE = "이미 승인이 취소된 사용자입니다."

    # 도움말도 방에 따라 다르다. 쓸 수 없는 명령어를 늘어놓는 도움말은 읽는
    # 사람이 자기에게 해당하는 줄을 골라내야 하는 목록이고, 그러면서 서버에
    # 어떤 위험한 문이 있는지 알려준다.
    HELP = """📌 사용 가능한 명령어

🎫 예약 관련
  /start - 예약 시작
  /fav - 즐겨찾기 (자주 타는 구간 저장·불러오기)
  /stop - 취소표 검색 중지
  /tickets - 잡아둔 표 확인·취소

🔑 계정
  /onboarding - 코레일 계정 등록 (최초 1회, /init 도 같음)
  /logout - 등록된 계정 지우기

ℹ️ 정보 확인
  /status - 지금 검색이 돌고 있는지
  /timezone - 표시 시간대 확인·변경
  /help - 도움말 보기
  /version - 봇 버전과 가동 상태
  /about - 이 봇에 대해

🔔 알림은 두 가지입니다
  /notify 둘 다 보고 끄는 화면
  ① 검색 진행 알림 - 검색이 도는 동안 정해둔 간격마다 (기본 꺼짐)
  ② 결제 재촉 알림 - 좌석을 잡은 뒤 결제 기한까지 · /notify_off 로 끄기

🚫 "취소"도 세 가지입니다
  /stop 검색 중지 (잡아둔 좌석은 그대로)
  /tickets 결제 전 좌석 놓아주기
  발권된 표 무르기(환불)는 코레일톡·SR 앱에서 — 봇은 하지 않습니다

💡 결제하시면 봇이 직접 확인해서 알려드립니다. 재촉 알림만 끄려면 /notify_off.
"""

    HELP_ADMIN_SECTION = """
🔧 관리자 명령어
  /approve - 사용 승인 요청 처리
  /users - 승인된 사용자 관리
  /allusers - 전체 사용자 확인
  /cancelall - 전체 예약 취소
  /broadcast [메시지] - 공지사항 전송
  /debug_on - 상세 디버그 로그 활성화
  /debug_off - 디버그 로그 비활성화
  /devoff - 개발자 모드 해제
  /flushredis - Redis 메모리 초기화 (⚠️ 위험)
"""

    # ========== 로그인 관련 메시지 ==========
    REQUEST_PHONE = """📱 코레일 로그인 정보 입력을 시작합니다.

현재 휴대폰 번호 로그인만 지원됩니다.
(코레일 회원번호나 이메일로는 로그인할 수 없습니다)

휴대전화번호를 입력해 주세요.
예시: 010-1234-5678 또는 01012345678

💡 하이픈(-)은 있어도 없어도 됩니다.
💡 취소를 원하시면 /cancel을 입력하세요.
"""

    REQUEST_PASSWORD = """✅ 휴대폰 번호 입력 완료

🔒 코레일 계정 비밀번호를 입력해주세요.
(코레일톡·홈페이지 로그인에 쓰는 그 비밀번호입니다)

⚠️ 입력한 비밀번호는 이 대화 기록에 남습니다.
   입력 후 해당 메시지를 삭제하시는 것을 권합니다.
"""

    # 날짜 예시는 오늘이다. 예전에는 20250425 가 그대로 박혀 있었는데,
    # 그 날짜가 과거가 된 뒤로는 시키는 대로 따라 친 사람이 "과거 날짜는
    # 입력할 수 없습니다"를 받았다. 예시가 곧 기본값 노릇을 하므로,
    # 기본값은 오늘이어야 한다.
    LOGIN_SUCCESS = """✅ 로그인 성공!

📅 출발 희망일을 8자리로 입력해주세요.
예시: {example}
"""

    LOGIN_SUCCESS_PRECONFIGURED = """✅ 서버에 설정된 코레일 계정으로 로그인했습니다. ({username})

📅 출발 희망일을 8자리로 입력해주세요.
예시: {example}
"""

    PRECONFIGURED_LOGIN_FAILED = """⚠️ 서버에 설정된 코레일 계정으로 로그인하지 못했습니다.

비밀번호가 변경되었을 수 있습니다. 직접 입력하는 방식으로 진행합니다.

📱 휴대전화번호를 입력해 주세요.
예시: 010-1234-5678 또는 01012345678

💡 하이픈(-)은 있어도 없어도 됩니다.
💡 취소를 원하시면 /cancel을 입력하세요.
"""

    LOGIN_FAILED_RETRY = """❌ 로그인 실패

입력하신 정보:
━━━━━━━━━━━━━━
아이디: {username}
비밀번호: 보안상 비공개
━━━━━━━━━━━━━━

다음 중 하나를 선택해주세요:
  • Y 또는 예 → 계정정보 다시 입력
  • N 또는 아니오 → 작업 취소
  • 비밀번호만 다시 입력 → 같은 아이디로 재시도

⚠️ 주의: 5회 이상 로그인 실패 시 코레일 홈페이지에서 비밀번호를 재설정해야 합니다.
"""

    # ========== 예약 정보 입력 메시지 ==========
    # 역 목록 주소는 설정에서 가져온다. 코레일이 사이트를 개편하면서 예전
    # 주소가 죽은 적이 있는데, 같은 주소를 여기 두 번 적어두면 그때 고칠
    # 곳이 세 군데가 된다.
    REQUEST_DATE = f"""✅ 출발일 입력 완료

🚉 출발역을 입력해주세요.
예시: 광명, 서울, 부산 등

💡 역 이름만 입력 ('역' 제외)
📍 역 목록: {settings.KORAIL_STATION_LIST_URL}
"""

    REQUEST_SRC_STATION = f"""✅ 출발역 입력 완료

🏁 도착역을 입력해주세요.
예시: 광주송정, 대전, 동대구 등

💡 역 이름만 입력 ('역' 제외)
📍 역 목록: {settings.KORAIL_STATION_LIST_URL}
"""

    REQUEST_DST_STATION = """✅ 도착역 입력 완료

🕐 검색 시작 시각을 입력해주세요.

🌐 열차 검색 시각은 대한민국 철도 시각(KST, UTC+09:00) 기준입니다.

형식: HHMM (24시간 기준, 4자리)
예시: 1305 (오후 1시 5분 이후 열차 검색)
"""

    REQUEST_DEP_TIME = """✅ 검색 시작 시각 입력 완료

🕐 검색 종료 시각을 입력해주세요.

🌐 대한민국 철도 시각(KST, UTC+09:00) 기준입니다.

형식: HHMM (24시간 기준, 4자리)
예시: 1800 (오후 6시까지의 열차만 검색)

💡 시간 제한 없이 검색하려면 2400 입력 (권장)
"""

    # "모든 열차" 가 무엇을 포함하는지 밝혀둔다. 이름만 보면 "더 많이 찾아
    # 주는 쪽" 으로 읽히지만, 실제로는 무궁화호까지 후보에 들어간다는 뜻이다.
    # 검색은 먼저 잡히는 자리를 잡으므로 KTX 를 기대하고 골랐다가 두 시간
    # 더 걸리는 열차를 예약당할 수 있는데, 그것은 표를 못 구하는 것과는
    # 다른 종류의 실패다. 고르기 전에 알아야 하는 정보다.
    REQUEST_TRAIN_TYPE = """✅ 시간 입력 완료

🚄 열차 종류를 선택해주세요.

━━━━━━━━━━━━━━━━━━━━
1️⃣ KTX 계열만
   • KTX, KTX-산천, KTX-이음
   • 빠른 열차만 원할 때

2️⃣ 모든 열차
   • 위 KTX 계열 + ITX-새마을 · 새마을호
     + 무궁화호 · 누리로 + ITX-청춘 + 통근열차
   • 코레일이 파는 좌석 전부가 후보가 됩니다
━━━━━━━━━━━━━━━━━━━━

⚠️ "모든 열차" 는 **먼저 나오는 자리**를 잡습니다.
   같은 구간에 무궁화호가 함께 다니면, KTX 를 기다리는 대신
   두 시간 더 걸리는 열차가 예약될 수 있습니다.

💡 표만 구하면 되는 상황이면 2️⃣, 시간이 중요하면 1️⃣ 입니다.
   특정 열차만 노리려면 뒤에서 열차를 직접 고를 수도 있습니다.

숫자를 입력하세요: 1 또는 2
"""

    # 좌석 질문의 본문. 앞의 "✅ ... 완료" 한 줄만 단계마다 다르다 - 예전 SRT 는
    # 열차 종류를 고른 적이 없으므로 고르지도 않은 것을 완료했다고 말할 수
    # 없고, 방금 답한 시간대를 짚어주는 편이 맞다.
    _SEAT_TYPE_BODY = """💺 좌석 종류를 선택해주세요.

1️⃣ 일반실 우선
2️⃣ 일반실만
3️⃣ 특실 우선
4️⃣ 특실만

숫자를 입력하세요: 1, 2, 3, 4
"""

    REQUEST_SEAT_TYPE = "✅ 열차 종류 선택 완료\n\n" + _SEAT_TYPE_BODY

    REQUEST_SEAT_TYPE_AFTER_TIME = "✅ 시간 입력 완료\n\n" + _SEAT_TYPE_BODY

    REQUEST_PASSENGER_COUNT = """✅ 좌석 종류 선택 완료

👥 탑승 인원수를 입력해주세요.

💡 1~9명까지 선택 가능합니다.
(현재는 성인 인원수만 지원합니다)

예) 2명이 탑승하는 경우: 2
"""

    REQUEST_SEAT_STRATEGY = """✅ 인원수 입력 완료 (총 {count}명)

🪑 좌석 배치 방식을 선택해 주십시오.

━━━━━━━━━━━━━━━━━━━━
1️⃣ 연속 좌석 (권장)
   • 같이 앉을 수 있도록 연속된 좌석 예약
   • 연속된 좌석이 없으면 예약 실패

2️⃣ 랜덤 배치
   • 한 자리씩 개별적으로 예약
   • 좌석이 떨어져 있을 수 있음
   • 예약 성공률이 더 높음
━━━━━━━━━━━━━━━━━━━━

숫자를 입력하세요: 1 또는 2
"""

    SELECT_TRAINS = """✅ 좌석 배치 선택 완료

🚄 감시할 열차를 선택해주세요.

📍 {srcLocate} → {dstLocate}
📅 {depDate}  🕐 {depTime}~{maxDepTime} KST (대한민국 철도 시각)
━━━━━━━━━━━━━━━━━━━━
조회된 열차 {count}개{truncated}

원하는 열차를 눌러 선택하세요. 여러 개 고를 수 있습니다.
선택한 열차에 취소표가 나오면 바로 잡습니다.

💡 특정 열차를 고집할 이유가 없다면 **시간대 전체 감시**가
   성공률이 훨씬 높습니다.
━━━━━━━━━━━━━━━━━━━━

⌨️ 열차번호를 직접 입력해도 됩니다 (예: 101 105)"""

    SELECT_TRAINS_TRUNCATED = "\n⚠️ 너무 많아 앞의 {shown}개만 표시합니다"

    TRAIN_LIST_EMPTY = """⚠️ 해당 시간대에 운행하는 열차가 없습니다.

조건에 맞는 열차를 찾지 못했습니다. 날짜나 시간대를 바꿔
다시 시도해보세요.

시간대 전체를 감시하는 방식으로 계속할 수도 있습니다."""

    TRAIN_LIST_FAILED = """⚠️ 열차 목록을 불러오지 못했습니다.

코레일 응답을 받지 못했습니다. 열차를 골라서 감시하는 대신
시간대 전체를 감시하는 방식으로 진행합니다.

(전체 감시는 원래 이 봇의 기본 동작이며 성공률이 더 높습니다)"""

    TRAIN_SELECT_UNKNOWN = """❌ '{value}' 은(는) 목록에 없는 열차번호입니다.

목록의 버튼을 누르거나, 표시된 열차번호를 입력해주세요."""

    CONFIRM_RESERVATION = """✅ 모든 정보 입력 완료!

📋 예약 정보 확인
━━━━━━━━━━━━━━━━━━━━
📅 출발일: {depDate}
🚉 출발역: {srcLocate}
🏁 도착역: {dstLocate}
🕐 검색 시작: {depTime} KST
⏰ 검색 종료: {maxDepTime} KST
🚄 열차: {trainTypeShow}
💺 좌석: {specialInfoShow}
👥 인원: {passengerCount}명
🪑 배치: {seatStrategy}
🔢 좌석: {seatPreference}
🎯 감시: {trainWatch}
━━━━━━━━━━━━━━━━━━━━

• Y 또는 예 → 예약 시작
• N 또는 아니오 → 작업 취소

⏱ 예약 완료까지 시간이 걸릴 수 있습니다.
"""

    # ========== 이미 지나온 질문을 다시 묻기 ==========
    #
    # 앞으로 갈 때 쓰는 안내문은 하나같이 "✅ ... 입력 완료" 로 시작하는데,
    # 되돌아온 사람에게 방금 지운 답을 완료했다고 말하는 셈이라 그대로
    # 재사용할 수 없다.
    #
    # 왜 이 질문이 다시 떴는지는 앞에 붙는 한 줄이 말한다. 뒤로가기로 왔을
    # 수도 있고, 하다 만 예약을 이어받아 왔을 수도 있어서 문구 자체는 이유를
    # 담지 않는다.
    #
    # 열차 선택과 최종 확인 화면은 예외다. 그 두 화면은 지금까지의 답을
    # 모아서 스스로 문구를 만들기 때문에, 되돌아왔을 때도 그대로 다시 그리면
    # 된다.
    BACK_PREFIX = "◀️ 이전 단계로 돌아왔습니다.\n\n"
    RESUME_PREFIX = "▶️ 하던 곳에서 이어갑니다.\n\n"

    ASK_AGAIN_PHONE = """📱 휴대전화번호를 다시 입력해 주세요.
예시: 010-1234-5678 또는 01012345678

💡 하이픈(-)은 있어도 없어도 됩니다."""

    ASK_AGAIN_DATE = """📅 출발 희망일을 다시 선택해주세요.
예시: {example}"""

    ASK_AGAIN_SRC_STATION = "🚉 출발역을 다시 선택해주세요."

    ASK_AGAIN_DST_STATION = "🏁 도착역을 다시 선택해주세요."

    ASK_AGAIN_DEP_TIME = """🕐 검색 시작 시각을 다시 선택해주세요.
예시: 1305 (오후 1시 5분 이후 열차 검색)"""

    ASK_AGAIN_MAX_DEP_TIME = """🕐 검색 종료 시각을 다시 선택해주세요.
예시: 1800 (오후 6시까지의 열차만 검색)

💡 시간 제한 없이 검색하려면 2400 입력 (권장)"""

    ASK_AGAIN_TRAIN_TYPE = """🚄 열차 종류를 다시 선택해주세요.

1️⃣ KTX 계열만 (KTX · KTX-산천 · KTX-이음)
2️⃣ 모든 열차 (무궁화호 · 새마을호까지 포함)

⚠️ 2️⃣ 는 먼저 나오는 자리를 잡으므로, 무궁화호가 예약될 수 있습니다."""

    ASK_AGAIN_SEAT_OPTION = """💺 좌석 종류를 다시 선택해주세요.

1️⃣ 일반실 우선
2️⃣ 일반실만
3️⃣ 특실 우선
4️⃣ 특실만"""

    ASK_AGAIN_PASSENGER_COUNT = """👥 탑승 인원수를 다시 선택해주세요.

💡 1~9명까지 선택 가능합니다."""

    # 좌석 조건 화면. 열은 눌러서 켜고 끄고, 번호 범위는 직접 입력한다.
    # 조건이 좁을수록 놓치는 표가 늘어난다는 것을 먼저 말해둔다 - 눌러본 뒤에
    # 알게 되면 이미 몇 시간을 버린 뒤다.
    REQUEST_SEAT_PREFERENCE = """🪑 원하시는 좌석을 정해주세요. (선택 사항)

━━━━━━━━━━━━━━━━━━━━
현재 조건: {current}
━━━━━━━━━━━━━━━━━━━━

• 아래에서 원하는 열을 눌러 고르세요 (여러 개 가능)
   A · D = 창측,  B · C = 내측
• 좌석 번호 범위는 직접 입력하세요 (예: 1-15)

⚠️ 조건이 좁을수록 맞지 않는 좌석은 돌려보내므로
   예약 성공까지 더 오래 걸립니다.
   {limit}번까지 놓치면 그때 잡힌 좌석으로 확정합니다."""

    ASK_AGAIN_SEAT_PREFERENCE = """🪑 좌석 조건을 다시 정해주세요.

현재 조건: {current}

열을 눌러 고르시거나, 번호 범위를 입력하세요 (예: 1-15)."""

    ASK_AGAIN_SEAT_STRATEGY = """🪑 좌석 배치 방식을 다시 선택해주세요. (총 {count}명)

1️⃣ 연속 좌석 (권장) - 붙어 앉는 대신 실패할 수 있음
2️⃣ 랜덤 배치 - 떨어져 앉는 대신 성공률이 높음"""

    # 첫 질문에서 뒤로를 누른 경우. 조용히 무시하면 버튼이 고장 난 것처럼
    # 보이므로, 왜 아무 일도 일어나지 않는지는 말해준다.
    BACK_AT_THE_START = """◀️ 더 돌아갈 단계가 없습니다.

처음부터 다시 하시려면 /cancel 후 /start 를 입력해주세요."""

    # 예약을 시작할 차례인데 세션에 로그인 정보가 없는 경우. 등록된 계정이
    # 있으면 그것으로 이어가므로 여기까지 오지 않고, 등록도 없을 때만 나온다.
    # 예전에는 이 상태에서 봇이 예외로 죽어 사용자는 아무 답도 받지 못했다.
    NO_CREDENTIALS = """⚠️ 로그인 정보를 찾지 못했습니다.

/start 로 다시 시작해주세요."""

    # ========== 하다 만 예약 ==========
    #
    # 질문이 열 개가 넘는 흐름이라 도중에 자리를 뜨는 일이 흔하다. 그 답들은
    # 세션에 그대로 남아 있는데, 예전에는 /start 가 아무 말 없이 그 위를 덮고
    # 처음부터 다시 물었다.
    RESUME_DRAFT = """📝 진행하시던 예약이 남아 있습니다

📍 {srcLocate} → {dstLocate}
📅 {depDate}
🕐 {depTime}~{maxDepTime}
👥 {passengerCount}명
❓ 다음 질문: {question}

이어서 진행할까요?
처음부터 다시 하시면 지금까지의 답은 사라집니다."""

    # 이어받기가 실패하는 유일한 경우. 저장된 계정이 없으면 다시 로그인할
    # 방법이 없어 답만 남고 그것으로 할 수 있는 일이 없다.
    RESUME_DRAFT_NO_ACCOUNT = """⚠️ 저장된 로그인 정보가 없어 이어서 진행할 수 없습니다.

계정을 등록한 뒤 다시 시작해주세요."""

    # ========== 결제를 기다리는 예약 ==========
    #
    # /status 는 검색 이야기만 했는데, 좌석을 잡고 결제를 기다리는 중이라면
    # 그것이야말로 지금 사용자에게 시간이 걸린 일이다.
    PAYMENT_PENDING_STATUS = """💳 결제를 기다리는 예약이 있습니다.
━━━━━━━━━━━━━━━━━━━━
{lines}
━━━━━━━━━━━━━━━━━━━━
🔗 결제: {paymentUrl}

결제하지 않으실 거라면 아래 버튼으로 좌석을 놓아줄 수 있습니다.
🎫 발권 완료된 표까지 함께 보려면 /tickets"""

    PAYMENT_CANCEL_CONFIRM = """🚫 정말 예약을 취소할까요?

{lines}

취소하면 좌석은 즉시 다른 사람에게 돌아갑니다.
되돌릴 수 없습니다."""

    PAYMENT_CANCEL_DONE = """예약을 취소했어요.

{lines}

좌석은 다시 판매될 수 있어요.
다시 찾으려면 새 검색을 시작해 주세요."""

    PAYMENT_CANCEL_KEPT = "예약을 그대로 두었습니다.\n결제 기한 안에 결제를 완료해주세요."

    PAYMENT_CANCEL_NOTHING = """결제를 기다리는 예약이 없습니다.

이미 결제하셨거나 기한이 지나 자동으로 취소된 것일 수 있습니다."""

    # ==================== /tickets ====================
    #
    # 한 화면에 결제 전 좌석과 발권된 표를 나란히 놓는다. 둘을 갈라놓은
    # 것이 요점이다 — 봇이 놓아줄 수 있는 것은 위쪽뿐이고, 아래쪽을 무르는
    # 것은 수수료가 붙는 환불이라 철도사 앱에서 사람이 해야 한다.

    TICKETS_HEADER = "🎫 표 현황"

    TICKETS_PENDING_SECTION = """💳 결제 전 — 좌석만 잡아둔 상태
━━━━━━━━━━━━━━━━━━━━
{lines}
━━━━━━━━━━━━━━━━━━━━
🔗 결제: {paymentUrl}

기한 안에 결제하지 않으면 좌석은 자동으로 풀립니다."""

    TICKETS_ISSUED_SECTION = """🎟️ 발권 완료 — 결제까지 끝난 표
━━━━━━━━━━━━━━━━━━━━
{lines}
━━━━━━━━━━━━━━━━━━━━
이 표를 무르시려면 코레일톡·SR 앱에서 환불하셔야 합니다.
수수료가 붙고 되돌릴 수 없어서, 봇은 발권된 표를 건드리지 않습니다."""

    # 조회 자체가 안 된 철도사는 "표 없음"에 섞지 않고 이름을 밝힌다.
    # "표가 없습니다"와 "아무도 안 봤습니다"는 다른 말이고, 둘을 합치면
    # 있는 표를 없다고 하게 된다.
    TICKETS_UNREACHABLE = """⚠️ 발권된 표는 조회하지 못했습니다.

로그인에 실패했거나 코레일이 응답하지 않았습니다.
표가 없다는 뜻이 아니니, 코레일톡에서 직접 확인해주세요."""

    TICKETS_NOTHING = """🎫 표 현황

잡아둔 좌석도, 발권된 표도 없습니다.

계정을 등록하지 않으셨다면 /onboarding 으로 먼저 등록해주세요.
표를 잡으시려면 /start 로 검색을 시작하세요."""

    # 취소를 시도했지만 철도사가 받아주지 않은 경우. 봇이 못 했다고 해서
    # 예약이 사라진 것은 아니므로, 직접 취소할 곳을 알려주는 편이 낫다.
    PAYMENT_CANCEL_FAILED = """❌ 예약을 취소하지 못했습니다.

{reason}

🔗 직접 취소: {paymentUrl}
결제하지 않고 두면 기한이 지난 뒤 자동으로 취소됩니다."""

    # 랜덤 배치는 좌석을 하나씩 잡아 나간다. 여기서 좌석을 돌려주면 검색이
    # 곧바로 다음 좌석을 잡아, 취소했다는 말과 함께 새 예약이 생긴다.
    PAYMENT_CANCEL_MID_RUN = """⚠️ 아직 좌석을 잡는 중입니다.

지금 예약을 취소하면 검색이 곧바로 다음 좌석을 잡습니다.
/cancel 로 검색을 먼저 멈춘 뒤 다시 시도해주세요."""

    PAYMENT_CANCEL_NO_ACCOUNT = "저장된 로그인 정보가 없어 봇이 대신 취소할 수 없습니다."
    PAYMENT_CANCEL_LOGIN_FAILED = "철도사에 로그인하지 못했습니다."
    PAYMENT_CANCEL_REFUSED = "철도사가 취소 요청을 받아주지 않았습니다."

    # ========== 검색 시작 시각 예약 ==========
    REQUEST_SCHEDULE = """⏰ 검색을 시작할 시각을 선택해주세요.

📍 {srcLocate} → {dstLocate}  📅 {depDate}

지금 시작하는 대신 정해진 시각에 검색을 시작합니다.
명절 예매 오픈 시각처럼 표가 풀리는 때를 노릴 때 씁니다.

🌐 입력 시간대: {timeZone}
다른 시간대를 쓰려면 /timezone 지역/도시 로 먼저 바꿔주세요.

⌨️ 직접 입력도 됩니다
   0700         → 다음 07:00
   0801 0700    → 8월 1일 07:00
"""

    SCHEDULE_CONFIRMED = """⏰ 검색이 예약되었습니다

🕐 시작 시각: {startAt}
📍 {srcLocate} → {dstLocate}
📅 {depDate}  {depTime}~{maxDepTime}
🎯 감시: {trainWatch}

그때가 되면 자동으로 검색을 시작하고 알려드립니다.
그전까지는 아무 요청도 보내지 않습니다.

💡 취소하려면 /cancel, 확인하려면 /status
"""

    SCHEDULE_STARTING = """⏰ 예약해두신 시각이 되어 검색을 시작합니다

📍 {srcLocate} → {dstLocate}
📅 {depDate}

취소표가 나오면 바로 알려드립니다.
"""

    SCHEDULE_MISSED = """⚠️ 예약해둔 검색을 시작하지 못했습니다

🕐 예정 시각: {startAt}
📍 {srcLocate} → {dstLocate}

그 시각에 봇이 꺼져 있었고, 지금 시작하기에는 너무 늦었습니다.
필요하시면 /start 로 다시 시작해주세요.
"""

    SCHEDULE_NO_CREDENTIALS = """⚠️ 예약해둔 검색을 시작하지 못했습니다

저장된 로그인 정보가 만료되었습니다.
/start 로 다시 시작해주세요.
"""

    SCHEDULE_IN_THE_PAST = "❌ 이미 지난 시각입니다. 앞으로의 시각을 선택해주세요."

    TIMEZONE_STATUS = """🌐 현재 표시 시간대: {timeZone}
🕐 현재 시각: {now}

Mini App을 열면 기기의 시간대로 자동 갱신됩니다.
직접 바꾸려면 아래처럼 IANA 지역명을 입력해주세요.
/timezone Europe/London"""

    TIMEZONE_CHANGED = """✅ 표시 시간대를 바꿨습니다.

🌐 {timeZone}
🕐 현재 시각: {now}

열차 검색 조건은 철도사 기준인 대한민국 시간으로 입력하며, 결과와 각종 기한은 이 시간대로 표시합니다."""

    TIMEZONE_INVALID = """❌ 시간대를 알아볼 수 없습니다: {value}

예: /timezone Asia/Seoul
    /timezone America/New_York
    /timezone Europe/London"""

    SCHEDULE_TOO_FAR = """❌ 너무 먼 미래입니다.

로그인 정보 보관 기한 때문에 최대 {days}일 뒤까지만 예약할 수 있습니다."""

    SCHEDULE_AFTER_DEPARTURE = """❌ 열차 출발({departure}) 이후에는 검색할 수 없습니다.

출발 전 시각을 선택해주세요."""

    SCHEDULE_UNPARSEABLE = """❌ 시각을 알아듣지 못했습니다: {value}

이렇게 입력해주세요.
   0700         → 다음 07:00
   0801 0700    → 8월 1일 07:00
   202608010700 → 2026년 8월 1일 07:00"""

    RESERVATION_STARTED = """취소표 대기를 시작했어요.

선택한 열차와 좌석 범위에서 빈자리가 생기는지 계속 확인할게요.
좌석을 예약하면 바로 알려드려요.

중지하려면 앱의 ‘검색 중지’ 버튼을 눌러 주세요."""

    # ========== 진행 상황 보고 ==========
    #
    # 검색은 몇 시간을 아무 말 없이 돌 수 있는데, 사용자 입장에서 그 침묵은
    # 죽은 프로세스와 구별되지 않는다. /notify 로 켜면 정해둔 간격마다
    # 살아있다고 알린다. 기본은 꺼짐이다. 원하지 않은 알림이 5분마다 오는
    # 것은 그 침묵보다 나쁘기 때문이다.
    SEARCH_PROGRESS = """🔍 검색 진행 중 ({elapsed}째)

📍 {srcLocate} → {dstLocate}
📅 {depDate}  🕐 {depTime}~{maxDepTime} KST (대한민국 철도 시각)
🎯 {watch}
━━━━━━━━━━━━━━━━━━━━
조회 {attempts}회
{health}

💡 알림을 끄려면 /notify off · 중단하려면 /cancel"""

    # 알림은 둘이고 서로 다른 것을 건드린다. 한 화면에 나란히 놓고 각각
    # 무엇에 대한 알림인지 밝힌다 — /notify 와 /notify_off 가 짝처럼 생겨서
    # 헷갈렸던 것이 이 화면을 이렇게 만든 이유다.
    NOTIFY_SETTINGS = """🔔 알림 설정

1️⃣ 검색 진행 알림 — 지금: {current}
   검색이 도는 동안 정해둔 간격마다 "아직 찾는 중" 이라고 알려줍니다.
   몇 시간짜리 검색이 살아있는지 보려고 /status 를 반복해 칠 필요가
   없어집니다. 아래 버튼이 이 알림의 간격입니다.

2️⃣ 결제 재촉 알림 — 지금: {payment}
   좌석을 잡은 뒤 결제 기한까지 재촉하는 알림입니다. 검색과는 상관이
   없고, /notify_off 로도 끌 수 있습니다.
   끄더라도 결제 여부는 계속 확인해서 알려드립니다.

⌨️ 직접 정할 수도 있습니다
   /notify 10    → 검색 알림 10분마다
   /notify off   → 검색 알림 끄기

(설정할 수 있는 범위: {min}~{max}분)"""

    NOTIFY_PAYMENT_ACTIVE = "켜짐 (결제 기한까지)"
    NOTIFY_PAYMENT_INACTIVE = "꺼짐"
    NOTIFY_PAYMENT_NONE = "해당 없음 (결제를 기다리는 예약이 없습니다)"

    NOTIFY_ON = """🔔 진행 상황 알림을 켰습니다.

{minutes}분마다 검색이 어떻게 되어가는지 알려드립니다.
지금 돌고 있는 검색에도 곧 적용됩니다.

끄려면 /notify off 를 입력하세요."""

    NOTIFY_OFF = """🔕 진행 상황 알림을 껐습니다.

검색은 그대로 진행되며, 예약에 성공하면 알려드립니다.
다시 켜려면 /notify 를 입력하세요."""

    NOTIFY_ALREADY_OFF = "이미 꺼져 있습니다.\n\n켜려면 /notify 를 입력하세요."

    NOTIFY_OUT_OF_RANGE = """❌ {value}분은 설정할 수 없습니다.

{min}분에서 {max}분 사이로 입력해주세요.
너무 잦은 알림은 알림이 아니라 소음입니다."""

    NOTIFY_UNPARSEABLE = """❌ '{value}' 을(를) 알아듣지 못했습니다.

이렇게 입력해주세요.
   /notify        → 설정 화면
   /notify 10     → 10분마다
   /notify off    → 끄기"""

    NOTIFY_ASK_MINUTES = """⌨️ 몇 분마다 알려드릴까요?

숫자만 입력해주세요. (예: 7)
{min}분에서 {max}분 사이로 정할 수 있습니다.

그만두려면 /cancel 을 입력하세요."""

    NOTIFY_CURRENT_OFF = "꺼짐"
    NOTIFY_CURRENT_ON = "{minutes}분마다"

    ALREADY_RUNNING = """⚠️ 이미 예약이 진행 중입니다.

📋 진행 중인 예약 정보
━━━━━━━━━━━━━━━━━━━━
📅 출발일: {depDate}
🚉 출발역: {srcLocate}
🏁 도착역: {dstLocate}
🕐 검색 시작: {depTime}
🚄 열차: {trainTypeShow}
💺 좌석: {specialInfoShow}
━━━━━━━━━━━━━━━━━━━━

💡 예약을 취소하려면 /cancel을 입력하세요.
"""

    # ========== 즐겨찾기 ==========
    #
    # 같은 구간을 자주 타는 사람은 매번 아홉 개의 질문에 같은 답을 한다.
    # 저장해두는 것은 날짜를 뺀 전부다. 날짜는 탈 때마다 다른 유일한 답이고,
    # 지난달 날짜를 기억하는 즐겨찾기는 지름길이 아니라 함정이다.
    FAV_EMPTY = """⭐ 저장된 즐겨찾기가 없습니다.

자주 타는 구간을 저장해두면 다음부터는 날짜만 고르면 됩니다.

📌 저장하는 방법
   /start 로 예약 정보를 끝까지 입력하면 나오는 확인 화면에서
   **⭐ 즐겨찾기에 저장** 을 누르세요."""

    FAV_LIST = """⭐ 즐겨찾기 ({count}개)

불러올 항목을 선택하세요."""

    FAV_DETAIL = """⭐ {name}

━━━━━━━━━━━━━━━━━━━━
📍 {route}
🕐 {window}
🚄 {trainType}
💺 {seatOption}
👥 {passengerCount}명{seatStrategy}
━━━━━━━━━━━━━━━━━━━━
저장: {createdAt}

💡 날짜는 저장하지 않습니다. 검색을 시작하면 날짜만 고르면 됩니다."""

    FAV_SAVED = """⭐ 즐겨찾기에 저장했습니다: {name}

다음부터는 /fav 에서 불러와 날짜만 고르면 됩니다.
이름은 /fav 에서 바꿀 수 있습니다."""

    FAV_FULL = """⚠️ 즐겨찾기는 {limit}개까지 저장할 수 있습니다.

/fav 에서 쓰지 않는 항목을 지우고 다시 시도해주세요."""

    FAV_INCOMPLETE = """⚠️ 아직 저장할 수 없습니다.

출발역·도착역까지 입력한 뒤에 저장할 수 있습니다."""

    FAV_GONE = "이미 지워진 즐겨찾기입니다.\n\n/fav 로 목록을 다시 열어주세요."

    FAV_DELETE_CONFIRM = """🗑️ '{name}' 을(를) 지울까요?

다시 만들려면 예약 정보를 처음부터 다시 입력해야 합니다."""

    FAV_DELETED = "🗑️ '{name}' 을(를) 지웠습니다."

    FAV_RENAME_PROMPT = """✏️ '{name}' 의 새 이름을 입력해주세요.

{max}자까지 쓸 수 있습니다.
그만두려면 /cancel 을 입력하세요."""

    FAV_RENAMED = "✏️ 이름을 '{name}' 으로 바꿨습니다."

    FAV_NAME_EMPTY = "❌ 이름을 입력해주세요."

    FAV_STARTED = """⭐ {name} 을(를) 불러왔습니다.

━━━━━━━━━━━━━━━━━━━━
📍 {route}
🕐 {window}
🚄 {trainType}
💺 {seatOption}
👥 {passengerCount}명
━━━━━━━━━━━━━━━━━━━━

📅 출발 희망일만 고르면 됩니다."""

    FAV_NEEDS_ACCOUNT = """⚠️ 코레일 계정이 등록되어 있지 않습니다.

/onboarding 으로 계정을 등록한 뒤에 이용해주세요."""

    FAV_BUSY = """⚠️ 이미 예약이 진행 중입니다.

/status 로 확인하시고, 새로 시작하려면 /cancel 후 다시 시도해주세요."""

    # ========== 업데이트 안내 ==========
    #
    # 봇을 올리는 사람과 쓰는 사람이 다르다. 쓰는 쪽은 버튼이 하나 늘어난
    # 것을 어쩌다 발견해서 알게 되는데, 그래서는 새로 생긴 기능이 있는 줄도
    # 모른 채 계속 쓴다. 버전이 바뀌면 한 번 알린다. 한 번이고, 바뀔 때만
    # 이다. 재시작할 때마다 오는 안내는 안내가 아니라 증상이다.
    # HTML 로 보낸다. expandable blockquote 는 텔레그램이 접어서 보여주는
    # 유일한 방법이고, 이 메시지가 마크업을 쓰는 유일한 메시지다. 넣는 값은
    # 전부 이스케이프한다.
    UPDATED_WITH_NOTES = """🚀 v{version} 으로 업데이트되었습니다

{headline}

<blockquote expandable>{detail}</blockquote>

/help 로 전체 명령어를 확인할 수 있습니다."""

    # 접을 것이 없는 릴리스. 접는 상자만 있고 안이 비어 있으면 눌러본 사람을
    # 속이는 셈이라 아예 만들지 않는다.
    UPDATED_HEADLINE_ONLY = """🚀 v{version} 으로 업데이트되었습니다

{headline}

/help 로 전체 명령어를 확인할 수 있습니다."""

    UPDATED = """🚀 v{version} 으로 업데이트되었습니다

/help 로 전체 명령어를 확인할 수 있습니다."""

    # ========== 에러 메시지 ==========
    ERROR_GENERIC = "⚠️ 오류가 발생했습니다.\n/cancel 또는 /start로 다시 시작해주세요."
    ERROR_INVALID_COMMAND = "❌ 알 수 없는 명령어입니다.\n/help로 사용 가능한 명령어를 확인하세요."
    ERROR_NO_PROGRESS = "ℹ️ 진행 중인 예약이 없습니다.\n/start를 입력하여 예약을 시작하세요."
    ERROR_PHONE_FORMAT = "❌ 전화번호 형식이 올바르지 않습니다.\n하이픈(-)을 포함하여 다시 입력해주세요.\n예시: 010-1234-5678"
    ERROR_DATE_FORMAT = """❌ 날짜 형식이 올바르지 않습니다.

8자리 숫자로 입력해주세요.
예시: {example}

⚠️ 과거 날짜는 입력할 수 없습니다.
"""
    ERROR_TIME_FORMAT = "❌ 시간 형식이 올바르지 않습니다.\nHHMM 형식 4자리로 입력해주세요.\n예시: 1430 (오후 2시 30분)"
    ERROR_TRAIN_TYPE_INVALID = "❌ 1 또는 2를 입력해주세요."
    ERROR_SEAT_TYPE_INVALID = "❌ 1, 2, 3, 4 중 하나를 입력해주세요."
    ERROR_PASSENGER_COUNT_NOT_DIGIT = "❌ 숫자를 입력해주세요. (1~9)"
    ERROR_PASSENGER_COUNT_RANGE = "❌ 1~9명 사이의 인원수를 입력해주세요."
    ERROR_SEAT_STRATEGY_INVALID = "❌ 1 또는 2를 입력해주세요."
    ERROR_CONFIRM_INVALID = """❌ 올바른 응답을 입력해주세요.

• Y 또는 예 → 예약 시작
• N 또는 아니오 → 작업 취소
"""
    ERROR_ADMIN_ENV = "⚠️ 서버 환경변수가 설정되지 않았습니다."
    ERROR_ADMIN_LOGIN = "⚠️ 관리자 계정 로그인에 실패했습니다."
    ERROR_RESERVATION_START_FAILED = "❌ 예약 프로세스 시작에 실패했습니다.\n다시 시도해주세요."
    # Self-hosted deployments are the norm for this bot, so this must not
    # point users at anyone else's paid service.
    ERROR_NOT_SUBSCRIBER = """🚫 사용 권한이 없습니다.

입력하신 번호가 이 봇의 허용 목록(PREAPPROVED_USERS)에 없습니다.

이 봇을 직접 운영하신다면 .env 의 PREAPPROVED_USERS 에 번호를 추가하거나,
제한 없이 열려면 값을 비워두세요.

그 외에는 봇 운영자에게 문의해주세요.
"""
    # ========== 취소 및 완료 메시지 ==========
    CANCELLED = "✅ 예약이 취소되었습니다."
    CANCELLED_BY_USER = "🚫 예약을 취소합니다."
    CANCEL_START_CONFIRMATION = "🚫 예매 진행을 취소합니다."

    PAYMENT_VERIFIED = """✅ 결제가 확인되었습니다

코레일에서 예약이 결제 완료 처리된 것을 확인했습니다.
더 이상 알림을 보내지 않습니다.

즐거운 여행 되세요! 🚄"""

    PAYMENT_EXPIRED_VERIFIED = """❌ 결제 기한이 지나 예약이 취소되었습니다

코레일에 확인한 결과 결제가 완료되지 않았습니다.
좌석은 다시 풀렸습니다.

다시 잡으시려면 /start 로 검색을 시작하세요."""

    # 예약이 목록에서 사라졌지만 발권된 표는 없는 경우.
    # "결제되었습니다"라고 말하지 않는 것이 이 메시지의 존재 이유다 —
    # 직접 취소한 예약도 결제된 예약과 똑같이 목록에서 사라진다.
    PAYMENT_RELEASED = """🚫 예약이 해제되었습니다

결제는 확인되지 않았습니다.
직접 취소하셨거나 결제 기한이 지난 것으로 보입니다.

좌석은 다시 풀렸습니다.
다시 잡으시려면 /start 로 검색을 시작하세요."""

    # 랜덤 배치는 좌석마다 결제가 따로 이뤄지므로 좌석 단위로 알린다.
    # 한 통에 몰아 쓰면 어느 좌석이 끝났는지가 사라진다.
    PAYMENT_VERIFIED_SEAT = """✅ 좌석 {seat}번 결제가 확인되었습니다

{train}

남은 미결제 좌석: {remaining}개"""

    PAYMENT_EXPIRED_SEAT = """❌ 좌석 {seat}번은 결제 기한이 지나 취소되었습니다

{train}

남은 미결제 좌석: {remaining}개"""

    # PAYMENT_VERIFIED_SEAT 과 같은 자리에 들어가므로 플레이스홀더도 같다.
    PAYMENT_RELEASED_SEAT = """🚫 좌석 {seat}번 예약이 해제되었습니다

{train}

결제는 확인되지 않았습니다. 직접 취소하셨거나 기한이 지난 것으로 보입니다.

남은 미결제 좌석: {remaining}개

💡 이 좌석을 다시 잡으시려면 /start 로 검색을 시작하세요."""

    # /notify_off 의 답. "결제했다"고 말하지 않는 것이 요점이다 — 알림만
    # 끄고, 결제 여부는 계속 철도사에 물어 확인되는 대로 알린다.
    NOTIFY_OFF_DONE = """🔕 결제 알림을 껐습니다.

결제 여부는 계속 확인합니다. 결제가 확인되면 알려드리고,
기한이 지나도록 결제되지 않으면 그것도 알려드립니다.

🎫 예약 상태는 /status 로 언제든 확인할 수 있습니다."""

    NOTIFY_OFF_NOTHING = """끌 결제 알림이 없습니다.

검색 진행 상황 알림을 끄시려면 /notify 를 사용해주세요."""

    PAYMENT_REMINDER_TIMEOUT = """⏰ 결제 리마인더 종료

예약 후 10분이 경과하여 리마인더가 자동 종료되었습니다.
결제를 완료하지 않으셨다면 예약이 취소되었을 수 있습니다.

💡 코레일 사이트에서 예약 상태를 확인해주세요.
"""

    # ========== 재시작 복구 메시지 ==========
    RESERVATION_RESUMED = """🔄 검색을 다시 시작했습니다

서버가 재시작되어 검색이 잠시 멈췄지만, 자동으로 이어서 진행합니다.

📋 {srcLocate} → {dstLocate}
📅 {depDate}

취소표가 나오면 바로 알려드립니다."""

    RESERVATION_INTERRUPTED = """⚠️ 검색이 중단되었습니다

서버가 재시작되면서 진행 중이던 검색이 멈췄습니다.

📋 {srcLocate} → {dstLocate}
📅 {depDate}

/start 를 입력해 다시 시작해주세요."""

    RESERVATION_INTERRUPTED_PARTIAL = """⚠️ 검색이 중단되었습니다

서버가 재시작되면서 검색이 멈췄습니다.
이미 예약된 좌석이 있어 중복 예약을 피하려고 자동 재개는 하지 않았습니다.

🔗 예약 확인: {paymentUrl}

남은 좌석이 필요하시면 /start 로 다시 시작해주세요."""

    # ========== 검색이 예고 없이 멈춘 경우 ==========

    # 검색은 끝날 때 스스로 알려온다. 아무 말 없이 사라졌다는 것은
    # 사고이고, 사용자는 있지도 않은 검색을 기다리고 있다는 뜻이다.
    SEARCH_DIED = """⚠️ 검색이 멈췄습니다

{causeLine}

📋 {srcLocate} → {dstLocate}
📅 {depDate}
🕐 {depTime}~{maxDepTime}
👥 {passengerCount}명
🔍 {watch}
⏱️ 멈춘 시각: {diedAt}

같은 조건으로 다시 시작할까요?"""

    SEARCH_DIED_CAUSE_START_FAILED = "검색 프로세스가 시작 직후 종료되었습니다."
    SEARCH_DIED_CAUSE_CRASHED = "검색 프로세스가 예기치 않게 종료되었습니다."

    # 재개 버튼을 줄 수 없는 경우. 저장된 로그인 정보가 사라지면
    # 다시 로그인할 방법이 없어 처음부터 다시 받는 수밖에 없다.
    SEARCH_DIED_NOT_RESUMABLE = """
⚠️ 저장된 로그인 정보가 없어 자동 재개는 할 수 없습니다.
/start 로 다시 시작해주세요."""

    SEARCH_RESUMED = """🔄 검색을 다시 시작했습니다

📍 {srcLocate} → {dstLocate}
📅 {depDate}

취소표가 나오면 바로 알려드립니다."""

    SEARCH_RESUME_FAILED = """❌ 검색을 다시 시작하지 못했습니다

/start 로 처음부터 다시 시도해주세요."""

    SEARCH_DEAD_DISCARDED = """🗑️ 멈춘 검색을 정리했습니다.

/start 를 입력하여 새로 시작할 수 있습니다."""

    SEARCH_DEAD_GONE = "이미 정리된 검색입니다.\n/start 로 새로 시작해주세요."

    # ========== 관리자 메시지 ==========
    ADMIN_AUTH_REQUIRED = "🔐 관리자 인증이 필요합니다.\n관리자 비밀번호를 입력해주세요."
    ADMIN_AUTH_SUCCESS = "✅ 관리자 인증 성공!"
    ADMIN_AUTH_FAILED = "❌ 관리자 인증 실패\n올바른 비밀번호를 입력해주세요."
    ADMIN_AUTH_FAILED_REMAINING = """❌ 관리자 인증 실패

남은 시도 횟수: {remaining}회
초과 시 {lockout_minutes}분간 인증이 차단됩니다."""
    ADMIN_AUTH_LOCKED = """🚫 관리자 인증이 일시적으로 차단되었습니다.

인증 실패 횟수를 초과했습니다.
{remaining_minutes}분 후에 다시 시도해주세요."""
    ADMIN_DISABLED = """🚫 관리자 기능이 비활성화되어 있습니다.

서버에 ADMIN_PASSWORD 환경변수가 설정되지 않았습니다."""

    # ========== 봇 자신에 대한 안내 ==========
    #: Shown to an operator when the numbers could not be read. Redis answering
    #: is what those numbers are read from, so a failure here is itself the
    #: finding - reporting zeroes would read as a quiet bot rather than a
    #: storage that is not answering.
    VERSION_OPERATOR_UNAVAILABLE = """🔧 운영 상태
저장소에서 숫자를 읽지 못했습니다. Redis 와 서버 로그를 확인해주세요."""

    # ========== Backward Compatibility Methods for MessageTemplates ==========
    # These methods provide compatibility with the old MessageTemplates interface

    @staticmethod
    def welcome_message(skip_login_prompts: bool = False):
        """Welcome message (compatibility method)

        The list of steps has to match what the user is actually asked for,
        so it drops the login step when the server logs in on its own.
        """
        if skip_login_prompts:
            return Messages.WELCOME_PRECONFIGURED
        return Messages.WELCOME

    @staticmethod
    def request_phone_number():
        """Request phone number (compatibility method)"""
        return Messages.REQUEST_PHONE

    @staticmethod
    def request_password():
        """Request password (compatibility method)"""
        return Messages.REQUEST_PASSWORD

    @staticmethod
    def date_example() -> str:
        """
        Today, written the way the box wants it.

        The example doubles as the default: a user with no strong opinion
        types what they were shown. It used to be a fixed 20250425, which
        aged into a past date, so following the instruction exactly earned
        "과거 날짜는 입력할 수 없습니다". Read from the railway's clock, which
        is the one validate_date compares against.
        """
        from datetime import datetime

        from korail_bot.utils.timezone import RAIL_TIMEZONE

        today = datetime.now(RAIL_TIMEZONE)
        return f"{today:%Y%m%d} ({today.year}년 {today.month}월 {today.day}일, 오늘)"

    @staticmethod
    def login_success():
        """Login success (compatibility method)"""
        return Messages.LOGIN_SUCCESS.format(example=Messages.date_example())

    @staticmethod
    def ask_again_date():
        """Ask for the date again, with today as the worked example."""
        return Messages.ASK_AGAIN_DATE.format(example=Messages.date_example())

    @staticmethod
    def bad_date_format():
        """What an unreadable date gets back, with a date that would work."""
        return Messages.ERROR_DATE_FORMAT.format(example=Messages.date_example())

    @staticmethod
    def preconfigured_login_success(username: str):
        """Login success with the account from the environment.

        The account is masked: whoever runs the bot knows which one it is, and
        the message lands in a chat transcript that outlives the reservation.
        """
        from korail_bot.utils.privacy import mask_phone

        return Messages.LOGIN_SUCCESS_PRECONFIGURED.format(
            username=mask_phone(username),
            example=Messages.date_example(),
        )

    @staticmethod
    def login_failure(username: str):
        """Login failure (compatibility method)"""
        return Messages.LOGIN_FAILED_RETRY.format(username=username)

    @staticmethod
    def request_departure_station():
        """Request departure station after date input (compatibility method)"""
        return Messages.REQUEST_DATE

    @staticmethod
    def request_arrival_station():
        """Request arrival station after departure station input (compatibility method)"""
        return Messages.REQUEST_SRC_STATION

    @staticmethod
    def not_in_allow_list():
        """Not in allow list (compatibility method)"""
        return Messages.ERROR_NOT_SUBSCRIBER

    @staticmethod
    def reservation_started():
        """Reservation started (compatibility method)"""
        return Messages.RESERVATION_STARTED

    @staticmethod
    def reservation_cancelled():
        """Reservation cancelled (compatibility method)"""
        return Messages.CANCELLED

    @staticmethod
    def help_message(is_admin: bool = False):
        """
        The command list, for whoever is asking.

        Args:
            is_admin: Whether this chat may actually use the operator's tools.
                Listing them for someone who cannot makes them work out which
                lines apply to them, and tells them which dangerous doors the
                server has.
        """
        if is_admin:
            return Messages.HELP + Messages.HELP_ADMIN_SECTION
        return Messages.HELP

    @staticmethod
    def version_message(
        version: str,
        uptime: str = "",
        release_headline: str = "",
        operator_status: str = "",
    ) -> str:
        """
        What is running here, and since when.

        The version alone is a number nobody can act on, so it arrives with
        the release's own headline - the same lines the update announcement
        sends, which say what changed in terms of the chat rather than the
        code. A release that shipped without notes simply has no such block;
        inventing one would be worse than the number on its own.

        Args:
            version: The version now running
            uptime: How long the bot has been up, already formatted. Empty
                when the process never recorded a start, in which case the
                line is left out rather than guessed at.
            release_headline: The release notes for this version, if any were
                written
            operator_status: The operational numbers, for a chat that may
                actually act on them. Empty for everyone else - they are of no
                use to a user and they count other people's bookings.
        """
        lines = [f"🤖 코레일 예매 봇 v{version}"]

        if uptime:
            lines.append(f"⏱️ 가동 시간: {uptime}")

        if release_headline:
            lines += ["", "🆕 이번 버전에서 바뀐 것", release_headline]

        if operator_status:
            lines += ["", operator_status]

        lines += ["", "💡 내 예약 상태는 /status, 이 봇에 대한 안내는 /about"]
        return "\n".join(lines)

    @staticmethod
    def operator_status(
        running: int,
        scheduled: int,
        awaiting_payment: int,
        registered_chats: int,
        debug_logging: bool,
    ) -> str:
        """
        The numbers whoever runs the bot checks before touching it.

        The first one is the one that matters: restarting is what an operator
        came here to decide, and every running search is someone who has been
        waiting for a seat and would have to be picked back up.

        Counted rather than listed. This block answers "is anything going on",
        and naming whose search it is would put other people's travel in a
        chat transcript to answer a question that did not ask.
        """
        return (
            "🔧 운영 상태\n"
            f"• 진행 중인 검색: {running}건\n"
            f"• 시작을 기다리는 예약 검색: {scheduled}건\n"
            f"• 결제 대기 중인 예약: {awaiting_payment}건\n"
            f"• 계정을 등록한 방: {registered_chats}개\n"
            f"• 디버그 로그: {'켜짐' if debug_logging else '꺼짐'}"
        )

    @staticmethod
    def about_message(version: str) -> str:
        """
        What this bot is, for someone deciding whether to trust it with a login.

        Half of it is what the bot does *not* do. Someone who hands over a
        railway password wants to know where the payment goes, and the answer
        - nowhere, they pay at the railway themselves - is the thing worth
        saying before they find out by waiting for a charge that never comes.
        """
        return f"""ℹ️ 코레일 취소표 예매 봇 (v{version})

매진된 열차를 대신 지켜보다가 자리가 나면 예약하고 알려드립니다.

• 결제는 대신 하지 않습니다. 좌석을 잡으면 알려드리고, 결제는 코레일·SR 앱이나 홈페이지에서 직접 하셔야 합니다.
• 카드번호 같은 결제 정보는 어디에서도 받지 않습니다.
• 철도 계정 비밀번호는 암호화해 보관하고, /logout 을 하면 즉시 지웁니다.
• 코레일·SR 과 무관한 비공식 개인 프로젝트입니다. 예매를 보장하지 않습니다.

명령어 목록은 /help, 지금 도는 버전과 가동 상태는 /version 에 있습니다."""

    @staticmethod
    def payment_reminder(remaining_minutes: int, remaining_seconds: int):
        """Payment reminder (compatibility method)"""
        if remaining_seconds == 0:
            time_text = f"{remaining_minutes}분"
        else:
            time_text = f"{remaining_minutes}분 {remaining_seconds}초"

        return f"""⏰ 결제 리마인더

예약 취소까지 남은 시간: {time_text}

서둘러 결제를 완료해주세요!
💡 결제하시면 자동으로 확인해 알려드립니다. 알림만 끄려면 /notify_off
"""


class MessageService:
    """메시지 전송 서비스"""

    def __init__(self, session, send_url):
        """
        Args:
            session: requests.session() 객체
            send_url: 텔레그램 API URL
        """
        self.session = session
        self.send_url = send_url

    def send(self, chat_id, message):
        """
        메시지 전송

        Args:
            chat_id: 채팅 ID
            message: 전송할 메시지 (str 또는 템플릿 메서드)
        """
        url = f"{self.send_url}/sendMessage"
        params = {"chat_id": chat_id, "text": message}
        self.session.get(url, params=params)

    def send_to_multiple(self, chat_ids, message):
        """
        여러 사용자에게 메시지 전송

        Args:
            chat_ids: 채팅 ID 리스트
            message: 전송할 메시지
        """
        for chat_id in chat_ids:
            self.send(chat_id, message)
