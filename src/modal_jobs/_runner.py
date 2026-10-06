import os
import resource
import sys
import time
from subprocess import PIPE, STDOUT, CalledProcessError, Popen, run


def job_stats(started_at: float) -> dict:
    """Return the timing, resource usage, and location of the command that just ran.

    `cpu_seconds` is the user and system CPU time of the command.
    """
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    cloud = os.environ.get("MODAL_CLOUD_PROVIDER")
    return {
        "started_at": started_at,
        "finished_at": time.time(),
        "cpu_seconds": usage.ru_utime + usage.ru_stime,
        "task_id": os.environ.get("MODAL_TASK_ID"),
        "region": os.environ.get("MODAL_REGION"),
        "cloud": cloud.removeprefix("CLOUD_PROVIDER_").lower() if cloud else None,
    }


def run_cmd(cmd, log_path=None, log_volume=None):
    """Run `cmd` and return its exit code and `job_stats`.

    If `log_path` is set, the command's stdout and stderr are also appended to that
    file, and the Modal volume named `log_volume`, if any, is committed afterwards.

    On failure, the `CalledProcessError` is re-raised with a `stats` attribute
    holding `job_stats`, so the job's outcome can be recorded either way.
    """
    started_at = time.time()
    try:
        if log_path is None:
            run(cmd, check=True)
        else:
            try:
                run_logged(cmd, log_path)
            finally:
                commit_volume(log_volume)
    except CalledProcessError as e:
        e.stats = job_stats(started_at)
        raise
    return {"exit_code": 0, **job_stats(started_at)}


def run_logged(cmd, log_path):
    """Run `cmd`, writing its combined stdout and stderr to stdout and appending it to `log_path`.

    Raises `CalledProcessError` if the command fails.
    """
    with open(log_path, "ab") as log:
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Retries append to the same file, so mark where each attempt starts.
        log.write(f"--- attempt started at {started} ---\n".encode())
        log.flush()
        with Popen(cmd, stdout=PIPE, stderr=STDOUT) as proc:
            # Read whatever is available, so partial lines like progress bars show up right away.
            while chunk := os.read(proc.stdout.fileno(), 65536):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                log.write(chunk)
                log.flush()
    if proc.returncode:
        raise CalledProcessError(proc.returncode, cmd)


def commit_volume(name):
    """Commit the Modal volume `name`, warning instead of failing if that doesn't work."""
    if name is None:
        return
    import modal

    try:
        modal.Volume.from_name(name).commit()
    # Saving the log must never change the job's outcome.
    except Exception as e:  # noqa: BLE001
        print(f"Could not save the job's log ({type(e).__name__}: {e})", file=sys.stderr)
