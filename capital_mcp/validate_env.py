"""Credential pre-flight check: `python -m capital_mcp.validate_env`.

Used by the install scripts to tell a configured setup from one that still holds the
`.env.example` placeholders. Validation is entirely local — no API call is made.
"""

import sys

from .config import get_config
from .errors import CapitalMCPError


def main() -> int:
    """Return 0 when credentials are usable, or 1 with an explanation on STDERR."""
    try:
        config = get_config()
    except CapitalMCPError as exc:
        print(f"ERROR [{exc.code}] {exc.message}", file=sys.stderr)
        return 1

    print(f"OK: credentials configured for the {config.cap_env.value} environment")
    return 0


if __name__ == "__main__":
    sys.exit(main())
