#!/usr/bin/env python3
"""Entry point: python3 -I ~/steamdeck-mcp/server.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from steamdeck_mcp.server import main  # noqa: E402

main()
