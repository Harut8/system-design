"""Make the lab's flat modules importable regardless of where pytest is invoked from."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
