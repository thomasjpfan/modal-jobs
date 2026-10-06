"""Formatting of job records, shared by the CLI and the dashboard."""

import shlex
import signal
import time


def format_duration(seconds: float) -> str:
    """Format `seconds` like `45s`, `3m12s`, `2h5m`, or `1d3h`."""
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{seconds}s"
    return f"{seconds}s"


def record_duration(record: dict, now: float | None = None) -> str:
    """Return how long the job of `record` ran, or has been running, or `-` if unknown."""
    started_at = record.get("started_at")
    if record["status"] == "running":
        # The start time is only known once the job finishes, so count from submission.
        started_at = record["submitted_at"]
        finished_at = time.time() if now is None else now
    else:
        finished_at = record.get("finished_at")
    if started_at is None or finished_at is None:
        return "-"
    return format_duration(max(finished_at - started_at, 0))


def format_timestamp(timestamp: float | None) -> str:
    if timestamp is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def format_memory(mebibytes: float) -> str:
    """Format a memory size in MiB like `512M` or `3.9G`."""
    if mebibytes < 1024:
        return f"{mebibytes:.0f}M"
    return f"{mebibytes / 1024:.1f}G"


def format_bytes(size: int) -> str:
    """Format a size in bytes like `512B`, `3K`, `512M`, or `3.9G`."""
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f}K"
    return format_memory(size / (1024 * 1024))


def format_exit_code(record: dict) -> str:
    """Format the exit code of `record`, naming the signal that killed the job, if any."""
    code = record["exit_code"]
    if code >= 0:
        return str(code)
    try:
        name = signal.Signals(-code).name
    except ValueError:
        return f"{code} (killed by signal {-code})"
    # The kernel's out-of-memory killer sends SIGKILL, but so can other things.
    if name == "SIGKILL":
        return f"{code} (killed by {name}, possibly out of memory)"
    return f"{code} (killed by {name})"


def format_labels(record: dict) -> str:
    """Format the labels of `record` like `team=ml,exp=3`, or `-` if it has none."""
    labels = record.get("labels") or {}
    return ",".join(f"{key}={value}" for key, value in labels.items()) or "-"


def usage_fields(record: dict) -> list[tuple[str, str]]:
    """Return the resource usage and location of the job of `record` as (label, value) pairs."""
    fields = []
    cpu_seconds = record.get("cpu_seconds")
    if cpu_seconds is not None:
        value = format_duration(cpu_seconds)
        wall = (record.get("finished_at") or 0) - (record.get("started_at") or 0)
        if wall > 0:
            value += f" ({cpu_seconds / wall:.1f} cores on average"
            value += f" of {record['cpu']:g})" if record.get("cpu") else ")"
        fields.append(("CPU time", value))
    if record.get("task_id"):
        where = ", ".join(value for value in (record.get("region"), record.get("cloud")) if value)
        fields.append(("Container", record["task_id"] + (f" ({where})" if where else "")))
    return fields


def record_fields(record: dict) -> list[tuple[str, str]]:
    """Return the details of a job record as (label, value) pairs, in display order."""
    fields = [("ID", record["id"]), ("Name", record["name"]), ("Status", record["status"])]
    if record.get("exit_code") is not None:
        fields.append(("Exit code", format_exit_code(record)))
    if record.get("error"):
        fields.append(("Error", record["error"]))
    fields += usage_fields(record)
    fields.append(("Command", shlex.join(record["command"])))
    if record.get("script"):
        fields.append(("Script", record["script"]))
    if record.get("image"):
        fields.append(("Image", record["image"]))
    if record.get("add_python"):
        fields.append(("Add Python", record["add_python"]))
    if record.get("dependencies"):
        fields.append(("Dependencies", ", ".join(record["dependencies"])))
    if record.get("gpu"):
        fields.append(("GPU", record["gpu"]))
    if record.get("cpu") is not None:
        fields.append(("CPU", f"{record['cpu']:g}"))
    if record.get("memory") is not None:
        fields.append(("Memory", f"{record['memory']} MiB"))
    if record.get("timeout") is not None:
        fields.append(("Timeout", f"{record['timeout']}s"))
    if record.get("retries"):
        fields.append(("Retries", str(record["retries"])))
    for name, dest in record.get("volumes", ()):
        fields.append(("Volume", f"{name} -> {dest}"))
    for local_dir, dest in record.get("local_dirs", ()):
        fields.append(("Local directory", f"{local_dir} -> {dest}"))
    for name in record.get("secrets", ()):
        fields.append(("Secret", name))
    for key in record.get("local_secret_keys", ()):
        fields.append(("Local secret", f"{key}=***"))
    for key, value in (record.get("labels") or {}).items():
        fields.append(("Label", f"{key}={value}"))
    fields += [
        ("Submitted by", record["submitted_by"]),
        ("Submitted", format_timestamp(record["submitted_at"])),
        ("Started", format_timestamp(record.get("started_at"))),
        ("Finished", format_timestamp(record.get("finished_at"))),
        ("Duration", record_duration(record)),
    ]
    return fields
