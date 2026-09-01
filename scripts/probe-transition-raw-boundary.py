#!/usr/bin/env python3
"""Canonical entry point for the raw Program transition-boundary probe.

The implementation lives in ``probe-transition-boundary.py`` so existing
runbooks remain compatible; this name makes the raw-frame evidence contract
explicit for artifact and Probe invocations.
"""

from __future__ import annotations

import pathlib
import runpy


if __name__ == "__main__":
    runpy.run_path(str(pathlib.Path(__file__).with_name("probe-transition-boundary.py")), run_name="__main__")
