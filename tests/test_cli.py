import pytest
from click.testing import CliRunner

from modal_jobs import _cli
from modal_jobs._cli import (
    JobSpec,
    build_job,
    is_local_path,
    main,
    parse_script_metadata,
    parse_secret,
    parse_volume,
    split_requirements,
)

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
    job = build_job([str(script)])
    assert job == JobSpec(
        function_name="job",
        command=["python", "/root/job.py"],
        local_path=script,
        remote_path="/root/job.py",
        dependencies=(),
    )


def test_build_job_script_args(tmp_path):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    job = build_job([str(script), "--n", "3"])
    assert job.command == ["python", "/root/job.py", "--n", "3"]


def test_build_job_command():
    job = build_job(["python", "-c", "print(1)"], ("six",))
    assert job == JobSpec(
        function_name="python",
        command=["python", "-c", "print(1)"],
        dependencies=("six",),
    )


def test_build_job_command_function_name():
    assert build_job(["/usr/bin/python3.12", "-V"]).function_name == "python3_12"


def test_build_job_with_dependencies(tmp_path):
    script = tmp_path / "job.py"
    script.write_text(UV_SCRIPT)
    job = build_job([str(script)])
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
    assert calls == [build_job([str(script)])]


def test_run_command(monkeypatch):
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(
        main, ["uv", "run", "--with", "six", "python", "-c", 'print("Hello from the cloud!")']
    )

    assert result.exit_code == 0, result.output
    assert calls == [build_job(["python", "-c", 'print("Hello from the cloud!")'], ("six",))]
    assert "Finished running python -c" in result.output


def test_run_script_args_not_parsed_as_options(tmp_path, monkeypatch):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(main, ["uv", "run", str(script), "-v", "x", "--with", "y"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0].command == ["python", "/root/job.py", "-v", "x", "--with", "y"]
    assert calls[0].volumes == ()
    assert calls[0].dependencies == ()


def test_run_requires_command():
    result = CliRunner().invoke(main, ["uv", "run"])
    assert result.exit_code == 2
    assert "Missing argument 'COMMAND...'" in result.output


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
    job = build_job([str(script)], ("six", "numpy>=2,<3,pandas"))
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


@pytest.mark.parametrize(
    "source, expected",
    [
        ("my-modal-volume", False),
        ("my_vol.v2", False),
        (".", True),
        ("./data", True),
        ("../data", True),
        ("~/data", True),
        ("/abs/data", True),
        ("data/sub", True),
    ],
)
def test_is_local_path(source, expected):
    assert is_local_path(source) == expected


def test_parse_volume_modal_volume():
    assert parse_volume("my-modal-volume:/mnt/vol") == ("my-modal-volume", "/mnt/vol")


def test_parse_volume_local_dir(tmp_path):
    (tmp_path / "data").mkdir()
    assert parse_volume("./data:/root/data", cwd=tmp_path) == (tmp_path / "data", "/root/data")
    assert parse_volume(f"{tmp_path}/data:/root/data") == (tmp_path / "data", "/root/data")


@pytest.mark.parametrize(
    "value, match",
    [
        ("my-modal-volume", "SOURCE:DEST"),
        (":/mnt/vol", "SOURCE:DEST"),
        ("vol:", "SOURCE:DEST"),
        ("vol:mnt/vol", "absolute"),
        ("./missing:/root/missing", "does not exist"),
    ],
)
def test_parse_volume_invalid(tmp_path, value, match):
    with pytest.raises(ValueError, match=match):
        parse_volume(value, cwd=tmp_path)


def test_build_job_with_volumes(tmp_path):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    job = build_job(
        [str(script)], volumes=(("my-modal-volume", "/mnt/vol"), (tmp_path / "data", "/root/data"))
    )
    assert job.volumes == (("my-modal-volume", "/mnt/vol"),)
    assert job.local_dirs == ((tmp_path / "data", "/root/data"),)


@pytest.mark.parametrize("flag", ["-v", "--volume"])
def test_run_volume(tmp_path, monkeypatch, flag):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "my-local-folder").mkdir()
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(
        main,
        [
            "uv",
            "run",
            flag,
            "my-modal-volume:/mnt/vol",
            flag,
            "./my-local-folder:/root/my-local-folder",
            str(script),
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0].volumes == (("my-modal-volume", "/mnt/vol"),)
    assert calls[0].local_dirs == ((tmp_path / "my-local-folder", "/root/my-local-folder"),)


def test_run_volume_invalid(tmp_path, monkeypatch):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    monkeypatch.setattr(_cli, "run_job", lambda job: None)

    result = CliRunner().invoke(main, ["uv", "run", "-v", "vol:relative", str(script)])

    assert result.exit_code == 2
    assert "Invalid value for '-v' / '--volume'" in result.output
    assert "absolute" in result.output


def test_parse_secret():
    assert parse_secret("my-modal-secret") == "my-modal-secret"
    assert parse_secret("MY_SECRET=value") == ("MY_SECRET", "value")
    assert parse_secret("MY_SECRET=a=b") == ("MY_SECRET", "a=b")
    assert parse_secret("MY_SECRET=") == ("MY_SECRET", "")


@pytest.mark.parametrize(
    "value, match",
    [
        ("", "must not be empty"),
        ("=value", "Invalid environment variable name"),
        ("1BAD=value", "Invalid environment variable name"),
        ("BAD-NAME=value", "Invalid environment variable name"),
    ],
)
def test_parse_secret_invalid(value, match):
    with pytest.raises(ValueError, match=match):
        parse_secret(value)


def test_build_job_with_secrets(tmp_path):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    job = build_job([str(script)], secrets=("my-modal-secret", ("MY_SECRET", "value")))
    assert job.secrets == ("my-modal-secret",)
    assert job.local_secrets == (("MY_SECRET", "value"),)


@pytest.mark.parametrize("flag", ["-s", "--secret", "--secrets"])
def test_run_secret(tmp_path, monkeypatch, flag):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(
        main, ["uv", "run", flag, "my-modal-secret", flag, "MY_SECRET=value", str(script)]
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0].secrets == ("my-modal-secret",)
    assert calls[0].local_secrets == (("MY_SECRET", "value"),)


def test_run_secret_invalid(tmp_path, monkeypatch):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    monkeypatch.setattr(_cli, "run_job", lambda job: None)

    result = CliRunner().invoke(main, ["uv", "run", "-s", "BAD-NAME=value", str(script)])

    assert result.exit_code == 2
    assert "Invalid value for '-s' / '--secret' / '--secrets'" in result.output


def test_build_job_with_gpu(tmp_path):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    assert build_job([str(script)]).gpu is None
    assert build_job([str(script)], gpu="T4").gpu == "T4"


def test_run_gpu(tmp_path, monkeypatch):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(main, ["uv", "run", "--gpu", "H100:8", str(script)])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0].gpu == "H100:8"


