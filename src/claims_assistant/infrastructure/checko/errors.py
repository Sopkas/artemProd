"""Shared Checko adapter errors, so every section catches the same types."""


class InvalidResponse(ValueError):
    """Malformed or excessive response, without its contents."""


class ApiRejected(Exception):
    """The envelope reported meta.status == "error"; a safe api_error result follows."""


class NotFound(Exception):
    """The source has no such company: an «ok» envelope with empty data and a message.

    Confirmed on the live API (20.09.2026): an unknown INN answers HTTP 200,
    ``meta.status == "ok"`` and ``"message": "Не найдено ни одной организации…"``. It is a
    legitimate answer, not a broken one, so it must not read as «invalid_response».
    """


def is_not_found(meta: object) -> bool:
    """Whether the envelope says the company itself is unknown to the source."""
    if not isinstance(meta, dict):
        return False
    message = meta.get("message")
    return isinstance(message, str) and "не найдено" in message.lower()
