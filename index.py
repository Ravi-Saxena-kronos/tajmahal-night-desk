"""Vercel WSGI entry. Local still uses python deployment/browser/server.py.

The API key stays on the server. The page only receives 60-second tokens.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deployment" / "browser"))

from server import app  # noqa: E402, F401
