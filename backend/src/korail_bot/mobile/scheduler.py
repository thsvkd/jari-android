"""Serialize scheduled starts with account unlink and booking cancellation."""

from korail_bot.services.scheduled_search_service import ScheduledSearchService


class MobileScheduledSearchService(ScheduledSearchService):
    def _fire(self, search, now):
        with self.reservation.operation_lock(search.chat_id):
            # tick may have read this record before an HTTP request cancelled
            # or replaced it. Revalidate after acquiring the common user lock.
            current = self.storage.get_scheduled_search(search.chat_id)
            if (
                current is None
                or current.created_at != search.created_at
                or current.start_at != search.start_at
            ):
                return
            super()._fire(search, now)
