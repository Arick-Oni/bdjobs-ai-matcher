import socket
import sys
import os
import uvicorn
from dotenv import load_dotenv

load_dotenv()


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="ignore")

    app_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, app_dir)

    local_ip = get_local_ip()
    port = 8000

    print("=" * 65)
    print("BDJobs AI Job Matcher Mobile Web App")
    print("=" * 65)
    print(f"Mobile Access (Same Wi-Fi):  http://{local_ip}:{port}")
    print(f"Desktop / Local Access:      http://localhost:{port}")
    print("=" * 65)
    print("Press Ctrl+C to stop the server.\n")

    uvicorn.run("backend.main:app", host="0.0.0.0", port=port, reload=True)
