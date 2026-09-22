"""Subprocess lifecycle scoped to this mobile runtime on Windows and Linux."""

import os
import subprocess
import sys
import threading

import psutil

from korail_bot.mobile.identity import digest
from korail_bot.services.reservation_service import ReservationService


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

    def cancel_reservation(self, chat_id):
        with self.operation_lock(chat_id), self.start_lock:
            record = self.storage.get_running_reservation(chat_id)
            if record is None:
                return False
            stopped = self._terminate_search_process(record.process_id)
            if not stopped and self._is_running(record.process_id):
                return False
            self.storage.delete_running_reservation(chat_id)
            self.storage.delete_resume_credentials(chat_id)
            self.storage.delete_app_session_start(chat_id)
            session = self.storage.get_user_session(chat_id)
            if session:
                session.reset()
                self.storage.save_user_session(session)
            self.telegram.send_message(chat_id, "검색을 중지했어요.")
            return True

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
