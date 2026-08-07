"""Deterministic and hostile fixture pages for WebGlass's M0-M2 test harness.

Everything under this package is served over plain HTTP from 127.0.0.1 on an
OS-assigned ephemeral port by :mod:`tests.fixtures.server` -- no external
network is used or required. See ``server.py`` for the route table and
``pages/`` for the served HTML/JS.
"""

from __future__ import annotations
