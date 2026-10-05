import re
import subprocess
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from modal_jobs._cli import build_job, main, run_job

pytestmark = pytest.mark.modal

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"

DEPS_SCRIPT = """\
# /// script
# dependencies = ["six"]
# ///

import six

print(f"six version {six.__version__}")
"""


def test_run_job_hello(tmp_path, capfd):
    script = tmp_path / "hello.py"
    script.write_text("print('hello from modal')\n")
    run_job(build_job([str(script)]))
    assert "hello from modal" in capfd.readouterr().out


def test_run_job_with_dependencies(tmp_path, capfd):
    script = tmp_path / "deps.py"
    script.write_text(DEPS_SCRIPT)
    run_job(build_job([str(script)]))
    assert "six version" in capfd.readouterr().out


def test_run_job_failure_raises(tmp_path):
    script = tmp_path / "fail.py"
    script.write_text("import sys\nsys.exit(3)\n")
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        run_job(build_job([str(script)]))
    assert exc_info.value.returncode == 3


def test_cli_uv_run(tmp_path):
    script = tmp_path / "hello.py"
    script.write_text("print('hello from modal')\n")
    result = CliRunner().invoke(main, ["uv", "run", str(script)])
    assert result.exit_code == 0, result.output
    assert "Finished running hello.py" in result.output


def test_cli_uv_run_failure(tmp_path):
    script = tmp_path / "fail.py"
    script.write_text("import sys\nsys.exit(3)\n")
    result = CliRunner().invoke(main, ["uv", "run", str(script)])
    assert result.exit_code != 0
    assert "fail.py exited with code 3" in result.output


def test_cli_uv_run_command():
    result = CliRunner().invoke(
        main, ["uv", "run", "python", "-c", "print('hello from the cloud')"]
    )
    assert result.exit_code == 0, result.output


def test_run_job_command_with(capfd):
    run_job(build_job(["python", "-c", "import six; print(f'six {six.__version__}')"], ("six",)))
    assert "six " in capfd.readouterr().out


# `--with` arguments for examples that rely on them.
EXAMPLE_WITH = {
    "03_with.py": ("rich", "requests>=2,<3"),
}

# `--secret` arguments for examples that rely on them.
EXAMPLE_SECRETS = {
    "04_secret.py": (("GREETING", "hello"), ("TARGET", "modal")),
}

# `--gpu` arguments for examples that rely on them.
EXAMPLE_GPU = {
    "05_gpu.py": "T4",
}


@pytest.mark.parametrize("example", sorted(EXAMPLES_DIR.glob("*.py")), ids=lambda p: p.name)
def test_examples(example):
    run_job(
        build_job(
            [str(example)],
            EXAMPLE_WITH.get(example.name, ()),
            secrets=EXAMPLE_SECRETS.get(example.name, ()),
            gpu=EXAMPLE_GPU.get(example.name),
        )
    )


def test_cli_uv_run_with(tmp_path):
    script = tmp_path / "with_deps.py"
    script.write_text("import six\n\nprint(f'six version {six.__version__}')\n")
    result = CliRunner().invoke(main, ["uv", "run", "--with", "six", str(script)])
    assert result.exit_code == 0, result.output
    assert "Finished running with_deps.py" in result.output


def test_cli_uv_run_local_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "hello.txt").write_text("hello from local dir\n")
    script = tmp_path / "read_dir.py"
    script.write_text("print(open('/root/data/hello.txt').read())\n")
    result = CliRunner().invoke(main, ["uv", "run", "-v", "./data:/root/data", str(script)])
    assert result.exit_code == 0, result.output


@pytest.fixture
def modal_volume():
    import uuid

    import modal

    name = f"modal-jobs-test-{uuid.uuid4().hex[:8]}"
    modal.Volume.objects.create(name)
    yield modal.Volume.from_name(name)
    modal.Volume.objects.delete(name)


def test_cli_uv_run_modal_volume(tmp_path, modal_volume):
    script = tmp_path / "write_vol.py"
    script.write_text("open('/mnt/vol/out.txt', 'w').write('written on modal')\n")
    result = CliRunner().invoke(
        main, ["uv", "run", "--volume", f"{modal_volume.name}:/mnt/vol", str(script)]
    )
    assert result.exit_code == 0, result.output
    assert b"".join(modal_volume.read_file("out.txt")) == b"written on modal"


def test_cli_uv_run_local_secret(tmp_path):
    script = tmp_path / "read_secret.py"
    script.write_text("import os\n\nassert os.environ['MY_SECRET'] == 'value=1'\n")
    result = CliRunner().invoke(main, ["uv", "run", "-s", "MY_SECRET=value=1", str(script)])
    assert result.exit_code == 0, result.output


