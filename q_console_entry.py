"""PyInstaller entry point for q_console.exe."""

import sys

from core.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
