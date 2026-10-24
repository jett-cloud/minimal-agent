"""Own background AgentLoop tasks and recover interrupted Runs."""

import asyncio

from minimal_agent.agent.loop import AgentLoop
from minimal_agent.schemas import RunRecord
from minimal_agent.storage import RunRepository
from minimal_agent.streaming import EventPublisher


class RunSupervisor:
    def __init__(
        self,
        loop: AgentLoop | None,
        *,
        runs: RunRepository,
        publisher: EventPublisher,
    ) -> None:
        self._loop = loop
        self._runs = runs
        self._publisher = publisher
        self._tasks: dict[str, asyncio.Task[RunRecord]] = {}
        self._accepting = True

    def set_loop(self, loop: AgentLoop) -> None:
        if self._tasks:
            raise RuntimeError("Cannot replace AgentLoop while Runs are active")
        self._loop = loop

    @property
    def active_run_ids(self) -> tuple[str, ...]:
        return tuple(self._tasks)

    def start(self, run_id: str) -> asyncio.Task[RunRecord]:
        if not self._accepting:
            raise RuntimeError("RunSupervisor is shutting down")
        if self._loop is None:
            raise RuntimeError("Agent runtime is not configured")
        if run_id in self._tasks:
            raise RuntimeError(f"Run is already supervised: {run_id}")
        task = asyncio.create_task(self._loop.execute(run_id), name=f"agent-run:{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda completed: self._discard(run_id, completed))
        return task

    async def recover_interrupted(self) -> int:
        """Fail Runs left running by a prior process and publish their durable terminals."""

        interrupted = await asyncio.to_thread(self._runs.list_running)
        recovered = 0
        for run in interrupted:
            await asyncio.to_thread(
                self._runs.fail_with_error,
                run.id,
                error_code="PROCESS_RESTARTED",
                error_message="Application restarted before the Run completed",
            )
            await self._publisher.notify_committed(run.id)
            recovered += 1
        return recovered

    async def shutdown(self, *, grace_seconds: float = 5.0) -> None:
        if grace_seconds < 0:
            raise ValueError("grace_seconds must not be negative")
        self._accepting = False
        tasks = tuple(self._tasks.values())
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _discard(self, run_id: str, task: asyncio.Task[RunRecord]) -> None:
        self._tasks.pop(run_id, None)
        if not task.cancelled():
            task.exception()
