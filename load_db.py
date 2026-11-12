"""Kept so the old entry point still works.  Same as: python -m get_data [options]

All logic now lives in the get_data/ package.  Run with --help for the options.
"""

from get_data.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
