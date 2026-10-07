"""Entry point: python run.py

Auto-reload is off by default: it restarts the server whenever a ZweiGeeks
source file changes, which silently kills any run in progress (jobs live in
memory). Set ZWEIGEEKS_RELOAD=1 when working on ZweiGeeks itself.
"""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "src.server:app", host="127.0.0.1", port=8008,
        reload=os.environ.get("ZWEIGEEKS_RELOAD") == "1",
    )
