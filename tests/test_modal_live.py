import subprocess
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
    run_job(build_job(script))
    assert "hello from modal" in capfd.readouterr().out


def test_run_job_with_dependencies(tmp_path, capfd):
    script = tmp_path / "deps.py"
    script.write_text(DEPS_SCRIPT)
    run_job(build_job(script))
    assert "six version" in capfd.readouterr().out


def test_run_job_failure_raises(tmp_path):
    script = tmp_path / "fail.py"
    script.write_text("import sys\nsys.exit(3)\n")
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        run_job(build_job(script))
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


# `--with` arguments for examples that rely on them.
EXAMPLE_WITH = {
    "03_uv_with.py": ("rich", "requests>=2,<3"),
}


@pytest.mark.parametrize("example", sorted(EXAMPLES_DIR.glob("*.py")), ids=lambda p: p.name)
def test_examples(example):
    run_job(build_job(example, EXAMPLE_WITH.get(example.name, ())))


def test_cli_uv_run_with(tmp_path):
    script = tmp_path / "with_deps.py"
    script.write_text("import six\n\nprint(f'six version {six.__version__}')\n")
    result = CliRunner().invoke(main, ["uv", "run", "--with", "six", str(script)])
    assert result.exit_code == 0, result.output
    assert "Finished running with_deps.py" in result.output
