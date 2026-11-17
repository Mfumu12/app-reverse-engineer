"""Entry point for `python -m hyperdl`."""

import multiprocessing

from hyperdl.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
