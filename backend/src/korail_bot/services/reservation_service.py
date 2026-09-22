"""Reservation orchestration service."""

import json
import os
import signal
import subprocess
import sys
import threading
import time

from korail_bot.config.settings import settings
from korail_bot.models import (
    DeadSearch,
    DeathCause,
    RunningReservation,
    TrainSearchParams,
)
from korail_bot.services.telegram_service import MessageTemplates, TelegramService
from korail_bot.storage.base import StorageInterface
from korail_bot.utils.logger import get_logger
from korail_bot.utils.privacy import mask_phones
from korail_bot.utils.timezone import format_user_datetime

logger = get_logger(__name__)

# Names the thread that feeds a child its credentials and seat plan, so it is
# identifiable in a thread dump and joinable by a test.
_STDIN_THREAD_PREFIX = "search-stdin-"


class ReservationService:
    """
    Service for managing train reservations.

    Orchestrates the reservation process including:
    - Starting background reservation processes
    - Managing running reservations
    - Cancelling reservations
    """

    def __init__(self, storage: StorageInterface, telegram_service: TelegramService):
        """
        Initialize reservation service.

        Args:
            storage: Storage interface for state management
            telegram_service: Telegram service for notifications
        """
        self.storage = storage
        self.telegram = telegram_service
        # Handles on the search processes this run started, keyed by PID.
        # Kept so that a finished search can be collected instead of lingering
        # as a zombie, and so shutdown knows exactly what to stop.
        self._children: dict[int, subprocess.Popen] = {}
        # Set once the app is on its way down. Shutdown kills the searches and
        # deliberately leaves their records for the next run to resume, which
        # is indistinguishable from a crash to anything watching for one - so
        # the watchdog is told to stop looking rather than left to announce
        # every search as dead on the way out.
        self._shutting_down = False

    def start_reservation_process(
        self,
        chat_id: int,
        username: str,
        password: str,
        search_params: TrainSearchParams,
        resumed: bool = False,
        resumed_message: str | None = None,
    ) -> bool:
        """
        Start a background reservation process.

        Args:
            chat_id: Telegram chat ID
            username: Korail username
            password: Korail password
            search_params: Train search parameters
            resumed: True when picking up a search that was interrupted, which
                     only changes what the user is told
            resumed_message: What to tell the user instead of the default
                     resume notice. A restart and a user pressing "resume" are
                     both resumptions, but explaining one as the other is a
                     small lie the user has no way to check.

        Returns:
            True if process started successfully
        """
        try:
            self._reap_children()

            # A search already recorded for this chat must not be replaced.
            # Resuming is the one case that legitimately starts a process for a
            # chat that still has a record: reconcile_after_restart has already
            # terminated the process that record pointed at.
            if not resumed and not self._may_start(chat_id):
                return False

            # A resumed search is exempt: it was counted when it first
            # started, and refusing to bring it back after a restart would
            # quietly lose it.
            if not resumed and not self._under_concurrency_limit(chat_id):
                return False

            # Credentials are deliberately absent from argv: anything passed on
            # a command line is world-readable through `ps` and /proc. They are
            # written to the child's stdin instead.
            arguments = [
                search_params.dep_date,
                search_params.src_locate,
                search_params.dst_locate,
                search_params.dep_time,
                search_params.train_type,
                search_params.special_option,
                str(chat_id),
                search_params.max_dep_time,
                str(search_params.passenger_count),
                search_params.seat_strategy,
                # Comma-joined so the slot stays one argument however many
                # trains were picked; empty means watch the whole window.
                # Not secret, unlike the credentials above - a train number is
                # public timetable data.
                ",".join(search_params.train_numbers),
                # Which seats will do, already flattened to one string by
                # SeatPreference.encode. Appended last, and every addition
                # goes last, so the positions before it keep their meaning -
                # which is what lets a search started by an older build resume
                # against the new one.
                search_params.seat_preference,
                # The seat plan itself travels on stdin beside the credentials:
                # a whole-formation plan is far past the 128KB a single argv
                # string may hold. The slot keeps its position, because every
                # argument before it would change meaning otherwise and a
                # resumed search depends on those positions, and it carries a
                # marker saying where the plan really is. A child from an older
                # build reads the marker as a plan, cannot parse it, and refuses
                # to start - which is the outcome to want: it has no way to
                # honour the plan, and searching for any free seat would book
                # one the user never picked.
                "stdin" if search_params.seat_plan_json else "",
            ]

            # Start background process.
            #
            # start_new_session puts the search in a session of its own, so a
            # Ctrl-C in the terminal running the bot reaches the bot alone.
            # Otherwise the search dies from the same keystroke, in the middle
            # of whatever it was doing and without the bot ever knowing - and
            # a search stopped that way behaves differently from one stopped by
            # /cancel or by a service manager. Ending a search is the bot's job,
            # and this leaves it the only way it happens.
            #
            # sys.executable, not "python": the interpreter running the app is
            # the one with korail_bot installed. Resolving the name through
            # PATH finds whichever python comes first there, which in a service
            # started outside an activated virtualenv is the system one - and
            # that one exits immediately with ModuleNotFoundError.
            cmd = self._process_command(arguments)
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, **self._process_options())
            self._children[proc.pid] = proc

            # Popen.stdin is Optional in general and never None here, because
            # stdin=PIPE is what asked for it. Bound once so the three uses
            # below do not each have to restate that, and raised rather than
            # ignored if that ever stops being true: a search whose child
            # never receives its credentials would sit there logging in
            # nowhere.
            child_stdin = proc.stdin
            if child_stdin is None:
                raise RuntimeError(f"No stdin pipe for search process {proc.pid}")

            # Hand over credentials and the seat plan, then close the pipe so
            # the child stops waiting.
            #
            # A child that died on startup makes this a write to a pipe with no
            # reader. That is not the failure worth reporting - the death is -
            # so it is noted and left to the check below to describe.
            # Written from a thread, because a seat plan runs to hundreds of
            # kilobytes and the pipe buffer holds 64KB: the rest of the write
            # blocks until the child drains it. The mobile runtime makes this
            # call while holding one lock shared by every user, so a child that
            # neither reads nor exits would otherwise freeze every start,
            # cancel and dead-search sweep on the server, with nothing left to
            # time it out. _confirm_started below still decides whether the
            # child lives; a child that died takes EPIPE here and ends the
            # thread.
            handover = json.dumps(
                {
                    "username": username,
                    "password": password,
                    "seat_plan": search_params.seat_plan_json,
                }
            )

            def hand_over():
                try:
                    child_stdin.write(handover.encode("utf-8") + b"\n")
                    child_stdin.flush()
                except (BrokenPipeError, OSError) as e:
                    logger.warning(f"Could not hand credentials to process {proc.pid}: {e}")
                finally:
                    child_stdin.close()

            threading.Thread(
                target=hand_over, name=f"{_STDIN_THREAD_PREFIX}{proc.pid}", daemon=True
            ).start()

            # Keep what a restart would need to log in again. Deleted as soon
            # as the search ends, whichever way it ends.
            #
            # Written before the search is confirmed, not after: a search that
            # fails to start is the one most worth offering to try again, and
            # the offer needs a login to make good on. Left behind by a failure
            # here, cleared when the user drops the dead search, and expiring
            # on its own either way.
            if settings.RESUME_ON_RESTART:
                self.storage.save_resume_credentials(chat_id, username, password)

            # Only now is the search real. Everything below records it and
            # tells people about it, and doing that for a process that is
            # already gone is how a user ends up waiting all day on nothing.
            if not self._confirm_started(proc, chat_id, username, search_params):
                return False

            logger.info(f"Started reservation process for chat_id={chat_id}, pid={proc.pid}")

            # Save running reservation, stamped with this run so that a later
            # start can tell whether the process is still around.
            reservation = RunningReservation(
                chat_id=chat_id,
                process_id=proc.pid,
                korail_id=username,
                search_params=search_params,
                run_id=settings.RUN_ID,
            )
            self.storage.save_running_reservation(reservation)

            # A search is running for this chat again, so any earlier one that
            # died is no longer something to decide about. Its buttons would
            # only be able to report that a search is already going.
            self.storage.delete_dead_search(chat_id)

            # Update user session
            session = self.storage.get_user_session(chat_id)
            if session:
                session.process_id = proc.pid
                self.storage.save_user_session(session)

            if resumed:
                self.telegram.send_message(
                    chat_id,
                    resumed_message
                    or MessageTemplates.RESERVATION_RESUMED.format(
                        srcLocate=search_params.src_locate,
                        dstLocate=search_params.dst_locate,
                        depDate=search_params.dep_date,
                    ),
                )
            else:
                # Send confirmation to user
                self.telegram.send_message(chat_id, MessageTemplates.reservation_started())

            return True

        except Exception as e:
            logger.error(f"Failed to start reservation process: {e}")
            return False

    def _process_command(self, arguments):
        return [sys.executable, "-m", "korail_bot.telegramBot.telebotBackProcess", *arguments]

    def _process_options(self):
        return {"start_new_session": True}

    def _under_concurrency_limit(self, chat_id: int) -> bool:
        """
        Check that one more search would not put the server over the ceiling.

        Korail sees one IP, not one user. The thing that gets rate limited or
        blocked is Korail's server, which takes every search running here down
        with it.

        Returns:
            True when there is room for another search
        """
        limit = settings.MAX_CONCURRENT_SEARCHES
        if limit <= 0:
            return True

        try:
            running = len(self.storage.get_all_running_reservations())
        except Exception as e:
            # Not a reason to refuse: failing to read the count is a storage
            # problem, and the search is what the user is waiting for.
            logger.error(f"Could not count running searches: {e}")
            return True

        if running < limit:
            return True

        logger.warning(
            f"Refusing a search for chat_id={chat_id}: {running} already running, limit is {limit}"
        )
        from korail_bot.telegramBot.messages import Messages

        self.telegram.send_message(chat_id, Messages.SERVER_BUSY.format(limit=limit))
        return False

    def _confirm_started(
        self,
        proc: subprocess.Popen,
        chat_id: int,
        username: str,
        search_params: TrainSearchParams,
    ) -> bool:
        """
        Check that a freshly spawned search is actually running.

        Popen returning is not evidence of anything: it reports that a process
        was created, not that the interpreter found the module, imported it, or
        survived the attempt. A search that cannot start dies within
        milliseconds, and without this the app would go on to announce it,
        record it, and leave the user waiting on a process that no longer
        exists - which is exactly what happened once.

        Args:
            proc: The process just spawned
            chat_id: Telegram chat ID
            username: Korail username, for the record kept if it died
            search_params: What the search was to do

        Returns:
            True when the process is still alive after the grace period
        """
        grace = settings.PROCESS_START_GRACE_SECONDS
        if grace > 0:
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                # Still running, which is what a working search looks like.
                return True
        elif proc.poll() is None:
            return True

        self._forget_child(proc.pid)
        logger.error(
            f"Search process for chat_id={chat_id} exited immediately "
            f"with code {proc.returncode} - treating it as failed to start"
        )
        self._record_death(chat_id, username, search_params, DeathCause.START_FAILED)
        return False

    # ==================== Searches that stopped on their own ====================

    def _record_death(
        self,
        chat_id: int,
        korail_id: str,
        search_params: TrainSearchParams,
        cause: DeathCause,
    ) -> None:
        """
        Put a stopped search where the user can act on it, and say so.

        The record moves out of the running reservations, because it is not
        one and everything reading them would be misled - /status most of all.
        It is not simply deleted either: the user asked for a search, the
        search did not happen, and the details are what starting it again
        needs.
        """
        resumable = bool(
            settings.RESUME_ON_RESTART and self.storage.get_resume_credentials(chat_id)
        )

        dead = DeadSearch(
            chat_id=chat_id,
            korail_id=korail_id,
            search_params=search_params,
            cause=cause,
            resumable=resumable,
        )

        try:
            self.storage.save_dead_search(dead)
            self.storage.delete_running_reservation(chat_id)

            session = self.storage.get_user_session(chat_id)
            if session:
                session.reset()
                self.storage.save_user_session(session)
        except Exception as e:
            logger.error(f"Could not record the dead search for chat_id={chat_id}: {e}")

        self._announce_death(dead)

    def _announce_death(self, dead: DeadSearch) -> None:
        """Tell the user their search stopped, and offer what to do about it."""
        from korail_bot.telegramBot import keyboards
        from korail_bot.telegramBot.messages import Messages

        causes = {
            DeathCause.START_FAILED: Messages.SEARCH_DIED_CAUSE_START_FAILED,
            DeathCause.CRASHED: Messages.SEARCH_DIED_CAUSE_CRASHED,
        }
        params = dead.search_params
        timezone_name = self.storage.get_user_timezone(dead.chat_id)

        text = Messages.SEARCH_DIED.format(
            causeLine=causes.get(dead.cause, Messages.SEARCH_DIED_CAUSE_CRASHED),
            srcLocate=params.src_locate,
            dstLocate=params.dst_locate,
            depDate=params.dep_date,
            depTime=params.dep_time[:4],
            maxDepTime=params.max_dep_time,
            passengerCount=params.passenger_count,
            watch=self._describe_watch(params),
            diedAt=format_user_datetime(dead.died_at, timezone_name, "%m/%d %H:%M"),
        )
        if not dead.resumable:
            text += Messages.SEARCH_DIED_NOT_RESUMABLE

        self.telegram.send_message(
            dead.chat_id,
            text,
            reply_markup=keyboards.dead_search_keyboard(dead.resumable),
        )

    def detect_dead_searches(self) -> int:
        """
        Find searches that stopped without saying so, and report them.

        A search that ends normally - a seat booked, or the attempt abandoned -
        calls back, and the callback is what clears its record. So a record of
        this run whose process is gone means the process died: killed by the
        OOM killer, by a stray signal, or by a crash deep enough to skip the
        error path. Nothing else notices, and the user goes on waiting.

        Records from earlier runs are not this method's business: they belong
        to reconcile_after_restart, which runs at startup and knows to resume
        them rather than report them.

        Returns:
            How many dead searches were found
        """
        if self._shutting_down:
            # Every search is about to be killed on purpose, records left
            # behind for the next run to resume. Reporting those as deaths
            # would move them where the next run cannot find them.
            return 0

        self._reap_children()

        try:
            reservations = self.storage.get_all_running_reservations()
        except Exception as e:
            logger.error(f"Could not read running reservations: {e}", exc_info=True)
            return 0

        found = 0
        for reservation in reservations:
            if reservation.is_stale(settings.RUN_ID):
                continue
            if self._owns_process(reservation.process_id):
                continue

            # Read the record again before acting on it. A search that has
            # just finished sends its callback and then exits, and between
            # those two the process is gone while the record is still there -
            # a gap this would otherwise report as a crash.
            if not self.storage.get_running_reservation(reservation.chat_id):
                continue

            logger.warning(
                f"Search process {reservation.process_id} for "
                f"chat_id={reservation.chat_id} is gone without a callback"
            )
            self._forget_child(reservation.process_id)
            self._record_death(
                reservation.chat_id,
                reservation.korail_id,
                reservation.search_params,
                DeathCause.CRASHED,
            )
            found += 1

        return found

    def resume_dead_search(self, chat_id: int) -> bool:
        """
        Start a stopped search again, on the same terms.

        Returns:
            True when a new search process is running
        """
        from korail_bot.telegramBot.messages import Messages

        dead = self.storage.get_dead_search(chat_id)
        if not dead:
            self.telegram.send_message(chat_id, Messages.SEARCH_DEAD_GONE)
            return False

        credentials = self.storage.get_resume_credentials(chat_id)
        if not credentials:
            logger.warning(f"Cannot resume the dead search for chat_id={chat_id}: no login stored")
            self.storage.delete_dead_search(chat_id)
            self.telegram.send_message(chat_id, Messages.SCHEDULE_NO_CREDENTIALS)
            return False

        # Cleared first. start_reservation_process refuses to run alongside a
        # record it would overwrite, and a failure here records a fresh death
        # that this stale one would sit on top of.
        self.storage.delete_dead_search(chat_id)

        username, password = credentials
        logger.info(f"Resuming the dead search for chat_id={chat_id}")

        started = self.start_reservation_process(
            chat_id=chat_id,
            username=username,
            password=password,
            search_params=dead.search_params,
            resumed=True,
            resumed_message=Messages.SEARCH_RESUMED.format(
                srcLocate=dead.search_params.src_locate,
                dstLocate=dead.search_params.dst_locate,
                depDate=dead.search_params.dep_date,
            ),
        )
        if not started:
            # Refusing usually comes with an explanation of its own: a search
            # already running says so, and one that dies on the way up is
            # announced as a death. What is left is the silent failure, and a
            # button press that produces nothing at all is worse than any of
            # them.
            if not self.storage.get_running_reservation(
                chat_id
            ) and not self.storage.get_dead_search(chat_id):
                self.telegram.send_message(chat_id, Messages.SEARCH_RESUME_FAILED)
            return False

        return True

    def discard_dead_search(self, chat_id: int) -> bool:
        """
        Drop a stopped search the user has finished with.

        Returns:
            True when there was one to drop
        """
        if not self.storage.get_dead_search(chat_id):
            return False

        self.storage.delete_dead_search(chat_id)
        self.storage.delete_resume_credentials(chat_id)
        self.storage.delete_app_session_start(chat_id)
        logger.info(f"Discarded the dead search for chat_id={chat_id}")
        return True

    # The placeholder PID used for reservations that never had a process.
    _NO_PROCESS = 9999999

    def _may_start(self, chat_id: int) -> bool:
        """
        Decide whether a new search may be started for this chat.

        Storage holds one running reservation per chat, so starting a second
        search overwrites the first record and leaves that process running with
        nothing tracking it - unkillable through /cancel and still hitting
        Korail on the same account. The conversation handler does check its own
        session state before getting here, but a session expires after
        SESSION_TTL_SECONDS while a search for a sold-out train can outlive it,
        and at that point the handler offers the user a fresh /start.

        Args:
            chat_id: Telegram chat ID

        Returns:
            True when nothing is recorded as running for this chat
        """
        existing = self.storage.get_running_reservation(chat_id)
        if not existing:
            return True

        logger.warning(
            f"Refusing to start a second search for chat_id={chat_id}: "
            f"pid={existing.process_id} is already recorded"
        )

        params = existing.search_params
        self.telegram.send_message(
            chat_id,
            MessageTemplates.ALREADY_RUNNING.format(
                depDate=params.dep_date,
                srcLocate=params.src_locate,
                dstLocate=params.dst_locate,
                depTime=params.dep_time[:4],
                trainTypeShow=params.train_type_display,
                specialInfoShow=params.special_option_display,
            ),
        )
        return False

    def is_search_alive(self, reservation: RunningReservation) -> bool:
        """
        Whether a process is still performing the search this record describes.

        The same question detect_dead_searches asks, asked one record at a
        time and without acting on the answer, so that a screen can say which
        of the two a running record is: a search being performed, or one whose
        process is gone and whose record nothing has cleared away yet.

        A record left by an earlier run counts as gone however its PID looks.
        The process that run started is not ours any more, and deciding what
        becomes of such a record belongs to reconcile_after_restart.

        Returns:
            True when something is still asking the operator for trains
        """
        if reservation.is_stale(settings.RUN_ID):
            return False
        return self._owns_process(reservation.process_id)

    def _owns_process(self, pid: int) -> bool:
        """
        Check that a PID really belongs to one of our search processes.

        PIDs get recycled. A record left behind by an earlier run can point at
        a PID the kernel has since handed to something else entirely, and
        signalling that would kill an innocent process.

        Returns:
            True when the PID may be signalled
        """
        if not os.path.isdir("/proc"):
            # No way to verify here; keep the previous behaviour rather than
            # silently refusing to cancel anything.
            return True

        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                return b"telebotBackProcess" in handle.read()
        except (FileNotFoundError, ProcessLookupError):
            return False
        except OSError as e:
            logger.warning(f"Could not inspect process {pid}: {e}")
            return False

    # How long a search process is given to act on SIGTERM before it is
    # killed outright, and how often it is checked in the meantime.
    _TERMINATE_GRACE_SECONDS = 3.0
    _TERMINATE_POLL_SECONDS = 0.05

    def _reap_children(self) -> None:
        """
        Collect the search processes that have already finished.

        A finished child stays in the process table until its parent picks up
        its exit status. The bot runs for weeks at a time and starts a process
        per search, so nothing may be left uncollected.
        """
        for pid, child in list(self._children.items()):
            if child.poll() is not None:
                del self._children[pid]
                logger.debug(f"Search process {pid} exited with {child.returncode}")

    def _forget_child(self, pid: int) -> None:
        """
        Drop our handle on a search process, collecting it on the way out.

        Dropping the handle without polling it first would strand a process
        that has exited but not yet been collected: nothing would be left that
        could ever collect it.
        """
        child = self._children.pop(pid, None)
        if child is not None:
            child.poll()

    def _is_running(self, pid: int) -> bool:
        """
        Check whether a signalled process is still executing.

        Our own children are asked through their process handle: a child that
        exited but has not been collected yet is still a process as far as
        kill() is concerned, and treating that zombie as alive would earn it a
        pointless SIGKILL.
        """
        child = self._children.get(pid)
        if child is not None:
            if child.poll() is None:
                return True
            del self._children[pid]
            return False

        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _wait_for_exit(self, pid: int, timeout: float) -> bool:
        """
        Wait up to `timeout` seconds for a process to go away.

        Returns:
            True if it exited within the timeout
        """
        deadline = time.monotonic() + timeout
        while True:
            if not self._is_running(pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self._TERMINATE_POLL_SECONDS)

    def _terminate_search_process(self, pid: int) -> bool:
        """
        Stop a search process, if it is still ours to stop.

        SIGTERM is a request, and a search blocked on a Korail request that
        never answers may not get around to it. A search left running is not
        harmless: it keeps asking Korail for seats, and it reports what it
        finds to an HTTP endpoint that may no longer exist, so the reservation
        it wins would expire unpaid without the user ever hearing about it.
        That is worth escalating to SIGKILL for.

        Args:
            pid: Process ID recorded for the reservation

        Returns:
            True if the process was stopped (or was already gone after being
            signalled)
        """
        if pid == self._NO_PROCESS:
            return False

        if not self._owns_process(pid):
            logger.info(f"Process {pid} is gone or no longer one of ours - not signalling it")
            self._forget_child(pid)
            return False

        try:
            os.kill(pid, signal.SIGTERM)
            logger.info(f"Asked search process {pid} to stop")
        except ProcessLookupError:
            logger.warning(f"Process {pid} not found")
            self._forget_child(pid)
            return False
        except OSError as e:
            logger.error(f"Failed to signal process {pid}: {e}")
            return False

        if self._wait_for_exit(pid, self._TERMINATE_GRACE_SECONDS):
            logger.info(f"Search process {pid} stopped")
            return True

        logger.warning(
            f"Search process {pid} did not stop within "
            f"{self._TERMINATE_GRACE_SECONDS:.0f}s - killing it"
        )
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as e:
            logger.error(f"Failed to kill process {pid}: {e}")
            return False

        self._wait_for_exit(pid, self._TERMINATE_GRACE_SECONDS)
        return True

    def shutdown(self) -> None:
        """
        Stop every search this run started.

        A search reports back to an HTTP endpoint served by this very process,
        so one that outlives the app is worse than useless: it goes on asking
        Korail for seats, and the reservation it eventually wins is announced
        to nobody and expires unpaid. The records in Redis are left in place -
        they are what the next start reads to pick the searches back up.
        """
        self._shutting_down = True
        self._reap_children()

        running = [pid for pid in list(self._children) if self._is_running(pid)]
        if not running:
            return

        logger.info(f"Stopping {len(running)} running search process(es)")
        for pid in running:
            try:
                self._terminate_search_process(pid)
            except Exception as e:
                logger.error(f"Failed to stop search process {pid}: {e}")

    def reconcile_after_restart(self) -> dict:
        """
        Deal with searches left behind by a previous run of the application.

        The search runs in a child process, so restarting the app abandons it
        while its record stays in Redis - /status would report a search that
        nothing is performing. Every record from an earlier run is either
        resumed or cleaned up and reported.

        Returns:
            Counts keyed 'resumed', 'interrupted' and 'failed'
        """
        from korail_bot.telegramBot.messages import Messages

        summary = {"resumed": 0, "interrupted": 0, "failed": 0}

        try:
            reservations = self.storage.get_all_running_reservations()
        except Exception as e:
            logger.error(f"Could not read running reservations: {e}", exc_info=True)
            return summary

        stale = [r for r in reservations if r.is_stale(settings.RUN_ID)]
        if not stale:
            return summary

        logger.info(f"Found {len(stale)} reservation(s) left over from an earlier run")

        for reservation in stale:
            chat_id = reservation.chat_id
            try:
                # A previous run may have left the process alive - a bare
                # restart of the app does not kill its children.
                self._terminate_search_process(reservation.process_id)

                if self._resume(reservation):
                    summary["resumed"] += 1
                    continue

                self._abandon(reservation, Messages)
                summary["interrupted"] += 1
            except Exception as e:
                logger.error(
                    f"Failed to reconcile reservation for chat_id={chat_id}: {e}", exc_info=True
                )
                summary["failed"] += 1

        logger.info(
            f"Restart recovery: {summary['resumed']} resumed, "
            f"{summary['interrupted']} interrupted, {summary['failed']} failed"
        )
        return summary

    def _resume(self, reservation: RunningReservation) -> bool:
        """
        Try to pick an interrupted search back up.

        Args:
            reservation: Record left behind by an earlier run

        Returns:
            True when a new search process was started
        """
        chat_id = reservation.chat_id

        if not settings.RESUME_ON_RESTART:
            return False

        # Random seating reserves one seat at a time. Restarting that search
        # from the beginning would try to book seats the user already holds,
        # so it is left to the user to decide.
        if self.storage.get_partial_reservations(chat_id):
            logger.info(f"Not resuming chat_id={chat_id}: seats are already reserved")
            return False

        credentials = self.storage.get_resume_credentials(chat_id)
        if not credentials:
            logger.info(f"Not resuming chat_id={chat_id}: no usable credentials")
            return False

        username, password = credentials
        logger.info(f"Resuming interrupted search for chat_id={chat_id}")

        return self.start_reservation_process(
            chat_id=chat_id,
            username=username,
            password=password,
            search_params=reservation.search_params,
            resumed=True,
        )

    def _abandon(self, reservation: RunningReservation, messages) -> None:
        """Tell the user their search is over and drop every trace of it."""
        chat_id = reservation.chat_id
        params = reservation.search_params

        if self.storage.get_partial_reservations(chat_id):
            text = messages.RESERVATION_INTERRUPTED_PARTIAL.format(
                paymentUrl=settings.KORAIL_PAYMENT_URL
            )
        else:
            text = messages.RESERVATION_INTERRUPTED.format(
                srcLocate=params.src_locate, dstLocate=params.dst_locate, depDate=params.dep_date
            )

        self.telegram.send_message(chat_id, text)

        self.storage.delete_running_reservation(chat_id)
        self.storage.delete_resume_credentials(chat_id)
        self.storage.delete_app_session_start(chat_id)

        session = self.storage.get_user_session(chat_id)
        if session:
            session.reset()
            self.storage.save_user_session(session)

    def cancel_reservation(self, chat_id: int) -> bool:
        """
        Cancel a running reservation.

        Args:
            chat_id: Telegram chat ID

        Returns:
            True if cancelled successfully
        """
        try:
            # Get running reservation
            reservation = self.storage.get_running_reservation(chat_id)
            if not reservation:
                logger.warning(f"No running reservation found for chat_id={chat_id}")
                # Say so. The caller used to report a cancellation regardless,
                # so /cancel with nothing running answered "예약이 취소되었습니다"
                # - which tells a user whose search has quietly died that it
                # was still going right up until they stopped it.
                self.telegram.send_message(chat_id, MessageTemplates.ERROR_NO_PROGRESS)
                return False

            # The record goes first, before the process it points at. Stopping
            # a search takes a few seconds - longer if it has to be killed -
            # and for all of them the record would say a search is running
            # while its process is on the way out, which is exactly the shape
            # the watchdog reports as a crash. The cancellation is decided by
            # this point; the record is already wrong.
            self.storage.delete_running_reservation(chat_id)
            self._terminate_search_process(reservation.process_id)

            # Clean up storage
            self.storage.delete_resume_credentials(chat_id)
            self.storage.delete_app_session_start(chat_id)

            # Reset user session
            session = self.storage.get_user_session(chat_id)
            if session:
                session.reset()
                self.storage.save_user_session(session)

            # Notify
            self.telegram.send_message(chat_id, MessageTemplates.reservation_cancelled())

            return True

        except Exception as e:
            logger.error(f"Error cancelling reservation: {e}")
            return False

    def cancel_all_reservations(self, admin_chat_id: int) -> int:
        """
        Cancel all running reservations (admin function).

        Args:
            admin_chat_id: Admin's chat ID for notification

        Returns:
            Number of reservations cancelled
        """
        reservations = self.storage.get_all_running_reservations()
        count = 0

        for reservation in reservations:
            try:
                self._terminate_search_process(reservation.process_id)
                self.storage.delete_resume_credentials(reservation.chat_id)
                self.storage.delete_app_session_start(reservation.chat_id)

                # Notify user
                self.telegram.send_message(
                    reservation.chat_id,
                    "관리자에 의해 실행중이던 예약이 강제 종료됩니다. 꼬우면 관리자에게 연락하세요.",
                )

                # Reset session
                session = self.storage.get_user_session(reservation.chat_id)
                if session:
                    session.reset()
                    self.storage.save_user_session(session)

                # Clean up
                self.storage.delete_running_reservation(reservation.chat_id)
                count += 1

            except Exception as e:
                logger.error(f"Error cancelling reservation {reservation.chat_id}: {e}")

        # Notify admin
        korail_ids = mask_phones(r.korail_id for r in reservations)
        self.telegram.send_message(
            admin_chat_id,
            f"총 {count}개의 진행중인 예약을 종료했습니다. 이용중이던 사용자 : {korail_ids}",
        )

        return count

    def get_status(self, chat_id: int) -> str:
        """
        Get reservation status for the requesting user.

        /status is open to every user, so it reports only the caller's own
        reservation plus an aggregate count. Other users' Korail IDs are
        phone numbers and are never disclosed here.

        Args:
            chat_id: Chat ID requesting status

        Returns:
            Status message
        """
        from korail_bot.utils.timezone import format_railway_window, format_user_datetime

        self._reap_children()
        timezone_name = self.storage.get_user_timezone(chat_id)

        reservations = self.storage.get_all_running_reservations()
        total = len(reservations)
        mine = next((r for r in reservations if r.chat_id == chat_id), None)

        if not mine:
            # A search that died is the one thing here the user most needs to
            # know, and reporting "nothing running" would be true in the least
            # useful way: they think a search is going on, and it is the fact
            # that it stopped that they are missing.
            dead = self.storage.get_dead_search(chat_id)
            if dead:
                params = dead.search_params
                return (
                    "⚠️ 검색이 멈춰 있습니다.\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"멈춘 시각: {format_user_datetime(dead.died_at, timezone_name)}\n"
                    f"구간: {params.src_locate} → {params.dst_locate}\n"
                    f"검색 시각:\n{format_railway_window(params.dep_date, params.dep_time, params.max_dep_time, timezone_name)}\n"
                    f"인원: {params.passenger_count}명\n"
                    f"감시: {self._describe_watch(params)}\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    + (
                        "알림 메시지의 버튼으로 재개하거나 정리할 수 있습니다."
                        if dead.resumable
                        else "저장된 로그인 정보가 없어 재개할 수 없습니다.\n/start 로 다시 시작해주세요."
                    )
                )

            # A search booked for later is not running, but it is very much
            # something the user has going on - and /status saying "nothing"
            # would read as the booking having been lost.
            scheduled = self.storage.get_scheduled_search(chat_id)
            if scheduled:
                params = scheduled.search_params
                return (
                    "⏰ 검색이 예약되어 있습니다.\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"시작 시각: {format_user_datetime(scheduled.start_at, timezone_name)}\n"
                    f"구간: {params.src_locate} → {params.dst_locate}\n"
                    f"검색 시각:\n{format_railway_window(params.dep_date, params.dep_time, params.max_dep_time, timezone_name)}\n"
                    f"인원: {params.passenger_count}명\n"
                    f"감시: {self._describe_watch(params)}\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "그때까지는 아무 요청도 보내지 않습니다.\n\n"
                    "취소하려면 /cancel 을 입력하세요."
                )

            return (
                "진행중인 예약이 없습니다.\n"
                f"(현재 서버 전체 실행중인 예약: {total}개)\n\n"
                "/start 를 입력하여 예약을 시작하세요."
            )

        params = mine.search_params
        return (
            "🔎 예약 검색이 진행중입니다.\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"출발일: {params.dep_date}\n"
            f"구간: {params.src_locate} → {params.dst_locate}\n"
            f"검색 시각:\n{format_railway_window(params.dep_date, params.dep_time, params.max_dep_time, timezone_name)}\n"
            f"인원: {params.passenger_count}명\n"
            f"감시: {self._describe_watch(params)}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"(현재 서버 전체 실행중인 예약: {total}개)\n\n"
            "중단하려면 /cancel 을 입력하세요."
        )

    @staticmethod
    def _describe_watch(params: TrainSearchParams) -> str:
        """Whether the search is watching the whole window or picked trains."""
        if not params.train_numbers:
            return "시간대 전체"
        return f"지정 열차 {len(params.train_numbers)}개 ({', '.join(params.train_numbers)}번)"
