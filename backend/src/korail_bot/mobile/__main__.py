"""python -m korail_bot.mobile {serve,invite}; no BOTTOKEN required."""

import argparse
import signal

from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.identity import IdentityStore


def main():
    parser = argparse.ArgumentParser(description="Standalone Teum API")
    commands = parser.add_subparsers(dest="command", required=True)
    invite = commands.add_parser("invite", help="Create a one-use app invitation")
    invite.add_argument("--ttl-hours", type=int, default=24)
    serve = commands.add_parser("serve", help="Run one API and booking worker owner")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8081)
    serve.add_argument(
        "--auth-only", action="store_true", help="Explicitly disable all railway features"
    )
    args = parser.parse_args()
    try:
        config = MobileConfig.from_env(auth_only=args.command == "invite" or args.auth_only)
        if args.command == "invite":
            print(IdentityStore(config.database).create_invite(ttl=args.ttl_hours * 3600))
            return
        from waitress import serve as serve_wsgi

        from korail_bot.mobile.runtime import MobileRuntime

        runtime = MobileRuntime(config)

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
                max_request_body_size=16384,
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
