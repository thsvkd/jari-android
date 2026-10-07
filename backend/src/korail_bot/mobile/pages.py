"""
Public pages outside /api/mobile: the privacy policy and how to delete an
account. Google Play links to both (the privacy policy, and the account- and
data-deletion URL of the data safety form), so they are served to any browser,
need no session and say only what the code in this repository actually keeps.

Change the text when what is stored changes, and keep it in step with the
data types declared on Play: user ID, phone number, other personal info,
purchase history, in-app search history, diagnostics, device IDs. The date on
the page is the day the text last changed, not the release date.
"""

from html import escape

APP_NAME = "자리났다"
DEVELOPER = "SonPang"
EFFECTIVE_DATE = "2026년 10월 8일"

_STYLE = """
:root { color-scheme: light dark; --bg: #f7f8fa; --text: #151a23; --muted: #5a6475; --line: #dde2ea; --accent: #2457d6; --card: #ffffff; }
@media (prefers-color-scheme: dark) { :root { --bg: #11151c; --text: #e8ecf2; --muted: #a3adbd; --line: #2b3240; --accent: #8fb0ff; --card: #171c25; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 16px/1.7 -apple-system, "Noto Sans KR", "Apple SD Gothic Neo", sans-serif; word-break: keep-all; overflow-wrap: anywhere; }
main { max-width: 760px; margin: 0 auto; padding: 24px 16px 64px; }
header { margin: 0 0 24px; padding: 18px 18px 14px; border: 1px solid var(--line); border-radius: 14px; background: var(--card); }
header .app { display: block; font-size: 1.7rem; font-weight: 700; line-height: 1.2; }
header .dev { display: block; margin-top: 4px; color: var(--muted); }
h1 { font-size: 1.4rem; line-height: 1.3; margin: 0 0 4px; }
h2 { font-size: 1.15rem; margin: 32px 0 8px; padding-top: 16px; border-top: 1px solid var(--line); }
h3 { font-size: 1rem; margin: 18px 0 4px; }
p, li { margin: 6px 0; }
ul, ol { padding-left: 1.3em; }
small, .muted { color: var(--muted); }
a { color: var(--accent); }
table { width: 100%; border-collapse: collapse; font-size: .95rem; }
th, td { text-align: left; vertical-align: top; padding: 8px 6px; border-bottom: 1px solid var(--line); }
th { white-space: nowrap; }
.table { overflow-x: auto; }
nav { margin: 0 0 16px; font-size: .95rem; }
"""


def _mail_link(contact: str) -> str:
    address = escape(contact, quote=True)
    # Cloudflare's email obfuscation swaps an address for a decoder script, which
    # this page's CSP blocks, leaving "[email protected]". These comments opt out.
    return f'<!--email_off--><a href="mailto:{address}">{address}</a><!--/email_off-->'


def _contact(contact: str | None) -> str:
    if contact:
        return (
            f"<p>개인정보 문의와 웹 탈퇴 요청: {_mail_link(contact)}</p>"
            "<p>보내실 때 앱 아이디를 적어 주세요. 비밀번호나 코레일 계정 정보는 보내지 마세요.</p>"
        )
    return (
        "<p>개인정보 문의와 탈퇴 요청은 앱의 <b>설정 → 회원 탈퇴</b>에서 하거나, "
        "앱 초대 코드를 보내 준 운영자에게 앱 아이디와 함께 알려 주세요. "
        "비밀번호나 코레일 계정 정보는 보내지 마세요.</p>"
    )


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · {APP_NAME}</title>
<style>{_STYLE}</style>
</head>
<body><main>
<nav><a href="/privacy">개인정보처리방침</a> · <a href="/delete-account">계정·데이터 삭제</a></nav>
<header><span class="app">{APP_NAME}</span><span class="dev">개발자 {DEVELOPER}</span></header>
{body}
</main></body>
</html>
"""


def _in_app_steps() -> str:
    return """<ol>
