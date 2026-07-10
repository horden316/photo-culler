#!/usr/bin/env python3
"""Desktop entry point: serve the culler on a random local port inside a native window."""
from __future__ import annotations

import threading

import webview

import server


def main() -> int:
    server.connect().close()
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    webview.create_window("Photo Culler", f"http://127.0.0.1:{port}", width=1320, height=900)
    webview.start()
    httpd.shutdown()
    thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
