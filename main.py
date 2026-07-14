"""Launcher — kept at the repo root so PyInstaller has a simple entry point."""

import sys

from slimbms.app import main

if __name__ == "__main__":
    sys.exit(main())
