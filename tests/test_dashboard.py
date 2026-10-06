import json

import pytest
from test_cli import FakeRegistry, make_record

from modal_jobs import _dashboard
from modal_jobs._store import JobStore


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path)


@pytest.fixture
def logs():
    return {}


@pytest.fixture
def app(store, logs):
    def read_log(job_id):
        try:
            return logs[job_id]
        except KeyError:
            raise FileNotFoundError(job_id) from None

    return _dashboard.create_app(lambda: FakeRegistry(store), read_log)


def callback(app, output):
    """Return the function of the callback of `app` that updates `output`."""
    return app.callback_map[output]["callback"].__wrapped__


def text(component) -> str:
    """Return all the text in the component tree `component`."""
    return json.dumps(component.to_plotly_json(), default=lambda c: c.to_plotly_json())


def test_serves_page_and_layout(app):
    client = app.server.test_client()

    assert client.get("/").status_code == 200
    assert client.get("/jobs/ap-1").status_code == 200
    layout = client.get("/_dash-layout")
    assert layout.status_code == 200
    assert "MantineProvider" in layout.get_data(as_text=True)


def test_route(app):
    route = callback(app, "page.children")

    assert route("/").children[1].children.id == "jobs-table"
    assert route("/jobs/ap-1").id == "job-detail"


def test_update_jobs_filters(app, store):
    store.put(make_record("ap-a", name="train.py"))
    store.put(make_record("ap-b", status="failed", name="train.py", submitted_at=1.0))
    store.put(make_record("ap-c", name="eval.py"))
    update_jobs = callback(app, "jobs-table.children")

    shown = text(update_jobs(None, "all", None))
    assert all(job_id in shown for job_id in ("ap-a", "ap-b", "ap-c"))

    shown = text(update_jobs(None, "failed", None))
    assert "ap-b" in shown and "ap-a" not in shown

    shown = text(update_jobs(None, "all", "TRAIN"))
    assert "ap-a" in shown and "ap-b" in shown and "ap-c" not in shown


def test_update_jobs_empty(app):
    assert "No jobs found." in text(callback(app, "jobs-table.children")(None, "all", None))


def test_update_job(app, store, logs):
    store.put(
        make_record("ap-a", status="failed", exit_code=3, finished_at=2.0, error="Boom", gpu="T4")
    )
    logs["ap-a"] = b"hello from the job\n"

    shown = text(callback(app, "job-detail.children")(None, "/jobs/ap"))

    assert "ap-a" in shown
    assert "Boom" in shown
    assert "python /root/job.py" in shown
    assert "hello from the job" in shown
    assert "T4" in shown


def test_update_job_running_without_log(app, store):
    store.put(make_record("ap-a"))

    shown = text(callback(app, "job-detail.children")(None, "/jobs/ap-a"))

    assert "modal-jobs logs --follow ap-a" in shown


def test_update_job_missing(app, store):
    store.put(make_record("ap-a"))
    store.put(make_record("ap-b"))
    update_job = callback(app, "job-detail.children")

    assert "No job found with ID 'ap-z'" in text(update_job(None, "/jobs/ap-z"))
    assert "ambiguous" in text(update_job(None, "/jobs/ap"))


def test_tail_log():
    assert _dashboard.tail_log(b"short") == "short"
    log = b"start" + b"x" * _dashboard.LOG_TAIL_BYTES + b"end"
    tail = _dashboard.tail_log(log)
    assert tail.startswith("... showing the last 200 KiB ...\n")
    assert tail.endswith("end")
    assert "start" not in tail
