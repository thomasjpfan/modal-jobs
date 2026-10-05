import pytest
from click.testing import CliRunner

from modal_jobs import _cli
from modal_jobs._cli import JobSpec, build_job, main, parse_script_metadata, split_requirements

UV_SCRIPT = """\
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests<3",
#   "rich",
# ]
# ///

import requests
"""


def test_build_job(tmp_path):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    job = build_job(script)
    assert job == JobSpec(
        local_path=script,
        remote_path="/root/job.py",
        function_name="job",
        command=["python", "/root/job.py"],
        dependencies=(),
    )


def test_build_job_with_dependencies(tmp_path):
    script = tmp_path / "job.py"
    script.write_text(UV_SCRIPT)
    job = build_job(script)
    assert job.dependencies == ("requests<3", "rich")


def test_parse_script_metadata():
    assert parse_script_metadata(UV_SCRIPT) == {
        "requires-python": ">=3.11",
        "dependencies": ["requests<3", "rich"],
    }


def test_parse_script_metadata_no_block():
    assert parse_script_metadata("print('hi')\n") == {}


def test_parse_script_metadata_ignores_other_types():
    script = "# /// pyproject\n# [tool.foo]\n# ///\n"
    assert parse_script_metadata(script) == {}


def test_parse_script_metadata_multiple_blocks():
    block = "# /// script\n# dependencies = []\n# ///\n"
    with pytest.raises(ValueError, match="Multiple"):
        parse_script_metadata(block + "\n" + block)


def test_run_runs_built_job(tmp_path, monkeypatch):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(main, ["uv", "run", str(script)])

    assert result.exit_code == 0, result.output
    assert calls == [build_job(script)]


def test_run_rejects_invalid_metadata(tmp_path, monkeypatch):
    script = tmp_path / "job.py"
    script.write_text("# /// script\n# dependencies = [\n# ///\n")
    monkeypatch.setattr(_cli, "run_job", lambda job: None)

    result = CliRunner().invoke(main, ["uv", "run", str(script)])

    assert result.exit_code == 1
    assert "Invalid script metadata" in result.output


def test_run_rejects_missing_file():
    result = CliRunner().invoke(main, ["uv", "run", "does_not_exist.py"])
    assert result.exit_code == 2
    assert "does not exist" in result.output


@pytest.mark.parametrize(
    "value, expected",
    [
        ("rich", ["rich"]),
        ("rich,six", ["rich", "six"]),
        ("rich, six", ["rich", "six"]),
        ("requests>=2,<3", ["requests>=2,<3"]),
        ("requests>=2, <3,rich", ["requests>=2, <3", "rich"]),
        ("pkg[a,b],six", ["pkg[a,b]", "six"]),
        ("rich,", ["rich"]),
    ],
)
def test_split_requirements(value, expected):
    assert split_requirements(value) == expected


def test_build_job_with(tmp_path):
    script = tmp_path / "job.py"
    script.write_text(UV_SCRIPT)
    job = build_job(script, ("six", "numpy>=2,<3,pandas"))
    assert job.dependencies == ("requests<3", "rich", "six", "numpy>=2,<3", "pandas")


def test_run_with(tmp_path, monkeypatch):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(
        main, ["uv", "run", "--with", "rich", "--with", "six,requests>=2,<3", str(script)]
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0].dependencies == ("rich", "six", "requests>=2,<3")
