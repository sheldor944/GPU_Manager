"""nvidia-smi integration: read live GPU metrics from local or SSH-accessible hosts.

Each GPU in the DB has optional host/ssh_user/ssh_port/ssh_password fields.
If host is empty/localhost, nvidia-smi runs locally.
Otherwise connects via SSH (password or key-based auth).

Returned dict shape (per GPU):
    {
        "util_gpu":  int,   # GPU utilization %
        "util_mem":  int,   # memory controller utilization %
        "mem_used":  int,   # MiB
        "mem_total": int,   # MiB
        "temp_c":    int,   # degrees Celsius
        "power_w":   float | None,
    }
Returns None on any error.
"""
from __future__ import annotations

import logging
import shlex
import socket
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

_SMI_FIELDS = "utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw"
_SMI_CMD = f"nvidia-smi --query-gpu={_SMI_FIELDS} --format=csv,noheader,nounits"


def _run_local(timeout: int = 5) -> Optional[str]:
    try:
        result = subprocess.run(shlex.split(_SMI_CMD), capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def _run_ssh(
    host: str,
    user: Optional[str] = None,
    port: int = 22,
    password: Optional[str] = None,
    timeout: int = 8,
) -> Optional[str]:
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: dict = dict(
            hostname=host,
            port=port,
            username=user,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
        )
        if password:
            connect_kwargs["password"] = password
            connect_kwargs["look_for_keys"] = False
            connect_kwargs["allow_agent"] = False
        else:
            connect_kwargs["look_for_keys"] = True
        client.connect(**connect_kwargs)
        _, stdout, _ = client.exec_command(_SMI_CMD, timeout=timeout)
        output = stdout.read().decode().strip()
        client.close()
        return output or None
    except Exception as e:
        logger.debug("SSH metrics failed for %s: %s", host, e)
        return None


def _parse_smi_output(output: str) -> list[dict]:
    results = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            power_w = None
            if len(parts) >= 6 and parts[5] not in ("", "[N/A]", "N/A"):
                try:
                    power_w = float(parts[5])
                except ValueError:
                    pass
            results.append({
                "util_gpu": int(parts[0]),
                "util_mem": int(parts[1]),
                "mem_used": int(parts[2]),
                "mem_total": int(parts[3]),
                "temp_c": int(parts[4]),
                "power_w": power_w,
            })
        except (ValueError, IndexError):
            continue
    return results


def _get_output(
    host: Optional[str],
    ssh_user: Optional[str] = None,
    ssh_port: Optional[int] = None,
    ssh_password: Optional[str] = None,
) -> Optional[str]:
    if not host or host in ("localhost", "127.0.0.1"):
        return _run_local()
    return _run_ssh(
        host=host,
        user=ssh_user,
        port=ssh_port or 22,
        password=ssh_password,
    )


def fetch_gpu_metrics(
    host: Optional[str],
    gpu_index: int = 0,
    ssh_user: Optional[str] = None,
    ssh_port: Optional[int] = None,
    ssh_password: Optional[str] = None,
) -> Optional[dict]:
    """Return live metrics dict for a single GPU, or None if unavailable."""
    output = _get_output(host, ssh_user, ssh_port, ssh_password)
    if not output:
        return None
    rows = _parse_smi_output(output)
    return rows[gpu_index] if gpu_index < len(rows) else None


def fetch_all_metrics(
    host: Optional[str],
    ssh_user: Optional[str] = None,
    ssh_port: Optional[int] = None,
    ssh_password: Optional[str] = None,
) -> list[dict]:
    """Return metrics for all GPUs on a host."""
    output = _get_output(host, ssh_user, ssh_port, ssh_password)
    if not output:
        return []
    return _parse_smi_output(output)


def is_gpu_idle(metrics: dict, idle_threshold_pct: int = 5) -> bool:
    return metrics.get("util_gpu", 100) < idle_threshold_pct


def check_host_reachable(host: str, port: int = 22, timeout: float = 2.0) -> bool:
    """Quick TCP connect check — returns True if the SSH port is open."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _run_ssh_command(
    host: str,
    command: str,
    user: Optional[str] = None,
    port: int = 22,
    password: Optional[str] = None,
    timeout: int = 12,
) -> Optional[str]:
    """Run an arbitrary shell command on a remote host via SSH."""
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: dict = dict(
            hostname=host,
            port=port,
            username=user,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
        )
        if password:
            connect_kwargs["password"] = password
            connect_kwargs["look_for_keys"] = False
            connect_kwargs["allow_agent"] = False
        else:
            connect_kwargs["look_for_keys"] = True
        client.connect(**connect_kwargs)
        _, stdout, _ = client.exec_command(command, timeout=timeout)
        output = stdout.read().decode().strip()
        client.close()
        return output or None
    except Exception as e:
        logger.debug("SSH command failed for %s: %s", host, e)
        return None


_LIVE_STATS_CMD = (
    f"{_SMI_CMD}; echo '__SMI_DONE__'; "
    "df -BG 2>/dev/null"
    " | grep -v '^Filesystem\\|tmpfs\\|devtmpfs\\|overlay\\|squashfs\\|udev\\|/dev/loop'"
    " | sort -k2 -rn"
    " | awk '{print $2, $3, $4, $5, $6}'"
)


def _parse_disk_output(raw: str) -> Optional[dict]:
    """Parse multi-line df output (one real filesystem per line, sorted largest first).

    Each line format: 'SIZE USED AVAIL PCT% MOUNT'  e.g. '811G 716G 95G 88% /home'
    Returns a dict for the primary (largest) partition plus an 'all' list, or None.
    """
    entries = []
    for line in raw.strip().splitlines():
        parts = line.strip().split()
        if len(parts) < 4:
            continue
        try:
            entries.append({
                "total_gb": int(parts[0].rstrip("G")),
                "used_gb":  int(parts[1].rstrip("G")),
                "avail_gb": int(parts[2].rstrip("G")),
                "pct":      int(parts[3].rstrip("%")),
                "mount":    parts[4] if len(parts) >= 5 else "/",
            })
        except (ValueError, IndexError):
            continue
    if not entries:
        return None
    primary = entries[0]
    return {**primary, "all": entries}


def fetch_live_stats(
    host: Optional[str],
    gpu_index: int = 0,
    ssh_user: Optional[str] = None,
    ssh_port: Optional[int] = None,
    ssh_password: Optional[str] = None,
) -> tuple[Optional[dict], Optional[dict]]:
    """One SSH connection → (gpu_metrics_dict, disk_dict).  Either may be None."""
    if not host or host in ("localhost", "127.0.0.1"):
        try:
            result = subprocess.run(
                ["bash", "-c", _LIVE_STATS_CMD],
                capture_output=True, text=True, timeout=12,
            )
            raw = result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            raw = None
    else:
        raw = _run_ssh_command(
            host, _LIVE_STATS_CMD,
            user=ssh_user, port=ssh_port or 22, password=ssh_password,
        )

    if not raw:
        return None, None

    smi_part, _, disk_part = raw.partition("__SMI_DONE__")

    rows = _parse_smi_output(smi_part.strip())
    gpu_m = rows[gpu_index] if gpu_index < len(rows) else None

    disk_m = _parse_disk_output(disk_part.strip())
    return gpu_m, disk_m


_PROBE_CMD = (
    "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null; "
    "echo '__GPU_DONE__'; "
    "grep -m1 'model name' /proc/cpuinfo 2>/dev/null | awk -F': ' '{print $2}'; "
    "echo '__CPU_DONE__'; "
    "awk '/MemTotal/{printf \"%.0f\", $2/1024}' /proc/meminfo 2>/dev/null"
)


def fetch_server_specs(
    host: Optional[str],
    ssh_user: Optional[str] = None,
    ssh_port: Optional[int] = None,
    ssh_password: Optional[str] = None,
) -> dict:
    """SSH into a server and collect GPU model/VRAM, CPU, and RAM specs.

    Returns a dict with keys: gpus (list), cpu_model (str|None),
    ram_gb (float|None), error (str|None).
    """
    if not host or host in ("localhost", "127.0.0.1"):
        try:
            result = subprocess.run(
                ["bash", "-c", _PROBE_CMD],
                capture_output=True, text=True, timeout=12,
            )
            raw = result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            raw = None
    else:
        raw = _run_ssh_command(
            host, _PROBE_CMD,
            user=ssh_user, port=ssh_port or 22, password=ssh_password,
        )

    if not raw:
        return {"gpus": [], "cpu_model": None, "ram_gb": None,
                "error": "Could not connect or no output returned"}

    gpu_section, _, rest = raw.partition("__GPU_DONE__")
    cpu_section, _, ram_section = rest.partition("__CPU_DONE__")

    gpus = []
    for line in gpu_section.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        fields = [f.strip() for f in line.split(",")]
        if len(fields) >= 2:
            try:
                mem_mib = int(fields[1])
                gpus.append({
                    "name": fields[0],
                    "memory_mib": mem_mib,
                    "memory_gib": round(mem_mib / 1024, 1),
                })
            except ValueError:
                pass

    cpu_model = cpu_section.strip() or None

    ram_gb = None
    try:
        ram_gb = round(int(ram_section.strip()) / 1024, 1)
    except (ValueError, TypeError):
        pass

    return {"gpus": gpus, "cpu_model": cpu_model, "ram_gb": ram_gb, "error": None}
