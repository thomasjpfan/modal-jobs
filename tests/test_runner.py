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

    result = _runner.run_cmd(["python", "-c", "x = bytearray(64 * 1024 * 1024)"])

    assert result["exit_code"] == 0
    assert result["started_at"] <= result["finished_at"]
    assert result["cpu_seconds"] >= 0
    assert result["peak_memory_mib"] >= 64
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
    assert "peak_memory_mib" in error.stats