<li>앱 아래쪽의 <b>설정</b>을 누릅니다.</li>
<li>맨 아래 <b>계정 → 회원 탈퇴</b>를 누릅니다.</li>
<li>본인 확인을 위해 앱 비밀번호를 한 번 더 입력하고 <b>탈퇴하기</b>를 누릅니다.</li>
</ol>
<p>탈퇴하면 진행 중인 자리 찾기와 예약해 둔 자리 찾기를 멈추고, 아래 정보를 곧바로 지운 뒤 로그아웃합니다.
결제를 기다리는 예약이 있으면 탈퇴하지 않고 알려 드려요. 코레일 앱에서 결제하거나 앱에서 예약을 취소한 뒤 다시 탈퇴해 주세요.
관리자 계정은 마지막 하나일 때 탈퇴할 수 없어요.</p>"""


def _email_steps(contact: str | None) -> str:
    if contact:
        how = (
            f"{_mail_link(contact)} 로 <b>앱 아이디</b>를 적어 탈퇴를 요청해 주세요. "
            "제목은 “자리났다 탈퇴 요청”이면 됩니다."
        )
    else:
        how = "앱 초대 코드를 보내 준 운영자에게 <b>앱 아이디</b>를 알려 탈퇴를 요청해 주세요."
    return f"""<ol>
<li>{how} 비밀번호나 코레일 계정 정보는 보내지 마세요.</li>
<li>본인 확인: 이 앱은 운영자가 직접 초대한 사람만 씁니다. 운영자가 초대 코드를 보냈던 연락처(메신저·전화 등)로
그 아이디의 주인이 맞는지 확인합니다. 확인되지 않으면 지우지 않습니다.</li>
<li>확인되면 앱에서 탈퇴할 때와 같은 방법으로 지우고, 지체 없이(늦어도 요청을 받은 날부터 30일 안에) 처리 결과를 알려 드립니다.</li>
</ol>"""


def _deleted_list() -> str:
    return """<ul>
<li>앱 계정: 앱 아이디, 비밀번호 해시, 권한, 로그인 세션</li>
<li>연결한 코레일 아이디(휴대전화 번호 또는 회원번호)와 비밀번호(암호화된 것)</li>
<li>검색 조건과 자리 찾기 기록(진행 중·예약한·멈춘 것), 즐겨찾기, 알림 간격, 시간대</li>
<li>서버가 들고 있는 예약 기록(예약 번호, 열차, 좌석, 결제 기한)</li>
<li>앱 알림함의 알림, 보내지 못한 알림, 휴대폰 알림 토큰(기기 ID)</li>
<li>이 휴대폰에 남은 연결 실패 기록(진단 정보)과 최근 조회 구간</li>
<li>에이전트 연결: 연결 기록과 그 토큰, 이 계정이 확인하던 연결 요청</li>
<li>이 계정의 요청 횟수 제한 기록</li>
</ul>"""


def _kept_list() -> str:
    return """<ul>
<li><b>검색이 끝났다는 표시</b>(끝난 이유·시각, 개인정보 없음): 서버 업데이트가 진행 중인 검색을 잃었는지 확인하는 데 쓰며 7일 뒤 자동으로 지워집니다.</li>
<li><b>서버 로그</b>(내부 사용자 번호, 요청 오류, 앱이 올린 연결 실패 기록): 줄 단위로 따로 지우지 않습니다.
10MB 파일 3개(최대 30MB)를 돌려 쓰며 넘치면 오래된 것부터 지워지고, 서버를 업데이트할 때(컨테이너를 새로 만들 때) 그 전 로그가 모두 지워집니다.
보관 기간을 날짜로 정해 두지는 않았습니다.</li>
<li><b>접속 기록</b>(접속 IP, 시각, 요청 경로): 서버 앞단의 프록시가 로그를 돌려 쓰며 덮어쓸 때까지, Cloudflare가 남긴 것은 Cloudflare 정책에 따릅니다.</li>
<li><b>데이터베이스 파일</b>: 지운 기록은 서비스에서 곧바로 읽을 수 없게 되지만, Redis·SQLite 파일 안에서 실제로 덮어써지기까지 시간이 걸릴 수 있습니다.
사용자 데이터를 따로 백업해 두지는 않습니다(배포 때 만드는 서버 사본에는 프로그램과 설정만 들어갑니다).</li>
<li><b>코레일의 기록</b>: 코레일에서 이미 예약·결제한 내역은 코레일의 기록이라 이 앱에서 지울 수 없습니다. 코레일에서 확인해 주세요.</li>
<li>법령 때문에 따로 보관하는 정보는 없습니다.</li>
</ul>"""


def _partial_steps() -> str:
    return """<p>계정을 두고 일부만 지울 수도 있습니다.</p>
