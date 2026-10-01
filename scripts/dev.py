"""Start the library API, control API, and Angular dashboard for local development."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
K8S_ROOT = ROOT / "deploy" / "k8s"


class Service(NamedTuple):
    name: str
    command: list[str]
    cwd: Path
    health_url: str | None
    port: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-control", action="store_true", help="Do not start the desktop control API")
    parser.add_argument("--no-web", action="store_true", help="Do not start the Angular dashboard")
    parser.add_argument("--timeout", type=float, default=45.0, help="Readiness timeout in seconds")
    kind = parser.add_mutually_exclusive_group()
    kind.add_argument(
        "--with-kind",
        action="store_true",
        help="Build/deploy the kind stack, then start local APIs and Angular",
    )
    kind.add_argument(
        "--deploy-kind",
        action="store_true",
        help="Build and deploy the kind stack, then exit",
    )
    return parser.parse_args()


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def wait_until_ready(service: Service, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{service.name} exited with code {process.returncode}")
        if service.health_url is None:
            if not port_is_free(service.port):
                return
            time.sleep(0.25)
            continue
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


def _run_setup_command(command: list[str], *, input_text: str | None = None,
                       capture_output: bool = False) -> str:
    print(f"[kind] {' '.join(command)}")
    result = subprocess.run(
        command, cwd=ROOT, input=input_text, text=True,
        capture_output=capture_output, check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: "
            f"{' '.join(command)}{suffix}"
        )
    return result.stdout if capture_output else ""


def _postgres_schema() -> str:
    """Read the canonical PostgreSQL schema from the migration source."""
    migration = ROOT / "scripts" / "migrate_sqlite_to_postgres.py"
    match = re.search(
        r'POSTGRES_DDL\s*=\s*"""(.*?)"""',
        migration.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    if match is None:
        raise RuntimeError(f"Could not find POSTGRES_DDL in {migration}")
    return match.group(1).strip() + "\n"


def _configured_pg_url() -> str | None:
    """Read KARAOKE_PG_URL using the same environment-first .env precedence."""
    configured = os.environ.get("KARAOKE_PG_URL")
    if configured:
        return configured
    dotenv = ROOT / ".env"
    if not dotenv.is_file():
        return None
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            if key.strip() == "KARAOKE_PG_URL":
                return value.strip().strip('"').strip("'")
    return None


def deploy_kind() -> None:
    """Build and deploy the API, Postgres schema, and RabbitMQ to kind."""
    context = os.environ.get("KUBE_CONTEXT", "kind-karaoke")
    cluster = os.environ.get("KIND_CLUSTER", "karaoke")
    namespace = os.environ.get("K8S_NAMESPACE", "karaoke")
    image = os.environ.get("IMAGE", "karaoke-api:dev")
    if not context.startswith("kind-"):
        raise RuntimeError(
            f"Refusing Kubernetes context {context!r}; only kind-* contexts are allowed"
        )

    docker = shutil.which("docker")
    kind = shutil.which("kind")
    kubectl = shutil.which("kubectl")
    missing = [name for name, path in (
        ("docker", docker), ("kind", kind), ("kubectl", kubectl)
    ) if path is None]
    if missing:
        raise RuntimeError(f"Required kind deployment tools are missing: {', '.join(missing)}")

    _run_setup_command([docker, "info", "--format", "{{.ServerVersion}}"])
    clusters = _run_setup_command([kind, "get", "clusters"], capture_output=True)
    if cluster not in clusters.splitlines():
        raise RuntimeError(
            f"kind cluster {cluster!r} was not found; available clusters: "
            f"{', '.join(clusters.splitlines()) or '(none)'}"
        )

    _run_setup_command([
        docker, "build", "--network=host", "-t", image,
        "-f", str(K8S_ROOT.parent / "Dockerfile"), str(ROOT),
    ])
    _run_setup_command([kind, "load", "docker-image", image, "--name", cluster])

    # The schema ConfigMap is consumed on first initialization of the Postgres
    # PVC. It is generated from the migration source to avoid a second DDL copy.
    _run_setup_command([
        kubectl, "--context", context, "apply", "-f",
        str(K8S_ROOT / "namespace.yaml"),
    ])
    with tempfile.TemporaryDirectory(prefix="karaoke-schema-") as temporary:
        schema_file = Path(temporary) / "schema.sql"
        schema_file.write_text(_postgres_schema(), encoding="utf-8")
        configmap = _run_setup_command([
            kubectl, "--context", context, "-n", namespace, "create",
            "configmap", "karaoke-schema", f"--from-file=schema.sql={schema_file}",
            "--dry-run=client", "-o", "yaml",
        ], capture_output=True)
        _run_setup_command(
            [kubectl, "--context", context, "apply", "-f", "-"],
            input_text=configmap,
        )

    _run_setup_command([
        kubectl, "--context", context, "apply", "-k", str(K8S_ROOT),
    ])
    _run_setup_command([
        kubectl, "--context", context, "-n", namespace, "rollout",
        "status", "deployment/postgres", "--timeout=300s",
    ])

    # Apply the idempotent canonical DDL even when the Postgres PVC already
    # existed before the ConfigMap was installed.
    _run_setup_command([
        kubectl, "--context", context, "-n", namespace, "exec", "-i",
        "deployment/postgres", "--", "psql", "-v", "ON_ERROR_STOP=1",
        "-U", "karaoke", "-d", "karaoke",
    ], input_text=_postgres_schema())
    _run_setup_command([
        kubectl, "--context", context, "-n", namespace, "rollout",
        "restart", "deployment/karaoke-api",
    ])
    for deployment in ("rabbitmq", "karaoke-api"):
        _run_setup_command([
            kubectl, "--context", context, "-n", namespace, "rollout",
            "status", f"deployment/{deployment}", "--timeout=300s",
        ])
    print(f"[ready] kind stack: context={context}, namespace={namespace}")


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
    if shutil.which("npm") is None and not args.no_web and not args.deploy_kind:
        print("npm is required to start the Angular dashboard.", file=sys.stderr)
        return 2
    if (not (WEB_ROOT / "node_modules").is_dir()
            and not args.no_web and not args.deploy_kind):
        print("Frontend dependencies are missing. Run: cd web; npm install", file=sys.stderr)
        return 2

    env = os.environ.copy()
    configured_pg_url = _configured_pg_url()
    if args.with_kind or args.deploy_kind:
        try:
            deploy_kind()
        except (OSError, RuntimeError) as error:
            print(f"[error] {error}", file=sys.stderr)
            return 1
        if args.deploy_kind:
            return 0
        if configured_pg_url:
            env["KARAOKE_PG_URL"] = configured_pg_url
        else:
            env["KARAOKE_PG_URL"] = (
                "postgresql://karaoke:karaoke@127.0.0.1:5432/karaoke"
            )
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
    if args.with_kind and not configured_pg_url:
        context = os.environ.get("KUBE_CONTEXT", "kind-karaoke")
        namespace = os.environ.get("K8S_NAMESPACE", "karaoke")
        services.insert(0, Service(
            "Postgres port-forward",
            [
                shutil.which("kubectl") or "kubectl", "--context", context,
                "-n", namespace, "port-forward", "svc/postgres", "5432:5432",
            ],
            ROOT, None, 5432,
        ))
    if not args.no_control:
        services.append(Service("control API", [sys.executable, "-m", "karaoke.ctrl_api"], ROOT, f"http://127.0.0.1:{control_port}/api/health", control_port))
    if not args.no_web:
        npm = "npm.cmd" if os.name == "nt" else "npm"
        services.append(Service("Angular dashboard", [npm, "start", "--", "--host", "127.0.0.1"], WEB_ROOT, "http://127.0.0.1:4200", 4200))

    services_to_start: list[Service] = []
    for service in services:
        if port_is_free(service.port):
            services_to_start.append(service)
        elif service.health_url and endpoint_is_healthy(service.health_url):
            print(f"[reuse] {service.name}: {service.health_url}")
        else:
            print(
                f"Port {service.port} is occupied but {service.name} is not "
                f"healthy at {service.health_url or 'the PostgreSQL port-forward'}.",
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
    except (OSError, RuntimeError) as error:
        print(f"[error] {error}", file=sys.stderr)
        return 1
    finally:
        for _, process in reversed(processes):
            stop_process(process)


if __name__ == "__main__":
    raise SystemExit(main())
