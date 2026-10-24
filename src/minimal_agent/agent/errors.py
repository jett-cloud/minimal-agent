"""Stable AgentLoop failures safe for persistence and SSE output."""


class AgentLoopError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MaxStepsExceeded(AgentLoopError):
    def __init__(self, max_steps: int) -> None:
        super().__init__(
            "MAX_STEPS_EXCEEDED",
            f"Agent exceeded the maximum of {max_steps} model steps",
        )
