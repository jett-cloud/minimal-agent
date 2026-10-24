"""In-process commit notifications for SSE readers."""

import asyncio
from collections import defaultdict


class EventPublisher:
    """Wake all readers only after a durable event batch has committed."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._revisions: defaultdict[str, int] = defaultdict(int)

    def revision(self, run_id: str) -> int:
        return self._revisions[run_id]

    async def notify_committed(self, run_id: str) -> int:
        async with self._condition:
            self._revisions[run_id] += 1
            revision = self._revisions[run_id]
            self._condition.notify_all()
            return revision

    async def wait_for_change(
        self,
        run_id: str,
        *,
        after_revision: int,
        timeout_seconds: float,
    ) -> int:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        async with self._condition:
            if self._revisions[run_id] > after_revision:
                return self._revisions[run_id]
            try:
                async with asyncio.timeout(timeout_seconds):
                    await self._condition.wait_for(lambda: self._revisions[run_id] > after_revision)
            except TimeoutError:
                pass
            return self._revisions[run_id]
