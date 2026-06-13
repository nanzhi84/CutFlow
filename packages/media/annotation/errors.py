"""V4 annotation failure taxonomy - for "retry only, never degrade" routing.

Only defines exception types; it does NOT implement retry logic (retry caps /
backoff / counting belong to the runner, a later step). Four classes:

- SchemaError       : format/schema failure (JSON not extractable / json.loads
                      failure / ValidationError / missing key fields).
                      -> retry by changing the prompt, not the frames.
- SemanticError     : valid format, unreasonable content (insufficient time
                      coverage / illegal role+lip_sync combo / low confidence /
                      self-contradiction / time out of bounds).
                      -> retry by resampling frames + adjusting signal phrasing.
- RuntimeVLMError   : runtime error (rate limit / timeout / 5xx / network).
                      -> retry with pure exponential backoff, change nothing.
- UnrecoverableError: not recoverable (corrupt file / unsupported format /
                      missing API key / local resource exhaustion). -> no retry.

These carry "which window / why" context but never a degraded annotation - V4 has
no needs_review degraded terminal state.
"""

from __future__ import annotations


class AnnotationV4Error(Exception):
    """Base class for V4 annotation failures (catch-all for the runner)."""


class SchemaError(AnnotationV4Error):
    """(1) Format/schema failure - retry by changing the prompt, not the frames."""


class SemanticError(AnnotationV4Error):
    """(2) Semantic quality failure - retry by resampling frames + adjusting phrasing."""


class RuntimeVLMError(AnnotationV4Error):
    """(3) Runtime error - retry with pure exponential backoff, change nothing."""


class UnrecoverableError(AnnotationV4Error):
    """(4) Unrecoverable - no retry, fail directly."""
