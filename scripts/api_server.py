#!/usr/bin/env python3
"""
pre API server 入口
用法: uv run python scripts/api_server.py [--port 19400]
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api import run_server

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="pre API server")
    parser.add_argument("--port", type=int, default=19400, help="Port (default: 19400)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host (default: 127.0.0.1)")
    args = parser.parse_args()
    run_server(host=args.host, port=args.port)
