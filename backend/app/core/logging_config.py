"""Logging configuration for the FastAPI application.

The security controls in this package record what they refuse and why through the standard
library's loggers. Nothing configured those loggers. Uvicorn configures loggers for itself -
``uvicorn``, ``uvicorn.error``, ``uvicorn.access`` - and leaves the root logger at Python's
default: level ``WARNING`` with no handlers. Nothing under ``backend/`` is named in that
configuration, so an application record below ``WARNING`` was discarded by the level check
before any handler was consulted, and a record at ``WARNING`` or above was served only by
:data:`logging.lastResort`, the bare stderr handler Python falls back to when a record reaches
no handler at all - which writes the message with no level, no timestamp and no logger name.

The visible consequence is that the start-up confirmations disappeared while the warnings
survived, which is the wrong way round for the one fact an operator most needs: the record
:func:`backend.app.core.rate_limit.register_rate_limiting` emits names the window store the
throttling tiers count in, and only a SHARED store enforces the configured quota rather than
that quota multiplied by the number of workers and pods. Without the record there is no way to
tell those two deployments apart from the log. A throttling degradation was equally
indistinguishable from a provider outage, and severity-based alerting could not work at all.

:func:`configure_logging` is called once by ``backend/app/main.py`` before any middleware is
registered. It installs one tagged handler on the root logger whose format carries the
timestamp, the level and the logger name, and it raises the application tree's level so
informational records are emitted rather than dropped. It is idempotent, so importing the entry
point more than once in a process does not double every line, and it is deliberately
conservative about the level: it sets one only where none has been chosen.

The formatter also escapes control characters in the formatted message. A record may quote a
value that arrived in a request header, and a value containing a newline or a carriage return
would otherwise write what looks like a second, fabricated log entry. Escaping applies to the
message only, so a traceback appended after it keeps the real newlines that make it readable.
The same escaping is applied to the server's own loggers, which record the peer address and the
request line and do not propagate to the root logger.
"""

import logging
from typing import Iterable, Optional, Union

# Root of this application's logger tree. Every module here calls
# ``logging.getLogger(__name__)``, so each of those loggers is a descendant of this one and its
# level governs whether their records survive the level check at all.
APPLICATION_LOGGER_NAME: str = "backend"

# Level the application tree is set to when it has none of its own. INFO, because the records
# this exists to rescue - which controls were installed, and where they count - are emitted at
# INFO, and a level inherited from the root logger is Python's default rather than a decision
# anybody took.
DEFAULT_APPLICATION_LOG_LEVEL: int = logging.INFO

# Format every record carries. Level and logger name are what make records filterable; the
# timestamp is what makes them correlatable with a request in another system.
LOG_FORMAT: str = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# ISO-8601 date and time, to the second. Milliseconds are appended by the formatter's
# default ``%(asctime)s`` handling only when no date format is given, and a stable width is
# worth more here than the extra precision.
LOG_DATE_FORMAT: str = "%Y-%m-%dT%H:%M:%S%z"

# Level the root logger is set to when no caller names one. INFO rather than WARNING so the
# records that state which controls are installed - and which window store is counting - are
# emitted rather than discarded.
DEFAULT_LOG_LEVEL: str = "INFO"

# Marks the handler this module installs, so a second call updates that handler instead of
# adding another one beside it.
_MANAGED_HANDLER_ATTRIBUTE: str = "_excel_clone_managed"

# Loggers whose records never reach the root handler, because the server configures them with
# propagation disabled. They are given the escaping filter directly.
_SERVER_LOGGER_NAMES: Iterable[str] = ("uvicorn.access", "uvicorn.error", "uvicorn")

# Characters replaced before a message is written. C0 controls and DEL: a newline or carriage
# return in a logged value forges a log entry, and the remainder corrupt the line silently.
_ESCAPED_CHARACTERS = {
    code_point: "\\x{0:02x}".format(code_point)
    for code_point in list(range(0x00, 0x20)) + [0x7F]
}


def escape_control_characters(text: str) -> str:
    """Return ``text`` with every C0 control character and DEL rendered as an escape.

    Args:
        text: The formatted message, which may quote a value taken from a request.

    Returns:
        str: The same text with control characters replaced by a printable ``\\xNN``
            sequence, so one record can never occupy more than one line.
    """
    return text.translate(_ESCAPED_CHARACTERS)


