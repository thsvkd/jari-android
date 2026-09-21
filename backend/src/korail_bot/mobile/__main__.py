"""python -m korail_bot.mobile {serve,invite,admin}; no BOTTOKEN required."""

import argparse
import os
import signal
import sys

from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.identity import IdentityStore


def main():
    parser = argparse.ArgumentParser(description="Standalone Teum API")
    commands = parser.add_subparsers(dest="command", required=True)
    invite = commands.add_parser("invite", help="Create a one-use app invitation")
    invite.add_argument("--ttl-hours", type=int, default=24)
    admin = commands.add_parser("admin", help="Create or update the operator app account")
    admin.add_argument("--username", required=True)
    admin.add_argument(
        "--password",
        help="Operator password; omit to read MOBILE_ADMIN_PASSWORD or stdin",
    )
    serve = commands.add_parser("serve", help="Run one API and booking worker owner")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8081)
    serve.add_argument(
        "--auth-only", action="store_true", help="Explicitly disable all railway features"
    )
    args = parser.parse_args()
    try:
        config = MobileConfig.from_env(
            auth_only=args.command in {"invite", "admin"} or args.auth_only
        )
        if args.command == "invite":
            print(IdentityStore(config.database).create_invite(ttl=args.ttl_hours * 3600))
            return
        if args.command == "admin":
            password = args.password or os.environ.get("MOBILE_ADMIN_PASSWORD") or ""
            if not password and not sys.stdin.isatty():
                password = sys.stdin.readline().strip()
            if not password:
                parser.error("Set --password or MOBILE_ADMIN_PASSWORD")
            user = IdentityStore(config.database).ensure_admin(args.username, password)
            print(user["username"])
            return
        from waitress import serve as serve_wsgi

        from korail_bot.mobile.runtime import MobileRuntime

        runtime = MobileRuntime(
            config, on_lease_lost=lambda: os.kill(os.getpid(), signal.SIGTERM)
        )

        def shutdown(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
        try:
            runtime.start()
            serve_wsgi(
                runtime.app,
                host=args.host,
                port=args.port,
                threads=8,
                # One limit, set in create_app. A smaller number here rejects
                # a seat plan before Flask ever sees it.
                max_request_body_size=runtime.app.config["MAX_CONTENT_LENGTH"],
                clear_untrusted_proxy_headers=True,
            )
        finally:
            runtime.stop()
    except ValueError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
