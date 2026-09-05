"""`python -m shipboard` entry point (mirrors the old script: exit code kept)."""

from .cli import main

raise SystemExit(main())
