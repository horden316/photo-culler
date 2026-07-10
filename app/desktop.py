#!/usr/bin/env python3
"""Desktop entry point: serve the culler on a random local port inside a native window."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import webview

import server


def unblock_bundled_dlls() -> None:
    """Strip mark-of-the-web from bundled DLLs on Windows.

    Zips extracted with Explorer propagate the Zone.Identifier stream to every
    file, and .NET refuses to load a blocked assembly — pywebview then dies
    with "Failed to resolve Python.Runtime.Loader.Initialize".
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    for dll in Path(sys._MEIPASS).rglob("*.dll"):
        try:
            os.remove(f"{dll}:Zone.Identifier")
        except OSError:
            pass


def main() -> int:
    unblock_bundled_dlls()
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
