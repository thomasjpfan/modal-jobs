import os
import resource
import sys
import time
from subprocess import CalledProcessError, run


def job_stats(started_at: float) -> dict:
    """Return the timing, resource usage, and location of the command that just ran.

    `cpu_seconds` is the user and system CPU time of the command, and
    `peak_memory_mib` is the peak memory of its largest process.
    """
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    # `ru_maxrss` is in KiB on Linux and bytes on macOS.
    peak_memory_mib = usage.ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024)
    cloud = os.environ.get("MODAL_CLOUD_PROVIDER")
    return {
        "started_at": started_at,
        "finished_at": time.time(),
        "cpu_seconds": usage.ru_utime + usage.ru_stime,
        "peak_memory_mib": peak_memory_mib,
        "task_id": os.environ.get("MODAL_TASK_ID"),
        "region": os.environ.get("MODAL_REGION"),
        "cloud": cloud.removeprefix("CLOUD_PROVIDER_").lower() if cloud else None,
    }


def run_cmd(cmd):
    """Run `cmd` and return its exit code and `job_stats`.

    On failure, the `CalledProcessError` is re-raised with a `stats` attribute
    holding `job_stats`, so the job's outcome can be recorded either way.
    """
    started_at = time.time()
    try:
        run(cmd, check=True)
    except CalledProcessError as e:
        e.stats = job_stats(started_at)
        raise
    return {"exit_code": 0, **job_stats(started_at)}
