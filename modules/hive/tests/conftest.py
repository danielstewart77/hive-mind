"""Put src/ on the import path for every test module, not just the ones that
happen to insert it themselves. Without this, a single test file can only be
collected when another module has already run its own sys.path insert."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