@pytest.fixture
def modal_secret():
    import uuid

    import modal

    name = f"modal-jobs-test-{uuid.uuid4().hex[:8]}"
    modal.Secret.objects.create(name, {"MODAL_SECRET": "from modal"})
    yield name
    modal.Secret.objects.delete(name)


def test_cli_uv_run_modal_secret(tmp_path, modal_secret):
    script = tmp_path / "read_secret.py"
    script.write_text(
        "import os\n\n"
        "assert os.environ['MODAL_SECRET'] == 'from modal'\n"
        "assert os.environ['LOCAL_SECRET'] == 'local'\n"
    )
    result = CliRunner().invoke(
        main,
        ["uv", "run", "--secret", modal_secret, "--secret", "LOCAL_SECRET=local", str(script)],
    )
    assert result.exit_code == 0, result.output


def test_cli_uv_run_gpu():
    result = CliRunner().invoke(main, ["uv", "run", "--gpu", "T4", "nvidia-smi"])
    assert result.exit_code == 0, result.output


def test_cli_run_image_add_python():
    result = CliRunner().invoke(
        main, ["run", "--add-python", "3.12", "docker.io/ubuntu", "echo", "Hello from the cloud!"]
    )
    assert result.exit_code == 0, result.output
    assert "Hello from the cloud!" in result.output


def test_cli_run_image_with_python():
    result = CliRunner().invoke(
        main, ["run", "python:3.12-slim", "python", "-c", "import sys; print(sys.version)"]
    )
    assert result.exit_code == 0, result.output
    assert "3.12" in result.output


def test_cli_uv_run_detach(tmp_path):
    script = tmp_path / "hello.py"
    script.write_text("print('hello from modal')\n")
    result = CliRunner().invoke(main, ["uv", "run", "--detach", str(script)])
    assert result.exit_code == 0, result.output
    assert "Started hello.py in the background" in result.output
    match = re.search(r"modal app stop (ap-\w+)", result.output)
    assert match is not None, result.output
    subprocess.run(["modal", "app", "stop", match.group(1)], check=False)


@pytest.fixture(scope="module")
def tracking_backend():
    from modal_jobs import _backend

    _backend.app.deploy()
    return _backend.registry()


def run_tracked(args):
    """Run `modal-jobs uv run ARGS` and return the job's app ID from Modal's output."""
    result = CliRunner().invoke(main, ["uv", "run", *args])
    match = re.search(r"modal\.com/apps/[^/]+/[^/]+/(ap-\w+)", result.output)
    assert match is not None, result.output
    return match.group(1)


def test_tracking_succeeded(tracking_backend):
    app_id = run_tracked(["python", "-c", "print('hi')"])
    record = tracking_backend.get_job.remote(app_id)
    assert record["status"] == "succeeded"
    assert record["exit_code"] == 0
    assert record["started_at"] <= record["finished_at"]
    assert record["peak_memory_mib"] > 0
    assert record["cpu_seconds"] >= 0
    assert record["task_id"].startswith("ta-")
    assert record["region"]


def test_tracking_killed(tracking_backend):
    kill = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    app_id = run_tracked(["python", "-c", kill])
    record = tracking_backend.get_job.remote(app_id)
    assert record["status"] == "failed"
    assert record["exit_code"] == -9
    result = CliRunner().invoke(main, ["show", app_id])
    assert "killed by SIGKILL" in result.output, result.output


def test_tracking_failed(tracking_backend, tmp_path):
    script = tmp_path / "fail.py"
    script.write_text("import sys\nsys.exit(3)\n")
    app_id = run_tracked([str(script)])
    record = tracking_backend.get_job.remote(app_id)
    assert record["status"] == "failed"
    assert record["exit_code"] == 3
    assert record["name"] == "fail.py"


def test_tracking_timed_out(tracking_backend):
    app_id = run_tracked(["--timeout", "10", "sleep", "60"])
    record = tracking_backend.get_job.remote(app_id)
    assert record["status"] == "timed_out"


def test_tracking_stopped(tracking_backend):
    app_id = run_tracked(["--detach", "sleep", "600"])
    assert tracking_backend.get_job.remote(app_id)["status"] == "running"
    subprocess.run(["modal", "app", "stop", "-y", app_id], check=True)
    # Modal takes a few seconds to report the job as terminated.
    for _ in range(30):
        result = CliRunner().invoke(main, ["show", app_id])
        assert result.exit_code == 0, result.output
        if "Status: running" not in result.output:
            break
        time.sleep(2)
    assert "Status: stopped" in result.output
