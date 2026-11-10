"""
Logistics Information Management System (Logistics IMS) - Application Entry Point.

Starts the Flask development / production WSGI server.
Usage:
    python -m app.main
    or
    python app/main.py
"""

import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"

    print("=" * 70)
    print("  Logistics Information Management System (Logistics IMS)")
    print(f"  Server running on http://{host}:{port}")
    print("  Relational Database: SQLite 3.53+ (WAL Mode enabled)")
    print("  NoSQL Engine: SQLite JSON1 Document Store")
    print("  Cache-Aside Layer: In-Memory Thread-Safe LRU")
    print("=" * 70)

    app.run(host=host, port=port, debug=debug)
