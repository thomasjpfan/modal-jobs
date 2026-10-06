import subprocess

import modal.exception
import pytest
from modal.call_graph import InputInfo, InputStatus

from modal_jobs._store import JobStore, classify_outcome, parse_label_filter


def make_record(job_id, submitted_at, status="running"):
    return {"id": job_id, "call_id": f"fc-{job_id}", "submitted_at": submitted_at, "status": status}


def test_put_get_round_trip(tmp_path):
    store = JobStore(tmp_path / "jobs")
    record = make_record("ap-abc", 1.0)
    store.put(record)
    assert store.get("ap-abc") == record


def test_put_overwrites(tmp_path):
    store = JobStore(tmp_path)
    store.put(make_record("ap-abc", 1.0))
    store.put(make_record("ap-abc", 1.0, status="succeeded"))
    assert store.get("ap-abc")["status"] == "succeeded"
    assert [p.name for p in tmp_path.iterdir()] == ["ap-abc.json"]


def test_delete(tmp_path):
    store = JobStore(tmp_path)
    store.put(make_record("ap-abc", 1.0))
    store.put(make_record("ap-def", 2.0))
    store.delete("ap-abc")
    store.delete("ap-missing")
    assert [record["id"] for record in store.list()] == ["ap-def"]


def test_script_round_trip(tmp_path):
    store = JobStore(tmp_path)
    store.put(make_record("ap-abc", 1.0))
    store.put_script("ap-abc", "print('hi')\n")

    assert store.get_script("ap-abc") == "print('hi')\n"
    # Scripts are not records.
    assert [record["id"] for record in store.list()] == ["ap-abc"]
    with pytest.raises(FileNotFoundError):
        store.get_script("ap-def")


def test_delete_removes_script(tmp_path):
    store = JobStore(tmp_path)
    store.put(make_record("ap-abc", 1.0))
    store.put_script("ap-abc", "print('hi')\n")

    store.delete("ap-abc")

    assert list(tmp_path.iterdir()) == []


def test_list_newest_first(tmp_path):
    store = JobStore(tmp_path)
    for i, job_id in enumerate(["ap-1", "ap-3", "ap-2"]):
        store.put(make_record(job_id, float(job_id[-1])))
    assert [r["id"] for r in store.list()] == ["ap-3", "ap-2", "ap-1"]
    assert [r["id"] for r in store.list(limit=2)] == ["ap-3", "ap-2"]


def test_list_empty(tmp_path):
    assert JobStore(tmp_path / "missing").list() == []


def test_list_status(tmp_path):
    store = JobStore(tmp_path)
    store.put(make_record("ap-1", 1.0, status="failed"))
    store.put(make_record("ap-2", 2.0))
    assert [r["id"] for r in store.list(status="failed")] == ["ap-1"]
    assert [r["id"] for r in store.running()] == ["ap-2"]


def test_list_name(tmp_path):
    store = JobStore(tmp_path)
    store.put({**make_record("ap-1", 1.0), "name": "train.py"})
    store.put({**make_record("ap-2", 2.0), "name": "eval.py"})
    store.put({**make_record("ap-3", 3.0), "name": "train.py"})
    store.put({**make_record("ap-4", 4.0), "name": "eval.py"})
    assert [r["id"] for r in store.list(name="train.py")] == ["ap-3", "ap-1"]
    # The name filter applies before the limit.
    assert [r["id"] for r in store.list(limit=1, name="train.py")] == ["ap-3"]


def test_list_labels(tmp_path):
    store = JobStore(tmp_path)
    store.put({**make_record("ap-1", 1.0), "labels": {"team": "ml", "exp": "1"}})
    store.put({**make_record("ap-2", 2.0), "labels": {"team": "infra"}})
    store.put({**make_record("ap-3", 3.0), "labels": {"team": "ml", "draft": ""}})
    # Records from before labels existed have none.
    store.put(make_record("ap-4", 4.0))
    assert [r["id"] for r in store.list(labels={"team": "ml"})] == ["ap-3", "ap-1"]
    assert [r["id"] for r in store.list(labels={"team": None})] == ["ap-3", "ap-2", "ap-1"]
    assert [r["id"] for r in store.list(labels={"draft": ""})] == ["ap-3"]
    assert [r["id"] for r in store.list(labels={"team": "ml", "exp": "1"})] == ["ap-1"]
    # The label filter applies before the limit.
    assert [r["id"] for r in store.list(limit=1, labels={"exp": None})] == ["ap-1"]


