"""Apply safe diagnostics in deployed services, including maintenance commands."""

import os

if os.getenv("APP_SERVICE"):
    from src.runtime_logging import configure_diagnostics

    configure_diagnostics()
