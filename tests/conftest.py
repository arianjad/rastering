"""Test-suite gate for RaX's protocol file ``bbx_zmq.zmq_v2`` (beambox-daq, installed editable).

The GUI imports it at start; without it every test that builds a controller fails at import.
``ZMQ_V2_REQUIRED=1`` (pre-merge gates) makes that one loud collection error instead of many.
"""
from __future__ import annotations

import os

if os.environ.get("ZMQ_V2_REQUIRED"):
    import bbx_zmq.zmq_v2  # noqa: F401  -- fail collection loudly if the lib is missing
