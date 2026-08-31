"""Allow ``python -m weekly_feedback``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
