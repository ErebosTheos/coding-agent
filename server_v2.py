"""V2 entry point — python server_v2.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.codegen_agent_v2.server import start

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=7071)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()
    start(port=args.port, host=args.host)
