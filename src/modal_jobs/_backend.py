"""The deployed `modal-jobs` app that records jobs as JSON files on a Modal volume.

A single `Registry` container owns the volume, so its view of the files is always
current. Running jobs are reconciled with their Modal function calls whenever they
are read, and periodically by a cron function.
"""

from pathlib import Path

import modal

from modal_jobs._store import RUNNING, JobStore, classify_outcome

APP_NAME = "modal-jobs"
VOLUME_NAME = "modal-jobs-db"
DATA_DIR = "/data"

app = modal.App(APP_NAME)
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
image = modal.Image.debian_slim().add_local_python_source("modal_jobs")


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
    def list_jobs(self, limit: int | None = None, status: str | None = None) -> list[dict]:
        # Filter after reconciling, since running jobs may have changed status.
        if status is None or status == RUNNING:
            records = self._reconcile(self.store.list(limit, status))
            if status is not None:
                records = [record for record in records if record["status"] == status]
            return records
        self._reconcile(self.store.running())
        return self.store.list(limit, status)

    @modal.method()
    def get_job(self, id_or_prefix: str) -> dict:
        return self._reconcile([self.store.get(id_or_prefix)])[0]

    @modal.method()
    def reconcile(self) -> int:
        """Reconcile all running jobs, and return how many are still running."""
        records = self._reconcile(self.store.running())
        return sum(record["status"] == RUNNING for record in records)


@app.function(image=image, schedule=modal.Period(minutes=10))
def reconcile_cron():
    Registry().reconcile.remote()


def registry():
    """Return a handle to the deployed `Registry`.

    Raises `modal.exception.NotFoundError` if the backend is not deployed.
    """
    cls = modal.Cls.from_name(APP_NAME, "Registry")
    cls.hydrate()
    return cls()
