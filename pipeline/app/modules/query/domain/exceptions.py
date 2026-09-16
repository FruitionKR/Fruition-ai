class QueryError(Exception):
    """Query domain base error."""


class InvalidQuestionError(QueryError):
    """Raised when a query question is invalid."""