<ul>
<li><b>코레일 계정 정보</b>: 설정 → 코레일 계정 → <b>코레일 계정 연결 해제</b>. 진행 중인 자리 찾기를 멈추고 서버의 코레일 아이디·비밀번호와 작성 중인 검색 조건을 지웁니다.</li>
<li><b>즐겨찾기</b>: 즐겨찾기 화면에서 각 항목의 <b>삭제</b>.</li>
<li><b>진행 중·예약한 자리 찾기</b>: 홈이나 검색 상세의 <b>그만 찾기</b>. 그 검색 기록과 검색용 코레일 로그인 사본을 지웁니다.</li>
<li><b>에이전트 연결</b>: 설정 → 에이전트 연결에서 각 연결의 <b>연결 끊기</b>. 그 연결 기록과 토큰을 바로 지우고, 그 에이전트는 더 이상 조회·예약할 수 없습니다.</li>
<li><b>휴대폰 알림</b>: Android 설정 → 앱 → 자리났다 → 알림에서 끕니다.</li>
<li><b>이 휴대폰에 남은 정보</b>(로그인, 최근 조회 구간): 앱에서 로그아웃하거나 앱을 지우면 지워집니다.</li>
</ul>"""


def privacy_page(contact: str | None) -> str:
    return _page(
        "개인정보처리방침",
        f"""<h1>개인정보처리방침</h1>
<p class="muted">시행일: {EFFECTIVE_DATE}</p>
<p>{APP_NAME}는 초대받은 사람만 쓰는 코레일 빈자리 찾기 앱입니다. 개인 개발자 {DEVELOPER}가 운영하며 광고, 광고 ID, 사용 분석 도구가 없고,
개인정보를 판매하거나 제3자와 공유하지 않습니다. 이 문서는 앱과 서버가 실제로 저장하고 보내는 정보를 적었습니다.</p>

