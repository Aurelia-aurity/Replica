"""Safe domain errors. Never copy upstream messages, details, tokens or SQL."""


class DatabaseError(Exception):
    message = "Database request failed."

    def __init__(self, *, code: str | None = None, outcome_unknown: bool = False):
        self.code = code
        self.outcome_unknown = outcome_unknown
        super().__init__(self.message)


class ValidationError(DatabaseError):
    message = "Invalid database function argument."


class AuthenticationError(DatabaseError):
    message = "A valid user access token is required."


class NotFoundError(DatabaseError):
    message = "Resource not found or unavailable."


class ConflictError(DatabaseError):
    message = "Resource state or request conflicts with an existing operation."


class PermissionDeniedError(DatabaseError):
    message = "Database access was denied."


class DatabaseUnavailableError(DatabaseError):
    message = "Database service is temporarily unavailable."
