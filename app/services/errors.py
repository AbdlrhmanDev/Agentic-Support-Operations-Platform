"""Domain errors. Translated to HTTP responses or tool errors at the boundary."""


class DomainError(Exception):
    """A business rule said no. Never retried."""

    code = "domain_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    code = "not_found"


class CustomerNotFound(NotFoundError):
    code = "customer_not_found"


class OrderNotFound(NotFoundError):
    code = "order_not_found"


class TicketNotFound(NotFoundError):
    code = "ticket_not_found"


class RunNotFound(NotFoundError):
    code = "run_not_found"


class ApprovalNotFound(NotFoundError):
    code = "approval_not_found"


class ActionDenied(DomainError):
    """The requested write breaks a business rule and no approval can change that."""

    code = "action_denied"


class ApprovalRequired(DomainError):
    """The write is allowed only with an approved approval record."""

    code = "approval_required"


class ApprovalStateError(DomainError):
    code = "approval_state_conflict"


class InvalidInput(DomainError):
    code = "invalid_input"


class TransientError(Exception):
    """A dependency failed in a way that may succeed on retry."""
