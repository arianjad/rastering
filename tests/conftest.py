"""Test-suite gate for the shared ``bbx_zmq`` package (the BLACS menu link, BBX-190).

The menu-server tests ``pytest.importorskip("bbx_zmq.menus.rastering")`` so they stay green on
a checkout where the beambox-daq package ``bbx_zmq`` (with its menus) is not on the path
— which also means a run that tested nothing exits 0. Set
``ZMQ_V2_REQUIRED=1`` (CI, pre-merge gates) to turn that skip into a loud
collection error instead.
"""
from __future__ import annotations

import os

if os.environ.get("ZMQ_V2_REQUIRED"):
    import bbx_zmq.menus.rastering  # noqa: F401  -- fail collection loudly if the lib is missing
