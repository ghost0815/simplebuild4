#!/usr/bin/env python3
"""SimpleBuild4 (s4) — Cross-compilation build system for oscam."""

import sys
import os

MIN_PYTHON = (3, 9)
if sys.version_info < MIN_PYTHON:
    sys.exit(f"s4 requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+, found {sys.version}")

# ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import main

if __name__ == "__main__":
    main()
