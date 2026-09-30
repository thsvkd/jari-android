"""
Public pages outside /api/mobile: the privacy policy and how to delete an
account. Google Play links to both, so they are served to any browser, need no
session and say only what the code in this repository actually keeps.

Change the text when what is stored changes. The date on the page is the day
the text last changed, not the release date.
"""

from html import escape

EFFECTIVE_DATE = "2026년 10월 1일"

_STYLE = """
:root { color-scheme: light dark; --bg: #f7f8fa; --text: #151a23; --muted: #5a6475; --line: #dde2ea; --accent: #2457d6; }
@media (prefers-color-scheme: dark) { :root { --bg: #11151c; --text: #e8ecf2; --muted: #a3adbd; --line: #2b3240; --accent: #8fb0ff; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 16px/1.7 -apple-system, "Noto Sans KR", "Apple SD Gothic Neo", sans-serif; word-break: keep-all; overflow-wrap: anywhere; }
main { max-width: 760px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 1.6rem; line-height: 1.3; margin: 0 0 4px; }
h2 { font-size: 1.15rem; margin: 32px 0 8px; padding-top: 16px; border-top: 1px solid var(--line); }
p, li { margin: 6px 0; }
ul, ol { padding-left: 1.3em; }
small, .muted { color: var(--muted); }
a { color: var(--accent); }
table { width: 100%; border-collapse: collapse; font-size: .95rem; }
th, td { text-align: left; vertical-align: top; padding: 8px 6px; border-bottom: 1px solid var(--line); }
th { white-space: nowrap; }
.table { overflow-x: auto; }
nav { margin: 0 0 24px; font-size: .95rem; }
"""


