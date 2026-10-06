"""Job records stored as one JSON file per job.

A record is written when the job starts, with status `running`, and once more when
the job reaches a terminal status, after which it never changes.
"""

import builtins
import json
import os
import subprocess
import tempfile
from pathlib import Path

from modal.call_graph import InputStatus

RECORD_VERSION = 1
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
TIMED_OUT = "timed_out"
STOPPED = "stopped"
UNKNOWN = "unknown"
STATUSES = (RUNNING, SUCCEEDED, FAILED, TIMED_OUT, STOPPED, UNKNOWN)


class JobStore:
    """Job records in `root`, one `<id>.json` file per job.

    The source of the script a job ran, if any, is saved next to its record as `<id>.py`,
    so listing records doesn't read it.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, job_id: str) -> Path:
        if not job_id or "/" in job_id or job_id.startswith("."):
            raise ValueError(f"Invalid job ID {job_id!r}")
        return self.root / f"{job_id}.json"

    def _script_path(self, job_id: str) -> Path:
        return self._path(job_id).with_suffix(".py")

    def _write(self, path: Path, text: str) -> None:
        """Write `text` to `path` atomically."""
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def put(self, record: dict) -> None:
        """Write `record`, replacing any existing record with the same ID atomically."""
        self._write(self._path(record["id"]), json.dumps(record, indent=2))

    def put_script(self, job_id: str, source: str) -> None:
        """Save `source` as the script of the job `job_id`."""
        self._write(self._script_path(job_id), source)

    def get_script(self, job_id: str) -> str:
        """Return the script of the job `job_id`.

        Raises `FileNotFoundError` if the job has no saved script.
        """
        return self._script_path(job_id).read_text()

    def _ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        with os.scandir(self.root) as entries:
            return [
                entry.name.removesuffix(".json")
                for entry in entries
                if entry.name.endswith(".json") and not entry.name.startswith(".")
            ]

    def _read(self, job_id: str) -> dict:
        return json.loads(self._path(job_id).read_text())

    def get(self, id_or_prefix: str) -> dict:
        """Return the record whose ID is `id_or_prefix`, or starts with it.

        Raises `KeyError` if no record matches, or `ValueError` if a prefix matches
        more than one record.
        """
        ids = self._ids()
        if id_or_prefix in ids:
            return self._read(id_or_prefix)
        matches = [job_id for job_id in ids if job_id.startswith(id_or_prefix)]
        if not matches:
            raise KeyError(id_or_prefix)
        if len(matches) > 1:
            raise ValueError(f"Job ID {id_or_prefix!r} is ambiguous: {', '.join(sorted(matches))}")
        return self._read(matches[0])

    def list(
        self,
        limit: int | None = None,
        status: str | None = None,
        name: str | None = None,
        labels: dict[str, str | None] | None = None,
    ) -> list[dict]:
        """Return records newest first, optionally only those with `status`, `name`, and `labels`.

        A record matches `labels` if it has every key, with the given value unless it is None.
        """
        records = [self._read(job_id) for job_id in self._ids()]
        if status is not None:
            records = [record for record in records if record["status"] == status]
        if name is not None:
            records = [record for record in records if record["name"] == name]
        if labels:
            records = [record for record in records if has_labels(record, labels)]
        records.sort(key=lambda record: record["submitted_at"], reverse=True)
        return records[:limit] if limit is not None else records

    def running(self) -> builtins.list[dict]:
        return self.list(status=RUNNING)

    def delete(self, job_id: str) -> None:
        """Delete the record with ID `job_id` and its script, if any."""
        self._path(job_id).unlink(missing_ok=True)
        self._script_path(job_id).unlink(missing_ok=True)


def parse_label_filter(value: str) -> tuple[str, str | None]:
    """Parse a `KEY=VALUE` or `KEY` label filter into a `(key, value)` pair.

    A bare `KEY` matches any value, so its value is None.
    """
    key, sep, label_value = value.partition("=")
    if not key.strip():
        raise ValueError(f"Label key must not be empty, got {value!r}")
    return key, label_value if sep else None


def has_labels(record: dict, labels: dict[str, str | None]) -> bool:
    """Return True if `record` has every label in `labels`, with its value unless that is None."""
    record_labels = record.get("labels") or {}
    return all(
        key in record_labels and (value is None or record_labels[key] == value)
        for key, value in labels.items()
    )


def classify_outcome(call) -> dict:
    """Return the terminal fields for the job of the `modal.FunctionCall` `call`.

    Returns an empty dict if the job is still running.
    """
    import modal.exception

    try:
        result = call.get(timeout=0)
    except modal.exception.OutputExpiredError:
        return {"status": UNKNOWN, "error": "The job's result expired before it was recorded"}
    except modal.exception.FunctionTimeoutError as e:
        return {"status": TIMED_OUT, "error": str(e) or "The job timed out"}
    except TimeoutError:
        return _classify_unfinished(call)
    except subprocess.CalledProcessError as e:
        return {"status": FAILED, "exit_code": e.returncode, **getattr(e, "stats", {})}
    except modal.exception.InputCancellation:
        return {"status": STOPPED}
    except modal.exception.RemoteError as e:
        # Stopping the job's app, e.g. with `modal app stop`, terminates its input. The
        # call graph shows this a few seconds after the error, so wait for it to settle.
        statuses = _input_statuses(call)
        if InputStatus.TERMINATED in statuses:
            return {"status": STOPPED}
        if statuses <= {InputStatus.PENDING}:
            return {}
        return {"status": FAILED, "error": repr(e)}
    # Any other error, e.g. a crash outside the command, means the job failed.
    except Exception as e:  # noqa: BLE001
        return {"status": FAILED, "error": repr(e)}
    # The result holds the exit code and `_runner.job_stats`.
    return {"status": SUCCEEDED, "exit_code": 0, **result}


def _classify_unfinished(call) -> dict:
    """Classify a job whose call has no output yet.

    When the job's app is stopped, e.g. with `modal app stop`, the call never gets an
    output, but its input in the call graph is no longer pending.
    """
    if _input_statuses(call) <= {InputStatus.PENDING}:
        return {}
    # Give an output that is about to arrive a moment to show up before calling it stopped.
    try:
        call.get(timeout=2)
    except TimeoutError:
        return {"status": STOPPED}
    return classify_outcome(call)


def _input_statuses(call) -> set:
    """Return the statuses of the inputs of `call` in its call graph."""
    return {
        info.status for info in call.get_call_graph() if info.function_call_id == call.object_id
    }