class ControlCharacterEscapingFormatter(logging.Formatter):
    """A formatter whose message can only ever be a single line.

    Only the message line is escaped. :meth:`logging.Formatter.format` appends exception and
    stack text afterwards, so a traceback keeps the newlines it needs to stay legible - and
    that text is generated by the interpreter rather than supplied by a caller.
    """

    def formatMessage(self, record: logging.LogRecord) -> str:
        # SECURITY: a value quoted from a request cannot forge a second log entry - a
        # newline or carriage return in one was written through unescaped
        return escape_control_characters(super().formatMessage(record))


class ControlCharacterEscapingFilter(logging.Filter):
    """Escapes control characters in a record's message and arguments.

    For loggers that own their handlers and do not propagate - the server's access logger
    records the peer address and the request line, both of which a caller influences - a
    filter is the only place the record can be reached without replacing the handler's own
    formatter, which would discard how the server chooses to render its records.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = escape_control_characters(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                escape_control_characters(argument)
                if isinstance(argument, str)
                else argument
                for argument in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: escape_control_characters(value)
                if isinstance(value, str)
                else value
                for key, value in record.args.items()
            }
        return True


def has_effective_handler(logger: logging.Logger) -> bool:
    """Whether a record emitted on ``logger`` would reach a configured handler.

    Walks the propagation chain the way :meth:`logging.Logger.callHandlers` does, stopping at
    the first logger that carries a handler or that does not propagate. This is the measured
    pre-bootstrap state: with the tree as uvicorn leaves it, it reports False for the
    application logger, which is why records reached only ``logging.lastResort``.

    Args:
        logger: The logger to inspect.

    Returns:
        bool: True when some logger in the chain already carries a handler.
    """
    current = logger
    while current is not None:
        if current.handlers:
            return True
        if not current.propagate:
            return False
        current = current.parent
    return False


def _managed_handler(root: logging.Logger) -> Optional[logging.Handler]:
    """Return the handler a previous call installed on ``root``, if there is one."""
    for handler in root.handlers:
        if getattr(handler, _MANAGED_HANDLER_ATTRIBUTE, False):
            return handler
    return None


def configure_logging(level: Union[int, str] = DEFAULT_LOG_LEVEL) -> logging.Logger:
    """Install the application's logging configuration, without overriding an operator.

    Called once from the application entry point, before anything that logs. Safe to call
    again: the handler it installs is tagged, so a second call re-uses it rather than
    duplicating every record.

    Two independent things decide whether a record is seen, because a dropped record has two
    possible causes and fixing one does not fix the other:

    * a HANDLER carrying the format and the escaping is installed on the ROOT logger, so it
      serves the application tree and the standard library alike through one formatter. It is
      placed on the root rather than on the application logger precisely so a record is
      formatted once and emitted once - a handler on both would double every application line.
    * the LEVEL of the application tree is raised to
      :data:`DEFAULT_APPLICATION_LOG_LEVEL` only when that logger has no level of its own. An
      inherited ``WARNING`` is Python's default rather than a choice, so raising it loses
      nothing an operator asked for, while an explicitly set level - from ``dictConfig``, or
      from a caller - is left exactly as it is.

    Existing loggers are left enabled and their own handlers untouched, so the server's records
    keep the form it gives them. The escaping filter is added to the loggers that do not
    propagate to the root handler, and adding it twice is avoided by type.

    Args:
        level: The level the root logger is set to, as a name or a numeric level. Defaults to
            :data:`DEFAULT_LOG_LEVEL`.

    Returns:
        logging.Logger: The application logger, so a caller can assert on what was configured.
    """
    root = logging.getLogger()
    application = logging.getLogger(APPLICATION_LOGGER_NAME)

    if application.level == logging.NOTSET:
        application.setLevel(DEFAULT_APPLICATION_LOG_LEVEL)

    handler = _managed_handler(root)
    if handler is None:
        handler = logging.StreamHandler()
        setattr(handler, _MANAGED_HANDLER_ATTRIBUTE, True)
        root.addHandler(handler)
    handler.setFormatter(
        ControlCharacterEscapingFormatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    )

    root.setLevel(level)
    for name in _SERVER_LOGGER_NAMES:
        server_logger = logging.getLogger(name)
        if not any(
            isinstance(existing, ControlCharacterEscapingFilter)
            for existing in server_logger.filters
        ):
            server_logger.addFilter(ControlCharacterEscapingFilter())

    return application
