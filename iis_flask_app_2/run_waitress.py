from __future__ import annotations

import os

from waitress import serve

from app import app

'''
def main() -> None:
    port = os.environ.get("HTTP_PLATFORM_PORT", "8000")
    host = os.environ.get("HTTP_PLATFORM_HOST", "127.0.0.1")
    serve(app, host=host, port=int(port))
'''
def main() -> None:
    port = int(os.environ.get("HTTP_PLATFORM_PORT", "8081"))
    # Always bind to localhost; IIS proxies requests
    serve(app, host="127.0.0.1", port=port)

if __name__ == "__main__":
    main()