def test_parse_label_filter():
    assert parse_label_filter("team=ml") == ("team", "ml")
    assert parse_label_filter("url=a=b") == ("url", "a=b")
    assert parse_label_filter("team=") == ("team", "")
    assert parse_label_filter("team") == ("team", None)
    with pytest.raises(ValueError, match="Label key must not be empty"):
        parse_label_filter("=ml")


def test_get_prefix(tmp_path):
    store = JobStore(tmp_path)
    store.put(make_record("ap-abc", 1.0))
    store.put(make_record("ap-abd", 2.0))
    assert store.get("ap-abc")["id"] == "ap-abc"
    with pytest.raises(ValueError, match="ambiguous"):
        store.get("ap-ab")
    with pytest.raises(KeyError):
        store.get("ap-x")


def test_get_exact_id_that_prefixes_another(tmp_path):
    store = JobStore(tmp_path)
    store.put(make_record("ap-ab", 1.0))
    store.put(make_record("ap-abc", 2.0))
    assert store.get("ap-ab")["id"] == "ap-ab"


@pytest.mark.parametrize("job_id", ["", "../x", ".hidden", "a/b"])
def test_invalid_id(tmp_path, job_id):
    with pytest.raises(ValueError, match="Invalid job ID"):
        JobStore(tmp_path).put(make_record(job_id, 1.0))


class FakeCall:
    object_id = "fc-1"

    def __init__(self, result=None, error=None, input_status=InputStatus.PENDING):
        self.result = result
        self.error = error
        self.input_status = input_status

    def get(self, timeout=None):
        if self.error is not None:
            raise self.error
        return self.result

    def get_call_graph(self):
        return [InputInfo("in-1", self.object_id, "ta-1", self.input_status, "f", "m", [])]


def test_classify_running():
    assert classify_outcome(FakeCall(error=TimeoutError())) == {}


def test_classify_stopped():
    call = FakeCall(error=TimeoutError(), input_status=InputStatus.FAILURE)
    assert classify_outcome(call) == {"status": "stopped"}


def test_classify_succeeded():
    call = FakeCall({"exit_code": 0, "started_at": 1.0, "finished_at": 2.0})
    assert classify_outcome(call) == {
        "status": "succeeded",
        "exit_code": 0,
        "started_at": 1.0,
        "finished_at": 2.0,
    }


def test_classify_failed_without_stats():
    error = subprocess.CalledProcessError(3, ["python"])
    assert classify_outcome(FakeCall(error=error)) == {"status": "failed", "exit_code": 3}


def test_classify_failed_with_exit_code():
    error = subprocess.CalledProcessError(3, ["python"])
    error.stats = {"started_at": 1.0, "finished_at": 2.0}
    assert classify_outcome(FakeCall(error=error)) == {
        "status": "failed",
        "exit_code": 3,
        "started_at": 1.0,
        "finished_at": 2.0,
    }


def test_classify_timed_out():
    error = modal.exception.FunctionTimeoutError("Function timed out")
    assert classify_outcome(FakeCall(error=error)) == {
        "status": "timed_out",
        "error": "Function timed out",
    }


def test_classify_expired():
    outcome = classify_outcome(FakeCall(error=modal.exception.OutputExpiredError()))
    assert outcome["status"] == "unknown"


def test_classify_cancelled():
    outcome = classify_outcome(FakeCall(error=modal.exception.InputCancellation()))
    assert outcome == {"status": "stopped"}


def test_classify_other_error():
    outcome = classify_outcome(FakeCall(error=RuntimeError("boom")))
    assert outcome == {"status": "failed", "error": "RuntimeError('boom')"}


def test_classify_terminated():
    error = modal.exception.RemoteError("")
    call = FakeCall(error=error, input_status=InputStatus.TERMINATED)
    assert classify_outcome(call) == {"status": "stopped"}


def test_classify_remote_error():
    error = modal.exception.RemoteError("crashed")
    call = FakeCall(error=error, input_status=InputStatus.FAILURE)
    assert classify_outcome(call) == {"status": "failed", "error": "RemoteError('crashed')"}


def test_classify_remote_error_before_call_graph_settles():
    call = FakeCall(error=modal.exception.RemoteError(""))
    assert classify_outcome(call) == {}
