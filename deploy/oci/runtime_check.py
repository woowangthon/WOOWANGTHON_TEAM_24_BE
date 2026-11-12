"""Read-only Linux/systemd activity checks; never prints command lines or secrets."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


def inspect_runtime(app_dir: Path, service: str = "boinun.service", port: int = 8000) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", service):
        raise ValueError("Invalid service name.")

    def property_value(name):
        return subprocess.check_output(
            ["systemctl", "show", service, "-p", name, "--value"], text=True
        ).strip()

    main_pid = int(property_value("MainPID"))
    group = property_value("ControlGroup")
    if not group.startswith("/") or ".." in Path(group).parts:
        raise ValueError("The existing service cgroup could not be inspected.")
    pid_file = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.procs"
    pids = pid_file.read_text().split()
    inodes = set()
    connections = 0
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in Path(table).read_text().splitlines()[1:]:
            fields = line.split()
            if int(fields[1].split(":")[1], 16) == port:
                if fields[3] == "01":
                    connections += 1
                elif fields[3] == "0A":
                    inodes.add(fields[9])

    listeners = set()
    pdf_jobs = 0
    writable_files = 0
    spawned_workers = 0
    app_main = False
    data_dir = (app_dir / "data").resolve()
    for pid in pids:
        try:
            process = Path("/proc") / pid
            arguments = (process / "cmdline").read_bytes().decode(errors="replace").split("\0")
            pdf_jobs += "app.pdf_processing" in arguments
            spawned_workers += any("spawn_main" in argument for argument in arguments)
            if int(pid) == main_pid:
                app_main = "uvicorn" in arguments and "app.main:app" in arguments
            for descriptor in (process / "fd").iterdir():
                try:
                    target = os.readlink(descriptor)
                    if target.startswith("socket:[") and target[8:-1] in inodes:
                        listeners.add(int(pid))
                    if target.startswith("/") and Path(target).is_relative_to(data_dir):
                        info = (process / "fdinfo" / descriptor.name).read_text()
                        flags = next(line.split()[1] for line in info.splitlines() if line.startswith("flags:"))
                        writable_files += bool(int(flags, 8) & os.O_ACCMODE)
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            continue
    worker_count = 1 if app_main and listeners == {main_pid} and not spawned_workers else None
    return {
        "service": service,
        "service_active": property_value("ActiveState") == "active",
        "main_pid": main_pid,
        "worker_count": worker_count,
        "listener_pids": sorted(listeners),
        "pdf_jobs": pdf_jobs,
        "established_connections": connections,
        "writable_data_files": writable_files,
    }


def require_idle(state: dict) -> None:
    if not state["service_active"] or state["worker_count"] != 1:
        raise ValueError("The existing single-worker service state must be confirmed first.")
    if any(state[name] for name in ("pdf_jobs", "established_connections", "writable_data_files")):
        raise ValueError("Active requests or data writers detected; defer the full backup.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, default=Path("/home/opc/BE"))
    parser.add_argument("--service", default="boinun.service")
    arguments = parser.parse_args()
    print(json.dumps(inspect_runtime(arguments.app_dir, arguments.service), indent=2))
