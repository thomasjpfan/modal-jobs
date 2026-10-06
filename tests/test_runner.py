import ast
import pickle
import subprocess
from pathlib import Path

import pytest

from modal_jobs import _runner


def test_runner_does_not_import_modal_jobs():
    # _runner.py is mounted alone into the Modal container, so it can't
    # import anything else from the modal_jobs package.
    tree = ast.parse(Path(_runner.__file__).read_text())

    bad_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                bad_imports.append(f"line {node.lineno}: relative import")
                continue
            names = [node.module]
        else:
            continue
        for name in names:
            if name == "modal_jobs" or name.startswith("modal_jobs."):
                bad_imports.append(f"line {node.lineno}: {name}")

    assert not bad_imports, f"_runner.py imports from modal_jobs: {bad_imports}"


def test_run_cmd_returns_stats(monkeypatch):
    monkeypatch.setenv("MODAL_TASK_ID", "ta-123")
    monkeypatch.setenv("MODAL_REGION", "us-west")
    monkeypatch.setenv("MODAL_CLOUD_PROVIDER", "CLOUD_PROVIDER_AZURE")

    result = _runner.run_cmd(["true"])

    assert result["exit_code"] == 0
    assert result["started_at"] <= result["finished_at"]
    assert result["cpu_seconds"] >= 0
    assert result["task_id"] == "ta-123"
    assert result["region"] == "us-west"
    assert result["cloud"] == "azure"


def test_run_cmd_stats_outside_modal(monkeypatch):
    for name in ("MODAL_TASK_ID", "MODAL_REGION", "MODAL_CLOUD_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    result = _runner.run_cmd(["true"])
    assert result["task_id"] is result["region"] is result["cloud"] is None


def test_run_cmd_failure_carries_stats():
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        _runner.run_cmd(["false"])
    error = pickle.loads(pickle.dumps(exc_info.value))
    assert error.returncode == 1
    assert error.stats["started_at"] <= error.stats["finished_at"]
    assert "cpu_seconds" in error.stats


def test_run_cmd_saves_log(tmp_path, capfd):
    log_path = tmp_path / "job.log"
    script = "import sys; print('out'); print('err', file=sys.stderr)"

    _runner.run_cmd(["python", "-c", script], str(log_path))

    log = log_path.read_text()
    assert log.startswith("--- attempt started at ")
    assert "out\n" in log
    assert "err\n" in log
    out = capfd.readouterr().out
    assert "out\n" in out
    assert "err\n" in out


def test_run_cmd_failure_saves_log(tmp_path):
    log_path = tmp_path / "job.log"
    script = "import sys; print('before failing'); sys.exit(3)"

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        _runner.run_cmd(["python", "-c", script], str(log_path))

    assert exc_info.value.returncode == 3
    assert "cpu_seconds" in exc_info.value.stats
    assert "before failing" in log_path.read_text()


def test_run_cmd_appends_log(tmp_path):
    log_path = tmp_path / "job.log"

    _runner.run_cmd(["echo", "first"], str(log_path))
    _runner.run_cmd(["echo", "second"], str(log_path))

    log = log_path.read_text()
    assert log.count("--- attempt started at ") == 2
    assert log.index("first") < log.index("second")