def _contact(contact: str | None) -> str:
    if contact:
        address = escape(contact, quote=True)
        return (
            f'<p>개인정보 문의와 웹 탈퇴 요청: <a href="mailto:{address}">{address}</a></p>'
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
<title>{title} · 자리났다</title>
<style>{_STYLE}</style>
</head>
<body><main>
<nav><a href="/privacy">개인정보처리방침</a> · <a href="/delete-account">회원 탈퇴 안내</a></nav>
{body}
</main></body>
</html>
"""


def _in_app_steps() -> str:
    return """<ol>
<li>앱 아래쪽의 <b>설정</b>을 누릅니다.</li>
<li>맨 아래 <b>회원 탈퇴</b>를 누릅니다.</li>
<li>앱 비밀번호를 한 번 더 입력하고 <b>탈퇴하기</b>를 누릅니다.</li>
</ol>
<p>탈퇴하면 진행 중인 자리 찾기와 예약해 둔 자리 찾기를 멈추고, 아래 정보를 곧바로 지운 뒤 로그아웃합니다.
결제를 기다리는 예약이 있으면 탈퇴하지 않고 알려 드려요. 코레일 앱에서 결제하거나 앱에서 예약을 취소한 뒤 다시 탈퇴해 주세요.
관리자 계정은 마지막 하나일 때 탈퇴할 수 없어요.</p>"""


def _deleted_list() -> str:
    return """<ul>
<li>앱 계정(아이디, 비밀번호 해시)과 로그인 세션</li>
<li>연결한 코레일 아이디·비밀번호(암호화된 것)</li>
<li>검색 조건, 진행 중·예약한·멈춘 자리 찾기 기록, 즐겨찾기, 알림 간격, 시간대</li>
<li>결제 기한 확인용 예약 기록(예약 번호, 열차, 좌석, 결제 기한)</li>
<li>앱 알림함의 알림과 휴대폰 알림 토큰, 보내지 못한 알림</li>
<li>이 계정의 요청 횟수 제한 기록</li>
</ul>"""


def privacy_page(contact: str | None) -> str:
    return _page(
        "개인정보처리방침",
        f"""<h1>자리났다 개인정보처리방침</h1>
<p class="muted">시행일: {EFFECTIVE_DATE}</p>
<p>자리났다는 초대받은 사람만 쓰는 코레일 빈자리 찾기 앱입니다. 개인 운영자가 운영하며 광고, 사용 분석 도구,
개인정보 판매나 마케팅 목적의 제공이 없습니다. 이 문서는 앱과 서버가 실제로 저장하고 보내는 정보를 적었습니다.</p>

<h2>1. 수집하는 정보와 쓰는 곳</h2>
<div class="table"><table>
<tr><th>정보</th><th>쓰는 곳</th><th>보관</th></tr>
<tr><td>앱 아이디, 비밀번호(scrypt 해시로만 저장), 권한(회원·관리자), 로그인 세션(토큰의 해시)</td><td>초대받은 사람의 로그인</td><td>탈퇴할 때까지. 세션은 30일 뒤 만료</td></tr>
<tr><td>코레일 아이디(휴대전화 번호 또는 회원번호)와 비밀번호</td><td>사용자 대신 코레일에 로그인해 열차·좌석을 조회하고 빈자리를 예약</td><td>서버 비밀키로 암호화해 저장. 코레일 계정 연결을 해제하거나 탈퇴할 때까지, 늦어도 연결한 뒤 90일</td></tr>
<tr><td>검색 조건(날짜, 출발·도착역, 시간대, 인원, 좌석 조건), 진행 중·예약한 자리 찾기, 즐겨찾기, 알림 간격, 시간대</td><td>자리 찾기와 화면 복원</td><td>즐겨찾기·알림 간격은 탈퇴할 때까지, 작성 중인 검색 조건은 하루, 멈춘 자리 찾기는 72시간, 진행 중인 자리 찾기는 끝날 때까지. 서버가 다시 시작돼도 검색을 이어 가려고 검색하는 동안 코레일 로그인 정보를 따로 암호화해 두고 72시간씩 연장</td></tr>
<tr><td>잡은 좌석의 예약 번호, 열차, 좌석, 결제 기한</td><td>결제 기한 알림</td><td>결제 기한이 지나고 5분 뒤 자동 삭제</td></tr>
<tr><td>앱 알림함의 알림 내용</td><td>자리 찾기·예약 소식 전달</td><td>탈퇴할 때까지</td></tr>
<tr><td>휴대폰 알림 토큰(Firebase Cloud Messaging), 계정당 최대 10대</td><td>휴대폰 알림 보내기. 알림을 켤 때만 등록</td><td>서버 비밀키로 암호화해 저장. 탈퇴할 때까지</td></tr>
<tr><td>연결 실패 기록(시각, 요청 종류와 경로, 원인, 온라인 여부, 화면 표시 여부, 걸린 시간)</td><td>휴대폰과 서버 사이의 연결 문제 진단</td><td>휴대폰에 최근 50건까지 두었다가 다음에 연결되면 서버 로그로 올리고 휴대폰에서 지움</td></tr>
<tr><td>IP 주소(Cloudflare가 넘겨준 접속 IP), 앱 아이디, 내부 사용자 번호</td><td>로그인 시도·요청 횟수 제한</td><td>해시로만 저장하고 1~5분 뒤 만료</td></tr>
<tr><td>서버 로그(요청 오류, 자리 찾기 진행, 내부 사용자 번호, 위의 연결 실패 기록)</td><td>장애 확인</td><td>파일 10MB씩 3개를 돌려 쓰며 오래된 줄부터 덮어씀. 비밀번호는 남기지 않음</td></tr>
</table></div>
<p>휴대폰에는 로그인 세션 토큰(Android Keystore 키로 암호화), 화면 테마, 최근에 조회한 구간, 위의 연결 실패 기록만 남습니다.
앱에서 로그아웃하면 세션 토큰과 최근 구간을 지웁니다.</p>

<h2>2. 제3자에게 보내는 정보</h2>
<ul>
<li><b>코레일(한국철도공사)</b>: 사용자가 요청한 조회·예약을 하려고 코레일 아이디·비밀번호와 검색 조건을 코레일 서버로 보냅니다. 결제는 사용자가 코레일에서 직접 합니다.</li>
<li><b>Google Firebase Cloud Messaging</b>(처리 위탁): 휴대폰 알림을 켠 경우 알림 토큰과 "새 알림이 왔어요" 문구, 알림 번호만 보냅니다. 알림 내용은 보내지 않고 앱 알림함에서 봅니다.</li>
<li><b>Cloudflare</b>(네트워크 제공): 앱과 서버 사이의 연결을 중계합니다. 접속 IP와 요청이 Cloudflare를 거칩니다.</li>
</ul>
<p>그 밖에는 누구에게도 제공하거나 판매하지 않습니다. 법령에 따른 요구가 있으면 그 범위에서만 따릅니다.</p>

<h2>3. 저장 위치와 보호</h2>
<p>서버 데이터는 운영자가 관리하는 서버의 Redis(자리 찾기·코레일 로그인 정보)와 SQLite(계정·알림·요청 횟수 제한)에 저장합니다.
코레일 비밀번호와 알림 토큰은 서버 비밀키로 암호화하고, 앱 비밀번호와 세션 토큰은 해시로만 저장합니다. 모든 통신은 HTTPS로 합니다.</p>

<h2>4. 탈퇴와 삭제</h2>
<p><b>앱에서 탈퇴</b></p>
{_in_app_steps()}
<p>지우는 정보:</p>
{_deleted_list()}
<p>남는 것: 검색이 끝났다는 표시(끝난 이유·시각, 개인정보 없음, 7일 뒤 자동 삭제)와 서버 로그의 해당 줄(돌려 쓰며 덮어쓸 때까지).
코레일에서 이미 결제한 예약은 코레일의 기록이라 이 앱에서 지우지 않습니다.
앱을 삭제하면 휴대폰에 남은 정보도 함께 지워집니다.</p>
<p><b>웹으로 탈퇴 요청</b>: 앱을 쓸 수 없으면 아래 연락처로 앱 아이디와 함께 탈퇴를 요청해 주세요.
운영자가 본인 확인 뒤 지체 없이, 늦어도 30일 안에 위와 같이 지우고 결과를 알려 드립니다.
<a href="/delete-account">회원 탈퇴 안내</a>에도 같은 내용이 있습니다.</p>

<h2>5. 이용자의 권리</h2>
<p>저장된 정보의 열람, 정정, 삭제, 처리 정지를 요청할 수 있습니다. 코레일 계정 연결 해제와 알림 끄기는 앱 설정에서 바로 할 수 있습니다.</p>

<h2>6. 문의</h2>
{_contact(contact)}

<h2>7. 바뀌는 경우</h2>
<p>저장하거나 보내는 정보가 바뀌면 이 문서를 고치고 시행일을 새로 적습니다.</p>
""",
    )


def delete_account_page(contact: str | None) -> str:
    return _page(
        "회원 탈퇴 안내",
        f"""<h1>자리났다 회원 탈퇴 안내</h1>
<p class="muted">시행일: {EFFECTIVE_DATE}</p>

<h2>앱에서 바로 탈퇴하기</h2>
{_in_app_steps()}

<h2>웹으로 탈퇴 요청하기</h2>
<p>앱을 지웠거나 로그인할 수 없으면 아래 연락처로 <b>앱 아이디</b>와 함께 탈퇴를 요청해 주세요.
운영자가 본인 확인 뒤 지체 없이, 늦어도 30일 안에 지우고 결과를 알려 드립니다.</p>
{_contact(contact)}

<h2>지우는 정보</h2>
{_deleted_list()}

<h2>남는 정보</h2>
<ul>
<li>검색이 끝났다는 표시(끝난 이유·시각, 개인정보 없음): 7일 뒤 자동 삭제</li>
<li>서버 로그의 해당 줄: 파일을 돌려 쓰며 덮어쓸 때까지(10MB씩 3개)</li>
<li>코레일에서 이미 결제한 예약: 코레일의 기록이라 코레일에서 관리</li>
</ul>
<p>자세한 내용은 <a href="/privacy">개인정보처리방침</a>을 봐 주세요.</p>
""",
    )
