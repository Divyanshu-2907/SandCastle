import enum


class RunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class StepType(str, enum.Enum):
    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