def test_build_job_with_image(tmp_path):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    job = build_job([str(script), "-x"], image="python:3.12-slim", add_python="3.12")
    assert job == JobSpec(
        function_name="job_py",
        command=[str(script), "-x"],
        image="python:3.12-slim",
        add_python="3.12",
    )


def test_docker_run(monkeypatch):
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(main, ["run", "docker.io/ubuntu", "echo", "Hello from the cloud!"])

    assert result.exit_code == 0, result.output
    assert calls == [build_job(["echo", "Hello from the cloud!"], image="docker.io/ubuntu")]
    assert "Finished running echo 'Hello from the cloud!'" in result.output


def test_docker_run_options(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(
        main,
        [
            "run",
            "--add-python",
            "3.12",
            "-v",
            "./data:/data",
            "-s",
            "MY_SECRET=value",
            "--gpu",
            "T4",
            "docker.io/ubuntu",
            "ls",
            "-v",
            "--gpu",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        JobSpec(
            function_name="ls",
            command=["ls", "-v", "--gpu"],
            local_dirs=((tmp_path / "data", "/data"),),
            local_secrets=(("MY_SECRET", "value"),),
            gpu="T4",
            image="docker.io/ubuntu",
            add_python="3.12",
        )
    ]


@pytest.mark.parametrize("args", [["run"], ["run", "docker.io/ubuntu"]])
def test_docker_run_missing_args(args):
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 2
    assert "Missing argument" in result.output


def test_docker_run_image_without_python(monkeypatch):
    import modal.exception

    def run_job(job):
        raise modal.exception.ConflictError(
            "We were unable to determine the version of Python installed in the Image"
        )

    monkeypatch.setattr(_cli, "run_job", run_job)

    result = CliRunner().invoke(main, ["run", "docker.io/ubuntu", "echo", "hi"])

    assert result.exit_code == 1
    assert "Could not find Python in docker.io/ubuntu" in result.output
    assert "--add-python" in result.output
