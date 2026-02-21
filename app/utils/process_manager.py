"""Zombie process detection and cleanup for port 8855 (Windows).

Enhanced version of Shasta-PRA-Backup's kill_port() with:
- Checks both 127.0.0.1 and 0.0.0.0 bindings
- Structured dict returns instead of just printing
- get_port_status() diagnostic
- CLI entry point with --port, --status, --kill flags

Usage:
    python -m app.utils.process_manager --status
    python -m app.utils.process_manager --kill
    python -m app.utils.process_manager --port 8855 --kill
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time


def get_port_pids(port: int) -> set[int]:
    """Return set of PIDs currently listening on the port.

    Checks both 127.0.0.1:{port} and 0.0.0.0:{port} since the app
    binds to 0.0.0.0.
    """
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return set()

    pids = set()
    patterns = [f"127.0.0.1:{port}", f"0.0.0.0:{port}"]
    my_pid = os.getpid()

    for line in result.stdout.splitlines():
        if "LISTENING" not in line:
            continue
        for pattern in patterns:
            if pattern in line:
                parts = line.strip().split()
                pid = parts[-1]
                if pid.isdigit() and int(pid) != my_pid:
                    pids.add(int(pid))
    return pids


def _get_process_name(pid: int) -> str:
    """Get the process name for a PID via tasklist."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.strip('"').split('","')
            if len(parts) >= 2:
                return parts[0]
    except Exception:
        pass
    return "unknown"


def kill_port(port: int) -> dict:
    """Kill all processes listening on the given port.

    Multi-strategy: SIGTERM -> taskkill /F /T -> verify.
    Returns structured dict with results.
    """
    pids = get_port_pids(port)
    result = {
        "port": port,
        "found_pids": sorted(pids),
        "killed": [],
        "failed": [],
        "port_free": False,
    }

    if not pids:
        result["port_free"] = is_port_free(port)
        return result

    print(f"  Killing {len(pids)} process(es) on port {port}: "
          f"{', '.join(str(p) for p in sorted(pids))}")

    # Strategy 1: os.kill with SIGTERM
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    time.sleep(1)
    remaining = get_port_pids(port)
    killed_s1 = pids - remaining
    result["killed"].extend(sorted(killed_s1))

    if not remaining:
        print("  Port cleared.")
        result["port_free"] = True
        return result

    # Strategy 2: taskkill /F /T (force + tree kill)
    for pid in remaining:
        try:
            os.system(f'taskkill /F /T /PID {pid} >nul 2>&1')
        except Exception:
            pass

    # Wait up to 5 seconds for the port to clear
    for _ in range(10):
        still = get_port_pids(port)
        if not still:
            result["killed"].extend(sorted(remaining))
            result["port_free"] = True
            print("  Port cleared.")
            return result
        time.sleep(0.5)

    still = get_port_pids(port)
    killed_s2 = remaining - still
    result["killed"].extend(sorted(killed_s2))
    result["failed"] = sorted(still)

    if still:
        print(f"  Warning: could not kill PIDs on port {port}: "
              f"{', '.join(str(p) for p in sorted(still))}")
        print(f"  Run manually:  taskkill /F /T /PID <pid>")
    else:
        result["port_free"] = True
        print("  Port cleared.")

    return result


def is_port_free(port: int) -> bool:
    """Test if port is available by attempting a socket bind."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        sock.close()
        return True
    except OSError:
        return False


def get_port_status(port: int) -> dict:
    """Get diagnostic info about a port: PIDs, process names, is_free."""
    pids = get_port_pids(port)
    processes = {}
    for pid in pids:
        processes[pid] = _get_process_name(pid)

    return {
        "port": port,
        "is_free": is_port_free(port) if not pids else False,
        "pids": sorted(pids),
        "processes": processes,
        "count": len(pids),
    }


def main():
    parser = argparse.ArgumentParser(description="Manage zombie processes on a port")
    parser.add_argument("--port", type=int, default=8855, help="Port to manage (default: 8855)")
    parser.add_argument("--status", action="store_true", help="Show port diagnostic info")
    parser.add_argument("--kill", action="store_true", help="Kill processes on the port")
    args = parser.parse_args()

    if args.status:
        status = get_port_status(args.port)
        print(f"\nPort {status['port']} status:")
        print(f"  Free: {status['is_free']}")
        print(f"  Processes: {status['count']}")
        for pid, name in status["processes"].items():
            print(f"    PID {pid}: {name}")
        if not status["pids"] and status["is_free"]:
            print("  No zombie processes detected.")
        sys.exit(0)

    if args.kill:
        result = kill_port(args.port)
        if result["killed"]:
            print(f"\nKilled PIDs: {result['killed']}")
        if result["failed"]:
            print(f"Failed to kill: {result['failed']}")
        if result["port_free"]:
            print(f"Port {args.port} is now free.")
        else:
            print(f"Port {args.port} may still be in use.")
        sys.exit(0 if result["port_free"] else 1)

    # Default: show status
    parser.print_help()


if __name__ == "__main__":
    main()
