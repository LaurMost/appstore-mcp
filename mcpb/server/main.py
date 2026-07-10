"""MCPB entry point: run appstore-mcp from the bundle's vendored packages.

Launched with -I (isolated mode), so neither PYTHONPATH nor the script
directory leak in; vendor/ is the only source of third-party packages.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

from appstore_mcp.server import main

main()
