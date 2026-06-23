"""Entry point for ``python -m webglass``."""

from __future__ import annotations

import sys

from webglass.cli import main

if __name__ == "__main__":
    sys.exit(main())