<h2>1. 수집하는 정보</h2>
<div class="table"><table>
<tr><th>종류</th><th>정보</th><th>쓰는 곳</th><th>보관</th></tr>
<tr><td>사용자 ID</td><td>앱 아이디, 비밀번호(scrypt 해시로만 저장), 권한(회원·관리자), 로그인 세션(토큰의 해시)</td><td>초대받은 사람의 로그인</td><td>탈퇴할 때까지. 세션은 30일 뒤 만료</td></tr>
<tr><td>전화번호</td><td>코레일 아이디(휴대전화 번호 또는 회원번호)</td><td rowspan="2">사용자 대신 코레일에 로그인해 열차·좌석을 조회하고, 사용자가 요청한 빈자리를 예약</td><td rowspan="2">서버 비밀키로 암호화해 저장. 코레일 계정 연결을 해제하거나 탈퇴할 때까지, 늦어도 연결한 뒤 90일. 검색하는 동안에는 서버가 다시 시작돼도 이어 가도록 암호화한 사본을 따로 두고 72시간씩 연장</td></tr>
<tr><td>기타 개인정보</td><td>코레일 비밀번호</td></tr>
<tr><td>앱 내 검색 기록</td><td>검색 조건(날짜, 출발·도착역, 시간대, 인원, 좌석 조건), 진행 중·예약한 자리 찾기, 즐겨찾기, 알림 간격, 시간대</td><td>자리 찾기와 화면 복원</td><td>즐겨찾기·알림 간격은 탈퇴할 때까지, 작성 중인 검색 조건은 하루, 멈춘 자리 찾기는 72시간, 진행 중인 자리 찾기는 끝날 때까지</td></tr>
<tr><td>구매 내역</td><td>잡은 좌석의 예약 번호, 열차, 좌석, 결제 기한(결제는 사용자가 코레일에서 직접 합니다)</td><td>결제 기한 알림</td><td>결제 기한이 지나고 5분쯤 뒤 자동 삭제</td></tr>
<tr><td>에이전트 연결</td><td>사용자가 앱에서 승인한 AI 에이전트의 연결 기록: 에이전트가 밝힌 이름, 돌아갈 곳(주소), 연결한 시각과 마지막으로 쓴 시각, 예약까지 맡겼는지, 연결을 요청한 브라우저의 국가(Cloudflare가 알려 준 두 글자), 연결 토큰(해시로만 저장)</td><td>승인한 에이전트가 사용자 대신 열차·좌석·상태를 조회하고, 사용자가 허용한 경우 자리 찾기·예약을 하게 함</td><td>연결을 끊거나 90일 동안 쓰지 않으면 삭제, 탈퇴하면 바로 삭제. 승인하지 않은 연결 요청은 10분 뒤 만료되고 곧 지워짐</td></tr>
<tr><td>앱 내 알림</td><td>앱 알림함의 알림 내용</td><td>자리 찾기·예약 소식 전달</td><td>탈퇴할 때까지</td></tr>
<tr><td>기기 ID</td><td>휴대폰 알림 토큰(Firebase Cloud Messaging), 계정당 최대 10대</td><td>휴대폰 알림 보내기. 사용자가 알림을 켤 때만 등록</td><td>서버 비밀키로 암호화해 저장. 탈퇴할 때까지</td></tr>
<tr><td>진단</td><td>연결 실패 기록(시각, 요청 종류와 경로, 원인, 온라인 여부, 화면 표시 여부, 걸린 시간)</td><td>휴대폰과 서버 사이의 연결 문제 진단</td><td>휴대폰에 최근 50건까지 두었다가 다음에 연결되면 서버 로그로 올리고 휴대폰에서 지움. 서버 로그 보관은 아래와 같음</td></tr>
<tr><td>보안</td><td>IP 주소(Cloudflare가 넘겨준 접속 IP), 앱 아이디, 내부 사용자 번호</td><td>로그인 시도·요청 횟수 제한</td><td>해시로만 저장하고 1~5분 뒤 만료</td></tr>
<tr><td>서버 로그</td><td>요청 오류, 자리 찾기 진행, 내부 사용자 번호, 위의 연결 실패 기록</td><td>장애 확인</td><td>10MB 파일 3개를 돌려 쓰며 오래된 것부터 지우고, 서버를 업데이트할 때 그 전 로그를 지움. 비밀번호는 남기지 않음</td></tr>
<tr><td>접속 기록</td><td>접속 IP, 시각, 요청 경로, 브라우저·앱 정보</td><td>서버 앞단의 프록시와 Cloudflare가 연결 문제·공격 확인용으로 남김</td><td>프록시 로그를 돌려 쓰며 덮어쓸 때까지, Cloudflare 기록은 Cloudflare 정책에 따름</td></tr>
</table></div>
<p>휴대폰에는 로그인 세션 토큰(Android Keystore 키로 암호화), 화면 테마, 최근에 조회한 구간, 위의 연결 실패 기록만 남습니다.
앱에서 로그아웃하면 세션 토큰과 최근 구간을 지웁니다.
로그인 화면의 <b>체험하기</b>는 앱 안의 샘플 데이터만 쓰고 서버나 코레일에 아무것도 보내지 않으며, 체험 중에 본 내용은 체험을 끝내면 사라집니다.</p>

