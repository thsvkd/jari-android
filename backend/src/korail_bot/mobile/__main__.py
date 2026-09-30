"""python -m korail_bot.mobile {serve,invite,admin,running}; no BOTTOKEN required."""

import argparse
import json
import os
import signal
import sys

from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.identity import IdentityStore


def main():
    parser = argparse.ArgumentParser(description="Standalone 자리났다 API")
    commands = parser.add_subparsers(dest="command", required=True)
    invite = commands.add_parser("invite", help="Create a one-use app invitation")
    invite.add_argument("--ttl-hours", type=int, default=24)
    admin = commands.add_parser("admin", help="Create or update the operator app account")
    admin.add_argument("--username", required=True)
    admin.add_argument(
        "--password",
        help="Operator password; omit to read MOBILE_ADMIN_PASSWORD or stdin",
    )
    running = commands.add_parser(
        "running",
        help="List the running searches and whether a restart would resume each (read-only)",
    )
    running.add_argument("--json", action="store_true", help="One JSON object, for scripts")
    serve = commands.add_parser("serve", help="Run one API and booking worker owner")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8081)
    serve.add_argument(
        "--auth-only", action="store_true", help="Explicitly disable all railway features"
    )
    args = parser.parse_args()
    try:
        config = MobileConfig.from_env(
            auth_only=args.command in {"invite", "admin"} or getattr(args, "auth_only", False)
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
        if args.command == "running":
            print_running(config, as_json=args.json)
            return
        from waitress import serve as serve_wsgi

        from korail_bot.mobile.runtime import MobileRuntime

        runtime = MobileRuntime(config, on_lease_lost=lambda: os.kill(os.getpid(), signal.SIGTERM))

        def shutdown(signum, frame):
            # Once: a second SIGTERM (docker stop during a lost-lease exit)
            # must not cut short the teardown that releases the lease.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
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
            # However the server ended - a signal, or start() or the server
            # failing - a SIGTERM now (docker stop) must not cut short the
            # teardown that stops the searches and releases the lease. One
            # handled before SIG_IGN takes effect raises KeyboardInterrupt
            # here; the teardown runs all the same.
            try:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            finally:
                runtime.stop()
    except ValueError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        pass


def print_running(config, *, as_json):
    """
    What scripts/deploy-backend.sh reads before and after it restarts the API.

    Run beside the API (docker compose exec), against the same Redis: it only
    reads. Each record says which run it belongs to - a restart that brought
    it back gives it the new run's - whether a worker is performing it and
    whether that worker has logged in.

    With --json the answer is one line, RUNNING=<json>, and everything else
    this process says - a log warning about a login it cannot decrypt, say -
    goes to stderr: the script reads that one line, and a warning ahead of the
    JSON used to make the whole listing unreadable.
    """
    import logging
    import time

    from korail_bot.mobile.process import MobileReservationService
    from korail_bot.mobile.storage import MobileStorage
    from korail_bot.utils.logger import LoggerFactory

    # Configured now, so that no later import configures it afresh on stdout.
    LoggerFactory.configure_root_logger()
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout:
            handler.setStream(sys.stderr)

    storage = MobileStorage(secret=config.secret, url=config.redis_url)
    try:
        service = MobileReservationService(storage, None, config)
        searches, unreadable = service.describe_running()
        stopped = service.describe_stopped()
        ended = service.describe_ended()
    finally:
        storage.close()
    if as_json:
        listing = {
            "now": int(time.time()),
            "searches": searches,
            # Running records there that this build cannot parse: every
            # reader skips them, so a restart would lose them without a word.
            "unreadable": unreadable,
            "stopped": stopped,
            "ended": ended,
        }
        print("RUNNING=" + json.dumps(listing))
        return
    for row in searches:
        print(
            f"{row['id']} run={row['runId']} pid={row['pid']} "
            f"worker={'alive' if row['workerAlive'] else 'gone'} "
            f"logged_in={'yes' if row['loggedIn'] else 'no'} "
            f"resumable={'yes' if row['resumable'] else 'no (' + row['reason'] + ')'} "
            f"login_ttl={row['credentialTtlSeconds']}s"
        )
    print(f"{len(searches)} running" + (f", {unreadable} unreadable" if unreadable else ""))


if __name__ == "__main__":
    main()
