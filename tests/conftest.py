"""Make the example section plugins importable as plain modules (`import study`, …) so
the tests exercise them exactly as `load_plugin_dir` would load them."""

import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "sections"
sys.path.insert(0, str(EXAMPLES))
