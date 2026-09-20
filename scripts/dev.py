"""Start the library API, control API, and Angular dashboard for local development."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import NamedTuple
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"


class Service(NamedTuple):
    name: str
    command: list[str]
    cwd: Path
    health_url: str
    port: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-control", action="store_true", help="Do not start the desktop control API")
    parser.add_argument("--no-web", action="store_true", help="Do not start the Angular dashboard")
    parser.add_argument("--timeout", type=float, default=45.0, help="Readiness timeout in seconds")
    return parser.parse_args()


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def wait_until_ready(service: Service, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{service.name} exited with code {process.returncode}")
        try:
            with urlopen(service.health_url, timeout=1) as response:
                if response.status < 500:
                    return
        except (URLError, OSError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"{service.name} did not become ready at {service.health_url}")


def endpoint_is_healthy(url: str) -> bool:
    try:
        with urlopen(url, timeout=1) as response:
            return response.status < 500
    except (URLError, OSError):
        return False


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        os.killpg(process.pid, signal.SIGTERM)


def main() -> int:
    args = parse_args()
    if sys.version_info < (3, 11):
        print("Python 3.11 or newer is required.", file=sys.stderr)
        return 2
    if shutil.which("npm") is None and not args.no_web:
        print("npm is required to start the Angular dashboard.", file=sys.stderr)
        return 2
    if not (WEB_ROOT / "node_modules").is_dir() and not args.no_web:
        print("Frontend dependencies are missing. Run: cd web; npm install", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    if os.name == "nt" and env.get("LOCALAPPDATA"):
        winget_links = Path(env["LOCALAPPDATA"]) / "Microsoft" / "WinGet" / "Links"
        if winget_links.is_dir():
            env["PATH"] = str(winget_links) + os.pathsep + env.get("PATH", "")
    api_port = int(env.get("KARAOKE_API_PORT", "8000"))
    control_port = int(env.get("KARAOKE_CTRL_PORT", "8765"))
    services = [
        Service("library API", [sys.executable, "-m", "karaoke.api"], ROOT, f"http://127.0.0.1:{api_port}/api/health", api_port),
    ]
    if not args.no_control:
        services.append(Service("control API", [sys.executable, "-m", "karaoke.ctrl_api"], ROOT, f"http://127.0.0.1:{control_port}/api/health", control_port))
    if not args.no_web:
        npm = "npm.cmd" if os.name == "nt" else "npm"
        services.append(Service("Angular dashboard", [npm, "start", "--", "--host", "127.0.0.1"], WEB_ROOT, "http://127.0.0.1:4200", 4200))

    services_to_start: list[Service] = []
    for service in services:
        if port_is_free(service.port):
            services_to_start.append(service)
        elif endpoint_is_healthy(service.health_url):
            print(f"[reuse] {service.name}: {service.health_url}")
        else:
            print(
                f"Port {service.port} is occupied but {service.name} is not healthy at "
                f"{service.health_url}.",
                file=sys.stderr,
            )
            return 2

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    processes: list[tuple[Service, subprocess.Popen[str]]] = []
    try:
        for service in services_to_start:
            print(f"[start] {service.name}: {' '.join(service.command)}")
            process = subprocess.Popen(
                service.command,
                cwd=service.cwd,
                env=env,
                text=True,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            processes.append((service, process))

        for service, process in processes:
            wait_until_ready(service, process, args.timeout)
            print(f"[ready] {service.name}: {service.health_url}")

        print("\nKaraoke dashboard: http://localhost:4200")
        print("Press Ctrl+C to stop all services.")
        while all(process.poll() is None for _, process in processes):
            time.sleep(0.5)
        failed = next((service for service, process in processes if process.poll() is not None), None)
        raise RuntimeError(f"{failed.name if failed else 'A service'} stopped unexpectedly")
    except KeyboardInterrupt:
        print("\n[stop] shutting down services")
        return 0
    except RuntimeError as error:
        print(f"[error] {error}", file=sys.stderr)
        return 1
    finally:
        for _, process in reversed(processes):
            stop_process(process)


if __name__ == "__main__":
    raise SystemExit(main())
