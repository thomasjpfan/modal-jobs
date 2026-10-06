"""The deployed `modal-jobs` app that records jobs as JSON files on a Modal volume.

A single `Registry` container owns the volume, so its view of the files is always
current. Running jobs are reconciled with their Modal function calls whenever they
are read, and periodically by a cron function.
"""

import time
from pathlib import Path

import modal

from modal_jobs._store import RUNNING, STOPPED, JobStore, classify_outcome

APP_NAME = "modal-jobs"
VOLUME_NAME = "modal-jobs-db"
DATA_DIR = "/data"
# Each job appends its output to `<job ID>.log` on this volume.
LOGS_VOLUME_NAME = "modal-jobs-logs"

app = modal.App(APP_NAME)
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
image = modal.Image.debian_slim().add_local_python_source("modal_jobs")
# Only the dashboard needs Dash, so the registry's image stays small.
dashboard_image = (
    modal.Image.debian_slim()
    .uv_pip_install("dash>=4", "dash-mantine-components>=2")
    .add_local_python_source("modal_jobs")
)


@app.cls(image=image, volumes={DATA_DIR: volume}, max_containers=1, scaledown_window=300)
class Registry:
    @modal.enter()
    def open_store(self):
        self.store = JobStore(Path(DATA_DIR) / "jobs")

    def _reconcile(self, records: list[dict]) -> list[dict]:
        """Update the running jobs in `records` that finished, and return all records."""
        updated = []
        for i, record in enumerate(records):
            if record["status"] != RUNNING:
                continue
            outcome = classify_outcome(modal.FunctionCall.from_id(record["call_id"]))
            if outcome:
                records[i] = {**record, **{k: v for k, v in outcome.items() if v is not None}}
                updated.append(records[i])
        for record in updated:
            self.store.put(record)
        if updated:
            volume.commit()
        return records

    @modal.method()
    def create_job(self, record: dict) -> None:
        self.store.put(record)
        volume.commit()

    @modal.method()
    def list_jobs(
        self,
        limit: int | None = None,
        status: str | None = None,
        name: str | None = None,
        labels: dict[str, str | None] | None = None,
    ) -> list[dict]:
        # Filter by status after reconciling, since running jobs may have changed status.
        # Names and labels never change, so the store can filter by them.
        if status is None or status == RUNNING:
            records = self._reconcile(self.store.list(limit, status, name, labels))
            if status is not None:
                records = [record for record in records if record["status"] == status]
            return records
        self._reconcile(self.store.running())
        return self.store.list(limit, status, name, labels)

    @modal.method()
    def get_job(self, id_or_prefix: str) -> dict:
        return self._reconcile([self.store.get(id_or_prefix)])[0]

    @modal.method()
    def stop_job(self, id_or_prefix: str, stopped_by: str) -> dict:
        """Stop the job `id_or_prefix` if it is running, and return its record."""
        record = self._reconcile([self.store.get(id_or_prefix)])[0]
        if record["status"] != RUNNING:
            return record
        modal.FunctionCall.from_id(record["call_id"]).cancel(terminate_containers=True)
        record = {
            **record,
            "status": STOPPED,
            "finished_at": time.time(),
            "error": f"Stopped by {stopped_by}",
        }
        self.store.put(record)
        volume.commit()
        return record

    @modal.method()
    def delete_jobs(self, ids: list[str]) -> list[str]:
        """Delete the records of the finished jobs `ids`, and return the deleted IDs.

        Raises `ValueError` without deleting anything if any of the jobs is running.
        """
        records = self._reconcile([self.store.get(job_id) for job_id in ids])
        running = [record["id"] for record in records if record["status"] == RUNNING]
        if running:
            raise ValueError(f"Jobs still running: {', '.join(running)}")
        for record in records:
            self.store.delete(record["id"])
        volume.commit()
        return [record["id"] for record in records]

    @modal.method()
    def reconcile(self) -> int:
        """Reconcile all running jobs, and return how many are still running."""
        records = self._reconcile(self.store.running())
        return sum(record["status"] == RUNNING for record in records)


@app.function(image=image, schedule=modal.Period(minutes=10))
def reconcile_cron():
    Registry().reconcile.remote()


@app.function(image=dashboard_image, scaledown_window=300)
@modal.concurrent(max_inputs=50)
@modal.wsgi_app()
def dashboard():
    """Serve the read-only web dashboard of the jobs."""
    from modal_jobs._dashboard import create_app

    return create_app(Registry, read_log).server


def dashboard_url() -> str:
    """Return the URL of the deployed dashboard.

    Raises `modal.exception.NotFoundError` if the deployed backend has no dashboard.
    """
    return modal.Function.from_name(APP_NAME, "dashboard").get_web_url()


def registry():
    """Return a handle to the deployed `Registry`.

    Raises `modal.exception.NotFoundError` if the backend is not deployed.
    """
    cls = modal.Cls.from_name(APP_NAME, "Registry")
    cls.hydrate()
    return cls()


def read_log(job_id: str) -> bytes:
    """Return the saved output of the job `job_id`.

    Raises `FileNotFoundError` if the job has no saved output.
    """
    volume = modal.Volume.from_name(LOGS_VOLUME_NAME, create_if_missing=True)
    return b"".join(volume.read_file(f"{job_id}.log"))


def delete_logs(job_ids: list[str]) -> None:
    """Delete the saved output of the jobs `job_ids`, skipping jobs without any."""
    volume = modal.Volume.from_name(LOGS_VOLUME_NAME, create_if_missing=True)
    # Removing a missing file raises a generic `InvalidError`, so only remove saved ones.
    saved = {entry.path for entry in volume.listdir("/")}
    for job_id in job_ids:
        if f"{job_id}.log" in saved:
            volume.remove_file(f"{job_id}.log")


def logs_usage() -> tuple[int, int]:
    """Return the number of saved log files and their total size in bytes."""
    volume = modal.Volume.from_name(LOGS_VOLUME_NAME, create_if_missing=True)
    entries = [entry for entry in volume.listdir("/") if entry.path.endswith(".log")]
    return len(entries), sum(entry.size for entry in entries)
