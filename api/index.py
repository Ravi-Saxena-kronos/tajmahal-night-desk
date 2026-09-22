"""File-based Vercel function. Rewrites in vercel.json send public routes here."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deployment" / "browser"))

from server import Handler as handler  # noqa: E402, F401
