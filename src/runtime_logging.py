"""Library diagnostics never serialize request URLs or exception messages."""

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone


def configure_diagnostics():
    handler = logging.StreamHandler()
    handler.setFormatter(DiagnosticFormatter())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)

    def exception_hook(kind, value, traceback):
        logging.error("Unhandled runtime exception", exc_info=(kind, value, traceback))

    sys.excepthook = exception_hook
    threading.excepthook = lambda args: exception_hook(
        args.exc_type, args.exc_value, args.exc_traceback
    )


class DiagnosticFormatter(logging.Formatter):
    def format(self, record):
        result = {
            "timestamp": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(),
            "service": os.getenv("APP_SERVICE", "local"),
            "level": record.levelname,
            "action": "runtime_diagnostic",
        }
        if record.exc_info and record.exc_info[0]:
            result["error_type"] = record.exc_info[0].__name__
        # record.getMessage(), args and traceback may contain SQL/HTTP secrets.
        return json.dumps(result)
