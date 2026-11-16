"""Structured domain exceptions mapped cleanly by REST, MCP, and Web UI."""

from __future__ import annotations


class DomainError(Exception):
    """Base application error carrying machine code and HTTP status."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class AuthError(DomainError):
    """Authentication failure (401)."""

    def __init__(self, message: str = "Authentication required", code: str = "unauthorized") -> None:
        super().__init__(code=code, message=message, status_code=401)


class PermissionDeniedError(DomainError):
    """Authorization or scope failure (403)."""

    def __init__(self, message: str = "Permission denied", code: str = "forbidden") -> None:
        super().__init__(code=code, message=message, status_code=403)


class NotFoundError(DomainError):
    """Missing resource (404)."""

    def __init__(self, message: str = "Resource not found", code: str = "not_found") -> None:
        super().__init__(code=code, message=message, status_code=404)


class ConflictError(DomainError):
    """State or idempotency conflict (409)."""

    def __init__(self, message: str, code: str = "conflict") -> None:
        super().__init__(code=code, message=message, status_code=409)


class ValidationError(DomainError):
    """Input validation error (400)."""

    def __init__(self, message: str, code: str = "validation_error") -> None:
        super().__init__(code=code, message=message, status_code=400)


class RateLimitError(DomainError):
    """Too many requests (429)."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        retry_after: int = 60,
        code: str = "rate_limited",
    ) -> None:
        super().__init__(code=code, message=message, status_code=429)
        self.retry_after = retry_after