<h2>2. 공유와 처리 위탁</h2>
<p>개인정보를 제3자와 공유하거나 판매하지 않습니다. 아래는 사용자가 요청한 일을 하거나 서비스를 전달하기 위해서만 정보를 받습니다.</p>
<ul>
<li><b>코레일(한국철도공사)</b>: 사용자가 요청한 조회·예약을 하려고 그 사용자의 코레일 아이디·비밀번호와 검색 조건을 코레일 서버로 보냅니다. 다른 목적으로는 보내지 않습니다.</li>
<li><b>Google Firebase Cloud Messaging</b>(처리 위탁): 휴대폰 알림을 켠 경우 알림 토큰과 "새 알림이 왔어요" 문구, 알림 번호만 보냅니다. 알림 내용은 보내지 않고 앱 알림함에서 봅니다.</li>
<li><b>사용자가 연결한 AI 에이전트</b>: 사용자가 앱에서 승인한 에이전트에게, 그 에이전트가 요청한 열차·좌석·상태·즐겨찾기 정보를 보냅니다. 에이전트 쪽에서 그 정보를 어떻게 다루는지는 그 서비스의 정책을 따릅니다. 연결은 앱에서 언제든 끊을 수 있습니다.</li>
<li><b>Cloudflare</b>(처리 위탁, 네트워크): 앱과 서버 사이의 연결을 중계합니다. 접속 IP와 요청이 Cloudflare를 거칩니다.</li>
</ul>
<p>법령에 따른 요구가 있으면 그 범위에서만 따릅니다.</p>

<h2>3. 저장 위치와 보호</h2>
<p>서버 데이터는 운영자가 관리하는 서버의 Redis(자리 찾기·코레일 로그인 정보)와 SQLite(계정·알림·요청 횟수 제한)에 저장합니다.
모든 통신은 HTTPS로 암호화됩니다. 코레일 비밀번호와 알림 토큰은 서버 비밀키로 암호화해 저장하고, 앱 비밀번호와 세션 토큰은 해시로만 저장합니다.</p>

<h2>4. 계정과 데이터 삭제</h2>
<h3>앱에서 탈퇴</h3>
{_in_app_steps()}
<h3>앱 없이 요청</h3>
{_email_steps(contact)}
<h3>지우는 정보</h3>
{_deleted_list()}
<h3>남는 정보</h3>
{_kept_list()}
<h3>일부만 지우기</h3>
{_partial_steps()}
<p>같은 내용이 <a href="/delete-account">계정·데이터 삭제</a>에 있습니다.</p>

<h2>5. 이용자의 권리</h2>
<p>저장된 정보의 열람, 정정, 삭제, 처리 정지를 요청할 수 있습니다. 아래 연락처로 알려 주세요.</p>

<h2>6. 문의</h2>
<p>운영: {DEVELOPER}</p>
{_contact(contact)}

<h2>7. 바뀌는 경우</h2>
<p>저장하거나 보내는 정보가 바뀌면 이 문서를 고치고 시행일을 새로 적습니다.</p>
""",
    )


def delete_account_page(contact: str | None) -> str:
    return _page(
        "계정·데이터 삭제",
        f"""<h1>계정·데이터 삭제</h1>
<p class="muted">시행일: {EFFECTIVE_DATE}</p>
<p>{APP_NAME}({DEVELOPER})의 계정과 데이터를 지우는 방법입니다.</p>

<h2>1. 앱에서 바로 탈퇴하기</h2>
{_in_app_steps()}

<h2>2. 앱 없이 탈퇴 요청하기</h2>
<p>앱을 지웠거나 로그인할 수 없으면 이렇게 요청해 주세요.</p>
{_email_steps(contact)}

<h2>3. 지우는 정보</h2>
{_deleted_list()}

<h2>4. 남는 정보와 기간</h2>
{_kept_list()}

<h2>5. 계정은 두고 일부만 지우기</h2>
{_partial_steps()}

<h2>6. 문의</h2>
{_contact(contact)}
<p>자세한 내용은 <a href="/privacy">개인정보처리방침</a>을 봐 주세요.</p>
""",
    )
