"""Errors of Phase 1. The service layer (M6) maps them to HTTP: 404, 409, 422."""


class BriefError(Exception):
    """Base of every error a caller of the brief service is expected to handle."""


class NotFound(BriefError):
    """No such session or run."""


class WrongState(BriefError):
    """The session is not at a point where this action makes sense (HTTP 409)."""


class StaleBrief(BriefError):
    """The hash does not belong to the current brief text (HTTP 409)."""


class InvalidInput(BriefError):
    """The input is empty, malformed or violates a limit (HTTP 422)."""


class UploadRejected(InvalidInput):
    """An upload violates a limit or cannot be read; nothing of the batch was stored."""
