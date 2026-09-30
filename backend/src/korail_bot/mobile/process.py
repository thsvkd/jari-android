"""Subprocess lifecycle scoped to this mobile runtime on Windows and Linux."""

import os
import subprocess
import sys
import threading

import psutil

from korail_bot.mobile.identity import digest
from korail_bot.services.reservation_service import ReservationService
from korail_bot.utils.timezone import as_utc


class MobileReservationService(ReservationService):
    def __init__(self, storage, notifications, config):
        super().__init__(storage, notifications)
        self.config = config
        self.tag = "--jari-runtime=" + digest(config.database + (config.redis_url or ""))[:24]
        self.start_lock = threading.RLock()
        self.operation_locks = [threading.RLock() for _ in range(128)]

    def operation_lock(self, chat_id):
        return self.operation_locks[abs(chat_id) % len(self.operation_locks)]

    def start_reservation_process(self, *args, **kwargs):
        chat_id = kwargs["chat_id"] if "chat_id" in kwargs else args[0]
        with self.operation_lock(chat_id), self.start_lock:
            return super().start_reservation_process(*args, **kwargs)

    def detect_dead_searches(self):
        with self.start_lock:
            return super().detect_dead_searches()

    def _reconcile_record(self, reservation):
        # Retries come from the background loop while the API serves, so a
        # user can stop the search, or start another, in the middle of one.
        # Under the chat's lock the record read again inside is the one acted on.
        with self.operation_lock(reservation.chat_id), self.start_lock:
            return super()._reconcile_record(reservation)

    def _give_up_resuming(self, reservation):
        # Under the same lock, for the same reason: the try that failed last
        # has let go of it, and what is recorded now may be a new search.
        with self.operation_lock(reservation.chat_id), self.start_lock:
            return super()._give_up_resuming(reservation)

    def shutdown(self):
        # The flag first, then the lock: a start already under way finishes
        # and its child is among those killed below; one not yet begun sees
        # the flag and starts nothing. Either way no search outlives the app.
        self._shutting_down = True
        with self.start_lock:
            super().shutdown()

    def cancel_reservation(self, chat_id):
        with self.operation_lock(chat_id), self.start_lock:
            record = self.storage.get_running_reservation(chat_id)
            if record is None:
                return False
            stopped = self._terminate_search_process(record.process_id)
            if not stopped and self._is_running(record.process_id):
                return False
            # The mark first: a failure between the two must not leave a
            # record gone with nothing to say how.
            self.storage.mark_search_ended(chat_id, "cancelled", record.process_id)
            self.storage.delete_running_reservation(chat_id)
            # The worker may have been recording itself as stopped - Korail
            # unreachable, _stop_resumable - outside this lock when it was
            # killed. It is gone now; what it left must not outlive the
            # user's cancel as a search to resume.
            self.storage.delete_dead_search(chat_id)
            self.storage.delete_resume_credentials(chat_id)
            self.storage.delete_app_session_start(chat_id)
            session = self.storage.get_user_session(chat_id)
            if session:
                session.reset()
                self.storage.save_user_session(session)
            self.telegram.send_message(chat_id, "검색을 중지했어요.")
            return True

    def describe_running(self):
        """
        Every running record as the deploy check reads it: which run it
        belongs to, whether a worker is performing it, whether that worker has
        logged in to Korail, and whether a restart would bring it back.
        Nothing secret - no Korail ID, no login.

        Called from a process of its own (python -m korail_bot.mobile running)
        beside the API, so no record is this process's run: whether a worker
        is alive is asked of the process table, as _owns_process does.

        Returns:
            (rows, unreadable): unreadable counts the records this build could
            not parse, in the same read as the rows
        """
        records, unreadable = self.storage.read_running_reservations()
        rows = []
        for record in records:
            blocker = self.resume_blocker(record)
            rows.append(
                {
                    "id": record.chat_id,
                    "runId": record.run_id,
                    "pid": record.process_id,
                    "workerAlive": self._owns_process(record.process_id),
                    # Spawned is not searching: a resumed worker can spend
                    # minutes riding out Korail before it logs in, or fail to.
                    "loggedIn": self.storage.search_mark_pid(record.chat_id, "logged_in")
                    == record.process_id,
                    "resumable": blocker is None,
                    "reason": blocker,
                    "credentialTtlSeconds": self.storage.resume_credentials_ttl(record.chat_id),
                    "startedAt": as_utc(record.started_at).isoformat(),
                }
            )
        return sorted(rows, key=lambda row: row["id"]), unreadable

    def describe_ended(self):
        """
        The searches that ended on their own terms (booked, cancelled by the
        user, or stopped on a reported error), each with when. The deploy
        check counts a vanished record as finished only when it is here.
        """
        return sorted(
            (
                {"id": chat_id, "why": note["reason"], "at": note["at"]}
                for chat_id, note in self.storage.get_search_endings().items()
            ),
            key=lambda row: row["id"],
        )

    def describe_stopped(self):
        """
        The searches that ended without finishing, for the deploy check to
        tell from ones that simply finished while it waited: every stopped
        search still on file (its cause), and every record a restart cleaned
        up instead of resuming (its reason). 'at' is epoch seconds.
        """
        rows = [
            {"id": dead.chat_id, "why": dead.cause.value, "at": int(dead.died_at.timestamp())}
            for dead in self.storage.get_all_dead_searches()
        ]
        rows += [
            {"id": chat_id, "why": f"not_resumed_{note['reason']}", "at": note["at"]}
            for chat_id, note in self.storage.get_resume_abandonments().items()
        ]
        return sorted(rows, key=lambda row: (row["id"], row["at"]))

    def _process_command(self, arguments):
        return [sys.executable, "-m", "korail_bot.mobile.worker", *arguments, self.tag]

    def _process_options(self):
        env = os.environ.copy()
        for key in ("BOTTOKEN", "USERID", "USERPW", "SESSION_SECRET", "INTERNAL_CALLBACK_TOKEN"):
            env.pop(key, None)
        env.update(
            MOBILE_SECRET=self.config.secret,
            MOBILE_REDIS_URL=self.config.redis_url,
            MOBILE_DATA_DIR=os.path.dirname(self.config.database),
        )
        # A configured file in the parent's env must not override the exact
        # key passed by this runtime's config object in the child.
        env.pop("MOBILE_SECRET_FILE", None)
        if self.config.fcm_credentials:
            env["MOBILE_FCM_CREDENTIALS"] = self.config.fcm_credentials
        else:
            env.pop("MOBILE_FCM_CREDENTIALS", None)
        return (
            {"env": env, "creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"env": env, "start_new_session": True}
        )

    def _owns_process(self, pid):
        try:
            command = psutil.Process(pid).cmdline()
            return "korail_bot.mobile.worker" in command and self.tag in command
        except (psutil.Error, ValueError):
            return False

    def _is_running(self, pid):
        child = self._children.get(pid)
        if child is not None:
            return child.poll() is None
        return self._owns_process(pid)

    def _terminate_search_process(self, pid):
        if not self._owns_process(pid):
            self._forget_child(pid)
            return False
        try:
            process = psutil.Process(pid)
            process.terminate()
            try:
                process.wait(timeout=3)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            self._forget_child(pid)
            return True
        except psutil.NoSuchProcess:
            return True
        except psutil.Error:
            return False
