"""Shared Checko adapter errors, so every section catches the same types."""


class InvalidResponse(ValueError):
    """Malformed or excessive response, without its contents."""


class ApiRejected(Exception):
    """The envelope reported meta.status == "error"; a safe api_error result follows."""
