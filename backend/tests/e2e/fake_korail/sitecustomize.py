"""e2e 스택이 PYTHONPATH 에 이 폴더를 올리면, 서버와 예약 워커가 시작할 때 가짜 코레일을 끼워요."""

import os
import site
import sys
import traceback

# Windows 에서 런처 없이 띄운 실제 인터프리터에 가상환경 패키지를 붙여요(.pth 까지 처리). stack.interpreter 참고.
if os.environ.get("JARI_E2E_VENV_SITE"):
    site.addsitedir(os.environ["JARI_E2E_VENV_SITE"])

if os.environ.get("JARI_E2E_FAKE_KORAIL") == "1":
    try:
        import jari_fake_korail

        jari_fake_korail.install()
    except BaseException:
        # 파이썬은 sitecustomize 의 예외를 경고만 하고 넘어가요. 그러면 실제 코레일로 나가니 여기서 멈춰요.
        traceback.print_exc()
        sys.stderr.write("e2e: 가짜 코레일을 끼우지 못해 멈춰요.\n")
        sys.stderr.flush()
        os._exit(97)
