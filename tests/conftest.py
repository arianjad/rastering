"""Test-suite gate for the shared ``zmq_v2`` library.

The v2 protocol tests ``pytest.importorskip("zmq_v2")`` so they stay green on
a checkout where the beambox-daq package ``bbx_zmq`` is not installed
— which also means a run that tested nothing exits 0. Set
``ZMQ_V2_REQUIRED=1`` (CI, pre-merge gates) to turn that skip into a loud
collection error instead.
"""
from __future__ import annotations

import os

if os.environ.get("ZMQ_V2_REQUIRED"):
    import bbx_zmq.zmq_v2  # noqa: F401  -- fail collection loudly if the lib is missing
