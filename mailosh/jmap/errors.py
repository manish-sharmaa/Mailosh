"""Typed exceptions for the JMAP client."""

from __future__ import annotations


class JmapError(Exception):
    """Base class for all errors raised by :mod:`mailosh.jmap`.

    Catching this alone covers every failure mode of :class:`JmapClient`: a
    JMAP-level method error (:class:`MethodError`), an HTTP/transport-level
    failure (:class:`TransportError`), or a malformed response — instead of
    making callers catch two unrelated exception hierarchies.
    """


class MethodError(JmapError):
    """A batched JMAP method call came back as an ``error`` response object.

    RFC 8620 §3.5.1: a method-level failure is reported in-line, as a normal
    entry in ``methodResponses`` whose name is the literal string ``"error"``
    rather than an HTTP-level failure. This exception surfaces that as a
    typed Python error instead of making every caller inspect the tuple.

    Attributes:
        type: the JMAP error type, e.g. ``"unknownMethod"`` or
            ``"invalidArguments"``.
        call_id: the client-supplied call id the error responded to, so the
            caller can tell which of several batched calls failed.
    """

    def __init__(self, type: str, call_id: str) -> None:
        self.type = type
        self.call_id = call_id
        super().__init__(f"JMAP call {call_id!r} failed: {type}")


class TransportError(JmapError):
    """The HTTP transport failed: a non-2xx response, or the request never
    completed at all (connection refused, timed out, DNS failure, ...).

    Wraps ``httpx.HTTPStatusError`` (bad status) and httpx's own
    ``TransportError`` hierarchy (connection-level failures) so callers only
    need to catch :class:`JmapError` to cover both JMAP-level and
    transport-level failures.

    Attributes:
        status_code: the HTTP status code for a bad response, or ``None``
            when the request never got a response at all (e.g. a connection
            failure or timeout).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class BlobTooLarge(JmapError):
    """A blob download exceeded the caller's byte cap.

    Raised by :meth:`JmapClient.fetch_blob` the instant the running total of
    bytes read so far exceeds ``max_bytes`` -- never after reading the rest
    of the body and checking its final size, which would defeat the point
    of a cap for a hostile or merely huge attachment.

    Attributes:
        blob_id: the blob that was being fetched.
        max_bytes: the cap that was exceeded.
    """

    def __init__(self, blob_id: str, max_bytes: int) -> None:
        self.blob_id = blob_id
        self.max_bytes = max_bytes
        super().__init__(f"blob {blob_id!r} exceeds the {max_bytes}-byte cap")
