"""Skip systemd startup until valid configuration is supplied; never call an API."""

import sys

from claims_assistant.runtime.settings import ConfigurationError, Settings

try:
    Settings.load()
except ConfigurationError:
    print("configuration_pending: fill /etc/claims-assistant/env", file=sys.stderr)
    # ExecCondition status 1 skips the unit without starting a restart loop.
    raise SystemExit(1) from None
