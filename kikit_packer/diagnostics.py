from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: Severity
    path: str
    message: str
    value_type: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["severity"] = self.severity.value
        return value


class PackerError(RuntimeError):
    def __init__(self, diagnostic: Diagnostic, exit_code: int = 3):
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic
        self.exit_code = exit_code


def error(code: str, path: str, message: str, value: Any = None) -> Diagnostic:
    return Diagnostic(
        code=code,
        severity=Severity.ERROR,
        path=path,
        message=message,
        value_type=None if value is None else type(value).__name__,
    )


def warning(code: str, path: str, message: str, **context: Any) -> Diagnostic:
    return Diagnostic(code, Severity.WARNING, path, message, context=context)
