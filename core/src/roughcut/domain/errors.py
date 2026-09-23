"""Shared domain errors."""


class ProjectError(ValueError):
    """Raised when project data or revision state is invalid."""


class WorkflowError(ValueError):
    """Raised when finite-workflow data or storage violates its frozen contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
