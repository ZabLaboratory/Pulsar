#!/usr/bin/env python3
"""Compatibility entry point for the raw Program transition probe."""

from __future__ import annotations

import pathlib
import runpy


if __name__ == "__main__":
    runpy.run_path(str(pathlib.Path(__file__).with_name("probe-transition-raw-boundary.py")), run_name="__main__")
