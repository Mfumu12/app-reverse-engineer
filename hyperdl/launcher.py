"""Standalone PyInstaller entry point for HyperDL.exe."""

import multiprocessing

from hyperdl.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
