
from __future__ import annotations
import logging
from .config import settings


# A verbosity tier BELOW DEBUG. DEBUG shows tool calls + args + results (the
# useful middle); TRACE additionally dumps full prompts, raw LLM responses, the
# whole AgentState and per-chunk text (the firehose). Registered + exposed as
# logger.trace(...) so it is usable like any standard level, and LOG_LEVEL=TRACE
# parses via getLevelName.
TRACE = 5
logging.addLevelName(TRACE, "TRACE")


def _trace(self, message, *args, **kwargs):
    if self.isEnabledFor(TRACE):
        self._log(TRACE, message, args, **kwargs)


logging.Logger.trace = _trace


_FORMAT = settings.log_format

# Chatty third-party loggers (incl. the MCP streamable-http SSE traffic, e.g.
# "mcp.client.streamable_http: SSE message: root=JSONRPCResponse(...)"). These
# belong to the TRACE tier ONLY: shown when LOG_LEVEL=TRACE, hidden at DEBUG and
# above so the DEBUG tier stays readable. "mcp" covers mcp.client.streamable_http
# and the other mcp.* children.
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "uvicorn.access", "mcp", "openai")

def configure_logging() -> None:

    level = logging.getLevelName(settings.log_level.upper())
    if not isinstance(level, int):  # unknown/typo'd name -> safe default
        level = logging.INFO

    logging.basicConfig(level=level, format=_FORMAT, force=True)

    # Only let the noisy loggers through in TRACE mode; otherwise hold them at
    # WARNING so DEBUG shows our tool I/O, not the MCP/HTTP wire chatter.
    noisy_level = logging.DEBUG if level <= TRACE else logging.WARNING
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(noisy_level)
    logging.getLogger(__name__).debug("logging configured at level %s", settings.log_level)

    