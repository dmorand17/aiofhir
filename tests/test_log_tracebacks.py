"""The redacting filter covers tracebacks and stack info, not only the message."""

from __future__ import annotations

import io
import logging
import sys

from aiofhir.log import IsoFormatter, RedactingFilter


def _logger() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(IsoFormatter())
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("aiofhir.test.tracebacks")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def test_logger_exception_traceback_is_redacted() -> None:
    logger, stream = _logger()
    try:
        raise RuntimeError("upstream said Bearer tok-secret for Patient/pt-secret")
    except RuntimeError:
        logger.exception("request failed")
    text = stream.getvalue()
    assert "Traceback" in text and "RuntimeError" in text
    assert "tok-secret" not in text
    assert "pt-secret" not in text
    assert "Patient/[id]" in text


def test_exc_info_true_is_redacted() -> None:
    logger, stream = _logger()
    try:
        raise ValueError('{"refresh_token": "rt-secret"}')
    except ValueError:
        logger.warning("bad", exc_info=True)
    assert "rt-secret" not in stream.getvalue()


def test_stack_info_is_redacted() -> None:
    logger, stream = _logger()
    record = logger.makeRecord(
        logger.name, logging.INFO, __file__, 1, "msg", (), None, sinfo="at Patient/pt-secret"
    )
    logger.handle(record)
    assert "pt-secret" not in stream.getvalue()


def test_exception_object_is_kept_for_other_handlers() -> None:
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "m", None, None)
    try:
        raise KeyError("Patient/pt-secret")
    except KeyError:
        record.exc_info = sys.exc_info()
    RedactingFilter().filter(record)
    assert record.exc_info is not None
    assert record.exc_text is not None and "pt-secret" not in record.exc_text
