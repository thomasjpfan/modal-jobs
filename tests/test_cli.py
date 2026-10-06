import json
import time
import urllib.error

import pytest
from click.testing import CliRunner

from modal_jobs import _cli, _format
from modal_jobs._cli import (
    JobSpec,
    build_job,
    format_job,
    is_local_path,
    main,
    parse_duration,
    parse_env_file,
    parse_memory,
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
        name="job.py",
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
        name="python",
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


@pytest.mark.parametrize(
    "value, seconds",
    [
        ("90", 90),
        ("30s", 30),
        ("10m", 600),
        ("2h", 7200),
        ("1d", 86400),
        ("1h30m", 5400),
        ("1h0m5s", 3605),
    ],
)
def test_parse_duration(value, seconds):
    assert parse_duration(value) == seconds


@pytest.mark.parametrize(
    "value, match",
    [
        ("", "Expected a duration"),
        ("1x", "Expected a duration"),
        ("1.5h", "Expected a duration"),
        ("30m1h", "Expected a duration"),
        ("1h30", "Expected a duration"),
        ("-5", "Expected a duration"),
        ("0", "between 1 second and 24 hours"),
        ("0m", "between 1 second and 24 hours"),
        ("86401", "between 1 second and 24 hours"),
        ("1d1s", "between 1 second and 24 hours"),
    ],
)
def test_parse_duration_invalid(value, match):
    with pytest.raises(ValueError, match=match):
        parse_duration(value)


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


def test_parse_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        """\
# comment
FOO=bar

export EXPORTED=1
  SPACED = value with spaces  
DOUBLE="quoted # not a comment"
SINGLE='single'
INLINE=value # comment
EMPTY=
EQUALS=a=b
"""
    )
    assert parse_env_file(env_file) == [
        ("FOO", "bar"),
        ("EXPORTED", "1"),
        ("SPACED", "value with spaces"),
        ("DOUBLE", "quoted # not a comment"),
        ("SINGLE", "single"),
        ("INLINE", "value"),
        ("EMPTY", ""),
        ("EQUALS", "a=b"),
    ]


@pytest.mark.parametrize(
    "content, match",
    [
        ("FOO=bar\nNO_EQUALS\n", r":2: Expected KEY=VALUE"),
        ("BAD-NAME=value\n", r":1: Invalid environment variable name"),
    ],
)
def test_parse_env_file_invalid(tmp_path, content, match):
    env_file = tmp_path / ".env"
    env_file.write_text(content)
    with pytest.raises(ValueError, match=match):
        parse_env_file(env_file)


def test_build_job_local_secrets_last_wins():
    job = build_job(["echo"], secrets=(("A", "1"), ("B", "2"), ("A", "3")))
    assert job.local_secrets == (("A", "3"), ("B", "2"))


@pytest.mark.parametrize(
    "command, args",
    [(["uv", "run"], ["echo", "hi"]), (["run"], ["docker.io/ubuntu", "echo", "hi"])],
)
def test_run_env_file(tmp_path, monkeypatch, command, args):
    (tmp_path / "a.env").write_text("FOO=from-a\nBAR=from-a\n")
    (tmp_path / "b.env").write_text("BAR=from-b\nBAZ=from-b\n")
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(
        main,
        [
            *command,
            "--env-file",
            str(tmp_path / "a.env"),
            "--env-file",
            str(tmp_path / "b.env"),
            "-s",
            "BAZ=from-flag",
            *args,
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0].local_secrets == (("FOO", "from-a"), ("BAR", "from-b"), ("BAZ", "from-flag"))


def test_run_env_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(_cli, "run_job", lambda job: None)

    result = CliRunner().invoke(
        main, ["uv", "run", "--env-file", str(tmp_path / "missing.env"), "echo", "hi"]
    )

    assert result.exit_code == 2
    assert "does not exist" in result.output


def test_run_env_file_invalid(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("NO_EQUALS\n")
    monkeypatch.setattr(_cli, "run_job", lambda job: None)

    result = CliRunner().invoke(main, ["uv", "run", "--env-file", str(env_file), "echo", "hi"])

    assert result.exit_code == 2
    assert "Invalid value for '--env-file'" in result.output
    assert "Expected KEY=VALUE" in result.output


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


def test_run_timeout(tmp_path, monkeypatch):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(main, ["uv", "run", "--timeout", "1h", str(script)])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0].timeout == 3600


@pytest.mark.parametrize("command", [["uv", "run"], ["run"]])
@pytest.mark.parametrize("timeout", ["0", "86401", "1x"])
def test_run_timeout_invalid(command, timeout, monkeypatch):
    monkeypatch.setattr(_cli, "run_job", lambda job: None)
    if command == ["run"]:
        args = [*command, "--timeout", timeout, "docker.io/ubuntu", "ls"]
    else:
        args = [*command, "--timeout", timeout, "ls"]

    result = CliRunner().invoke(main, args)

    assert result.exit_code == 2
    assert "Invalid value for '--timeout'" in result.output


def test_run_retries(tmp_path, monkeypatch):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(main, ["uv", "run", "--retries", "3", str(script)])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0].retries == 3


@pytest.mark.parametrize("command", [["uv", "run"], ["run"]])
@pytest.mark.parametrize("retries", ["-1", "11", "x"])
def test_run_retries_invalid(command, retries, monkeypatch):
    monkeypatch.setattr(_cli, "run_job", lambda job: None)
    if command == ["run"]:
        args = [*command, "--retries", retries, "docker.io/ubuntu", "ls"]
    else:
        args = [*command, "--retries", retries, "ls"]

    result = CliRunner().invoke(main, args)

    assert result.exit_code == 2
    assert "Invalid value for '--retries'" in result.output


def test_build_job_with_image(tmp_path):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    job = build_job([str(script), "-x"], image="python:3.12-slim", add_python="3.12")
    assert job == JobSpec(
        function_name="job_py",
        name="python",
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
            "--timeout",
            "10m",
            "--retries",
            "2",
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
            name="ubuntu",
            command=["ls", "-v", "--gpu"],
            local_dirs=((tmp_path / "data", "/data"),),
            local_secrets=(("MY_SECRET", "value"),),
            gpu="T4",
            timeout=600,
            retries=2,
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


def test_format_job(tmp_path):
    script = tmp_path / "job.py"
    script.write_text(UV_SCRIPT)
    (tmp_path / "data").mkdir()
    job = build_job(
        [str(script), "--name", "a b"],
        ("pkg[a,b]",),
        volumes=(("my-modal-volume", "/mnt/vol"), (tmp_path / "data", "/root/data")),
        secrets=("my-modal-secret", ("MY_SECRET", "hunter2")),
        gpu="T4",
    )
    assert format_job(job) == "\n".join(
        [
            "Name: job.py",
            "Function: job",
            "Command: python /root/job.py --name 'a b'",
            f"Script: {script} -> /root/job.py",
            "Dependencies: requests<3, rich, pkg[a,b]",
            "GPU: T4",
            "Volume: my-modal-volume -> /mnt/vol",
            f"Local directory: {tmp_path / 'data'} -> /root/data",
            "Secret: my-modal-secret",
            "Local secret: MY_SECRET=***",
        ]
    )


def test_format_job_command():
    job = build_job(["python", "-c", "print(1)"])
    assert format_job(job) == "Name: python\nFunction: python\nCommand: python -c 'print(1)'"


def test_run_dry_run(tmp_path, monkeypatch):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(
        main, ["uv", "run", "--dry-run", "-s", "MY_SECRET=hunter2", str(script)]
    )

    assert result.exit_code == 0, result.output
    assert calls == []
    assert (
        result.output
        == format_job(build_job([str(script)], secrets=(("MY_SECRET", "hunter2"),))) + "\n"
    )
    assert "hunter2" not in result.output


def test_format_job_image():
    job = build_job(["echo", "hi"], image="docker.io/ubuntu", add_python="3.12")
    assert format_job(job) == (
        "Name: ubuntu\nFunction: echo\nCommand: echo hi\nImage: docker.io/ubuntu\nAdd Python: 3.12"
    )


def test_docker_run_dry_run(monkeypatch):
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(
        main, ["run", "--dry-run", "--add-python", "3.12", "docker.io/ubuntu", "echo", "hi"]
    )

    assert result.exit_code == 0, result.output
    assert calls == []
    assert (
        result.output
        == format_job(build_job(["echo", "hi"], image="docker.io/ubuntu", add_python="3.12")) + "\n"
    )


def test_format_job_timeout():
    job = build_job(["echo", "hi"], timeout=600)
    assert format_job(job) == "Name: echo\nFunction: echo\nCommand: echo hi\nTimeout: 600s"


def test_format_job_retries():
    assert "Retries" not in format_job(build_job(["echo", "hi"]))
    job = build_job(["echo", "hi"], retries=3)
    assert format_job(job) == "Name: echo\nFunction: echo\nCommand: echo hi\nRetries: 3"


def test_run_url(monkeypatch):
    urls = []
    calls = []
    monkeypatch.setattr(_cli, "fetch_script", lambda url: urls.append(url) or UV_SCRIPT)
    monkeypatch.setattr(_cli, "run_job", calls.append)
    url = "https://example.com/jobs/hello_gpu.py"

    result = CliRunner().invoke(main, ["uv", "run", "--gpu", "T4", url, "--n", "3"])

    assert result.exit_code == 0, result.output
    assert urls == [url]
    assert len(calls) == 1
    assert calls[0].function_name == "hello_gpu"
    assert calls[0].command == ["python", "/root/hello_gpu.py", "--n", "3"]
    assert calls[0].dependencies == ("requests<3", "rich")
    assert calls[0].gpu == "T4"
    assert "Finished running hello_gpu.py" in result.output


def test_run_url_download_error(monkeypatch):
    def fetch_script(url):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(_cli, "fetch_script", fetch_script)
    monkeypatch.setattr(_cli, "run_job", lambda job: None)

    result = CliRunner().invoke(main, ["uv", "run", "https://example.com/job.py"])

    assert result.exit_code == 1
    assert "Could not download https://example.com/job.py" in result.output


def test_run_url_not_script(monkeypatch):
    monkeypatch.setattr(_cli, "run_job", lambda job: None)

    result = CliRunner().invoke(main, ["uv", "run", "https://example.com/job.txt"])

    assert result.exit_code == 2
    assert "must point to a .py script" in result.output


@pytest.mark.parametrize("flag", ["--detach", "-d"])
@pytest.mark.parametrize(
    ("command", "args"),
    [(["uv", "run"], []), (["run"], ["--add-python", "3.12", "docker.io/ubuntu"])],
)
def test_run_detach(monkeypatch, flag, command, args):
    jobs = []

    def run_job(job):
        jobs.append(job)
        return "ap-123"

    monkeypatch.setattr(_cli, "run_job", run_job)

    result = CliRunner().invoke(main, [*command, flag, *args, "echo", "hi"])

    assert result.exit_code == 0, result.output
    assert len(jobs) == 1
    assert jobs[0].detach
    assert "Started echo hi in the background" in result.output
    assert "modal-jobs logs --follow ap-123" in result.output
    assert "modal-jobs wait ap-123" in result.output
    assert "modal-jobs stop ap-123" in result.output
    assert "Finished" not in result.output


def test_run_interrupted(monkeypatch):
    jobs = []

    def run_job(job):
        jobs.append(job)
        return "ap-123"

    monkeypatch.setattr(_cli, "run_job", run_job)

    result = CliRunner().invoke(main, ["uv", "run", "echo", "hi"])

    assert result.exit_code == 0, result.output
    assert not jobs[0].detach
    assert "Finished" not in result.output
    assert "Started" not in result.output


def test_run_detach_dry_run(monkeypatch):
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(main, ["uv", "run", "--detach", "--dry-run", "echo", "hi"])

    assert result.exit_code == 0, result.output
    assert calls == []
    assert result.output == "Name: echo\nFunction: echo\nCommand: echo hi\nDetach: yes\n"


def test_format_job_detach():
    assert "Detach" not in format_job(build_job(["echo", "hi"]))


def test_run_stdin(monkeypatch):
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(
        main, ["uv", "run", "--gpu", "T4", "-", "--n", "3"], input=UV_SCRIPT
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0].function_name == "stdin"
    assert calls[0].command == ["python", "/root/stdin.py", "--n", "3"]
    assert calls[0].dependencies == ("requests<3", "rich")
    assert calls[0].gpu == "T4"
    assert "Finished running stdin.py" in result.output


def test_run_stdin_empty(monkeypatch):
    monkeypatch.setattr(_cli, "run_job", lambda job: None)

    result = CliRunner().invoke(main, ["uv", "run", "-"], input="")

    assert result.exit_code == 2
    assert "No script provided on stdin" in result.output


def test_run_stdin_dry_run():
    result = CliRunner().invoke(main, ["uv", "run", "--dry-run", "-", "a"], input=UV_SCRIPT)

    assert result.exit_code == 0, result.output
    assert "Function: stdin\n" in result.output
    assert "Command: python /root/stdin.py a\n" in result.output
    assert "Dependencies: requests<3, rich\n" in result.output


@pytest.mark.parametrize(
    "value, expected",
    [("512", 512), ("512M", 512), ("512Mi", 512), ("4G", 4096), ("4gb", 4096), ("2Gi", 2048)],
)
def test_parse_memory(value, expected):
    assert parse_memory(value) == expected


@pytest.mark.parametrize("value", ["", "0", "0G", "4T", "1.5G", "-1", "G"])
def test_parse_memory_invalid(value):
    with pytest.raises(ValueError):
        parse_memory(value)


def test_run_cpu_memory(tmp_path, monkeypatch):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(main, ["uv", "run", "--cpu", "2.5", "--memory", "4G", str(script)])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0].cpu == 2.5
    assert calls[0].memory == 4096


def test_docker_run_cpu_memory(monkeypatch):
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(
        main, ["run", "--cpu", "4", "--memory", "512", "docker.io/ubuntu", "echo", "hi"]
    )

    assert result.exit_code == 0, result.output
    assert calls[0].cpu == 4
    assert calls[0].memory == 512


@pytest.mark.parametrize(
    "args, message",
    [(["--cpu", "0"], "--cpu"), (["--memory", "lots"], "Expected a memory size")],
)
def test_run_cpu_memory_invalid(monkeypatch, args, message):
    monkeypatch.setattr(_cli, "run_job", lambda job: None)

    result = CliRunner().invoke(main, ["uv", "run", *args, "echo", "hi"])

    assert result.exit_code == 2
    assert message in result.output


def test_format_job_cpu_memory():
    job = build_job(["echo", "hi"], cpu=0.5, memory=2048)
    assert (
        format_job(job)
        == "Name: echo\nFunction: echo\nCommand: echo hi\nCPU: 0.5\nMemory: 2048 MiB"
    )


def test_job_record_masks_local_secrets(tmp_path):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    job = build_job([str(script)], secrets=("my-secret", ("TOKEN", "hunter2")), gpu="T4")

    record = _cli.job_record(job, "ap-123", "fc-456")

    assert record["id"] == "ap-123"
    assert record["call_id"] == "fc-456"
    assert record["name"] == "job.py"
    assert record["status"] == "running"
    assert record["gpu"] == "T4"
    assert record["secrets"] == ["my-secret"]
    assert record["local_secret_keys"] == ["TOKEN"]
    assert "hunter2" not in json.dumps(record)


@pytest.mark.parametrize(
    "image, name",
    [
        ("ubuntu", "ubuntu"),
        ("python:3.12-slim", "python"),
        ("docker.io/library/python:3.12-slim", "python"),
        ("localhost:5000/team/trainer", "trainer"),
        ("localhost:5000/team/trainer:v2", "trainer"),
        ("ubuntu@sha256:abc123", "ubuntu"),
        ("ghcr.io/org/app:1.0@sha256:abc123", "app"),
    ],
)
def test_image_name(image, name):
    assert _cli.image_name(image) == name


def test_build_job_name_command():
    assert build_job(["/usr/bin/python3.12", "-V"]).name == "python3.12"


def test_build_job_name_image():
    assert build_job(["echo", "hi"], image="docker.io/library/python:3.12").name == "python"


def test_build_job_name_override(tmp_path):
    script = tmp_path / "job.py"
    script.write_text("print('hi')")
    assert build_job([str(script)], name="exp1").name == "exp1"
    assert build_job(["echo", "hi"], image="ubuntu", name="exp1").name == "exp1"


def test_job_record_name():
    job = build_job(["echo", "hi"], image="docker.io/ubuntu")
    assert _cli.job_record(job, "ap-1", "fc-1")["name"] == "ubuntu"


@pytest.mark.parametrize(
    "args",
    [
        ["run", "--name", "exp1", "docker.io/ubuntu", "echo", "hi"],
        ["uv", "run", "--name", "exp1", "echo", "hi"],
    ],
)
def test_run_name(args, monkeypatch):
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(main, args)

    assert result.exit_code == 0, result.output
    assert calls[0].name == "exp1"
    assert calls[0].command == ["echo", "hi"]


def test_run_empty_name(monkeypatch):
    monkeypatch.setattr(_cli, "run_job", lambda job: None)

    result = CliRunner().invoke(main, ["uv", "run", "--name", "", "echo", "hi"])

    assert result.exit_code == 2
    assert "Name must not be empty" in result.output


def test_parse_label():
    assert _cli.parse_label("team=ml") == ("team", "ml")
    assert _cli.parse_label("url=a=b") == ("url", "a=b")
    assert _cli.parse_label("draft") == ("draft", "")


@pytest.mark.parametrize("value", ["", "=ml", " =ml"])
def test_parse_label_empty_key(value):
    with pytest.raises(ValueError, match="Label key must not be empty"):
        _cli.parse_label(value)


def test_build_job_labels_last_wins():
    job = build_job(["echo", "hi"], labels=(("team", "ml"), ("exp", "1"), ("team", "infra")))
    assert job.labels == (("team", "infra"), ("exp", "1"))


def test_format_job_labels():
    job = build_job(["echo", "hi"], labels=(("team", "ml"), ("draft", "")))
    assert format_job(job).endswith("\nLabel: team=ml\nLabel: draft=")


def test_job_record_labels():
    job = build_job(["echo", "hi"], labels=(("team", "ml"),))
    assert _cli.job_record(job, "ap-1", "fc-1")["labels"] == {"team": "ml"}


@pytest.mark.parametrize(
    "args",
    [
        ["run", "-l", "team=ml", "--label", "draft", "docker.io/ubuntu", "echo", "hi"],
        ["uv", "run", "-l", "team=ml", "--label", "draft", "echo", "hi"],
    ],
)
def test_run_labels(args, monkeypatch):
    calls = []
    monkeypatch.setattr(_cli, "run_job", calls.append)

    result = CliRunner().invoke(main, args)

    assert result.exit_code == 0, result.output
    assert calls[0].labels == (("team", "ml"), ("draft", ""))
    assert calls[0].command == ["echo", "hi"]


def test_run_empty_label_key(monkeypatch):
    monkeypatch.setattr(_cli, "run_job", lambda job: None)

    result = CliRunner().invoke(main, ["uv", "run", "--label", "=ml", "echo", "hi"])

    assert result.exit_code == 2
    assert "Label key must not be empty" in result.output


def test_register_job_warns_on_failure(monkeypatch, capsys):
    from modal_jobs import _backend

    def missing_registry():
        raise RuntimeError("not deployed")

    monkeypatch.setattr(_backend, "registry", missing_registry)

    _cli.register_job(build_job(["echo", "hi"]), "ap-1", "fc-1")

    assert "modal-jobs backend deploy" in capsys.readouterr().out


class FakeMethod:
    def __init__(self, fn):
        self.remote = fn


class FakeRegistry:
    """A stand-in for the deployed `Registry`, backed by a local `JobStore`."""

    def __init__(self, store):
        self.list_jobs = FakeMethod(
            lambda limit, status, name=None, labels=None: store.list(limit, status, name, labels)
        )
        self.get_job = FakeMethod(store.get)
        self.stop_job = FakeMethod(lambda job_id, stopped_by: fake_stop_job(store, job_id))
        self.delete_jobs = FakeMethod(lambda ids: fake_delete_jobs(store, ids))


def fake_stop_job(store, job_id):
    record = store.get(job_id)
    if record["status"] == "running":
        record = {**record, "status": "stopped", "finished_at": time.time()}
        store.put(record)
    return record


def fake_delete_jobs(store, ids):
    records = [store.get(job_id) for job_id in ids]
    if any(record["status"] == "running" for record in records):
        raise ValueError("Jobs still running")
    for record in records:
        store.delete(record["id"])
    return [record["id"] for record in records]


def make_record(job_id, status="running", **fields):
    return {
        "id": job_id,
        "call_id": f"fc-{job_id}",
        "name": "job.py",
        "command": ["python", "/root/job.py"],
        "gpu": None,
        "submitted_by": "me@host",
        "submitted_at": time.time() - 90,
        "started_at": None,
        "finished_at": None,
        "status": status,
        "exit_code": None,
        "error": None,
        **fields,
    }


@pytest.fixture
def store(tmp_path, monkeypatch):
    from modal_jobs import _backend
    from modal_jobs._store import JobStore

    store = JobStore(tmp_path)
    monkeypatch.setattr(_backend, "registry", lambda: FakeRegistry(store))
    return store


def test_ls(store):
    store.put(make_record("ap-running", gpu="T4"))
    store.put(
        make_record(
            "ap-failed",
            status="failed",
            exit_code=3,
            started_at=1.0,
            finished_at=66.0,
            submitted_at=0.0,
        )
    )

    result = CliRunner().invoke(main, ["ls"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "ID" in lines[0] and "STATUS" in lines[0]
    assert "ap-running" in lines[1] and "running" in lines[1] and "T4" in lines[1]
    assert "ap-failed" in lines[2] and "failed" in lines[2] and "1m5s" in lines[2]


def test_ls_status(store):
    store.put(make_record("ap-running"))
    store.put(make_record("ap-failed", status="failed"))

    result = CliRunner().invoke(main, ["ls", "--status", "failed"])

    assert result.exit_code == 0, result.output
    assert "ap-failed" in result.output
    assert "ap-running" not in result.output


def test_ls_name(store):
    store.put(make_record("ap-train", name="train.py"))
    store.put(make_record("ap-eval", name="eval.py"))
    store.put(make_record("ap-train2", name="train.py2"))

    result = CliRunner().invoke(main, ["ls", "--name", "train.py"])

    assert result.exit_code == 0, result.output
    assert "ap-train " in result.output
    assert "ap-eval" not in result.output
    assert "ap-train2" not in result.output


def test_ls_label(store):
    store.put(make_record("ap-ml1", labels={"team": "ml", "exp": "1"}))
    store.put(make_record("ap-ml2", labels={"team": "ml", "exp": "2"}))
    store.put(make_record("ap-infra", labels={"team": "infra"}))
    store.put(make_record("ap-unlabeled"))

    def ls_ids(*args):
        result = CliRunner().invoke(main, ["ls", "--json", *args])
        assert result.exit_code == 0, result.output
        return sorted(record["id"] for record in json.loads(result.output))

    assert ls_ids("--label", "team=ml") == ["ap-ml1", "ap-ml2"]
    assert ls_ids("--label", "exp") == ["ap-ml1", "ap-ml2"]
    assert ls_ids("--label", "team=ml", "--label", "exp=2") == ["ap-ml2"]
    assert ls_ids("--label", "team=other") == []


def test_ls_labels_column(store):
    store.put(make_record("ap-labeled", labels={"team": "ml", "exp": "3"}))
    store.put(make_record("ap-unlabeled", submitted_at=1.0))

    result = CliRunner().invoke(main, ["ls"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "LABELS" in lines[0]
    assert "ap-labeled" in lines[1] and lines[1].rstrip().endswith("team=ml,exp=3")
    assert "ap-unlabeled" in lines[2] and lines[2].rstrip().endswith("-")


def test_show_labels(store):
    store.put(make_record("ap-abc", labels={"team": "ml"}))

    result = CliRunner().invoke(main, ["show", "ap-abc"])

    assert result.exit_code == 0, result.output
    assert "Label: team=ml" in result.output


def test_ls_empty(store):
    result = CliRunner().invoke(main, ["ls"])
    assert result.exit_code == 0, result.output
    assert "No jobs found." in result.output


def test_show(store):
    store.put(
        make_record(
            "ap-abc", status="failed", exit_code=3, local_secret_keys=["TOKEN"], secrets=["s"]
        )
    )

    result = CliRunner().invoke(main, ["show", "ap-a"])

    assert result.exit_code == 0, result.output
    assert "ID: ap-abc" in result.output
    assert "Status: failed" in result.output
    assert "Exit code: 3" in result.output
    assert "Local secret: TOKEN=***" in result.output
    assert "modal app logs ap-abc" in result.output
    assert "modal app stop" not in result.output


@pytest.mark.parametrize("job_id, message", [("ap-x", "No job found"), ("ap-", "ambiguous")])
def test_show_errors(store, job_id, message):
    store.put(make_record("ap-abc"))
    store.put(make_record("ap-abd"))

    result = CliRunner().invoke(main, ["show", job_id])

    assert result.exit_code == 1
    assert message in result.output


def test_ls_backend_not_deployed(monkeypatch):
    import modal.exception

    from modal_jobs import _backend

    def missing_registry():
        raise modal.exception.NotFoundError("App not found")

    monkeypatch.setattr(_backend, "registry", missing_registry)

    result = CliRunner().invoke(main, ["ls"])

    assert result.exit_code == 1
    assert "modal-jobs backend deploy" in result.output


@pytest.mark.parametrize(
    "seconds, expected", [(45, "45s"), (192, "3m12s"), (7500, "2h5m"), (97200, "1d3h")]
)
def test_format_duration(seconds, expected):
    assert _cli.format_duration(seconds) == expected


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"exit_code": 3}, "3"),
        ({"exit_code": -11}, "-11 (killed by SIGSEGV)"),
        ({"exit_code": -9}, "-9 (killed by SIGKILL, possibly out of memory)"),
        ({"exit_code": -200}, "-200 (killed by signal 200)"),
    ],
)
def test_format_exit_code(fields, expected):
    assert _format.format_exit_code(fields) == expected


def test_usage_fields():
    record = make_record(
        "ap-1",
        status="succeeded",
        started_at=0.0,
        finished_at=100.0,
        cpu_seconds=150.0,
        cpu=4,
        task_id="ta-1",
        region="us-west",
        cloud="aws",
    )
    assert _format.usage_fields(record) == [
        ("CPU time", "2m30s (1.5 cores on average of 4)"),
        ("Container", "ta-1 (us-west, aws)"),
    ]


def test_usage_fields_without_stats():
    assert _format.usage_fields(make_record("ap-1")) == []


def test_parse_volume_logs_dir_reserved():
    with pytest.raises(ValueError, match="reserved"):
        parse_volume(f"my-volume:{_cli.LOGS_DIR}/")


def test_job_record_log():
    assert _cli.job_record(build_job(["echo", "hi"]), "ap-123", "fc-456")["log"] == "ap-123.log"


@pytest.fixture
def saved_logs(monkeypatch):
    from modal_jobs import _backend

    logs = {}

    def read_log(job_id):
        if job_id not in logs:
            raise FileNotFoundError(job_id)
        return logs[job_id]

    monkeypatch.setattr(_backend, "read_log", read_log)
    return logs


def test_logs(store, saved_logs):
    store.put(make_record("ap-abc", log="ap-abc.log"))
    saved_logs["ap-abc"] = b"hello\n"

    result = CliRunner().invoke(main, ["logs", "ap-a"])

    assert result.exit_code == 0, result.output
    assert result.output == "hello\n"


def test_logs_not_saved_yet(store, saved_logs):
    store.put(make_record("ap-abc", log="ap-abc.log"))

    result = CliRunner().invoke(main, ["logs", "ap-abc"])

    assert result.exit_code == 1
    assert "No output was saved" in result.output
    assert "modal-jobs logs --follow ap-abc" in result.output


def test_logs_before_saved_logs(store, saved_logs):
    store.put(make_record("ap-abc"))

    result = CliRunner().invoke(main, ["logs", "ap-abc"])

    assert result.exit_code == 1
    assert "before logs were saved" in result.output


def test_logs_unknown_job(store, saved_logs):
    result = CliRunner().invoke(main, ["logs", "ap-x"])

    assert result.exit_code == 1
    assert "No job found" in result.output


def test_show_saved_logs_hint(store):
    store.put(make_record("ap-abc", log="ap-abc.log"))

    result = CliRunner().invoke(main, ["show", "ap-abc"])

    assert result.exit_code == 0, result.output
    assert "modal-jobs logs ap-abc" in result.output


def test_show_running_hints(store):
    store.put(make_record("ap-abc", log="ap-abc.log"))

    result = CliRunner().invoke(main, ["show", "ap-abc"])

    assert result.exit_code == 0, result.output
    assert "modal-jobs logs --follow ap-abc" in result.output
    assert "modal-jobs logs ap-abc" in result.output
    assert "modal-jobs stop ap-abc" in result.output


def test_show_json(store):
    record = make_record("ap-abc", status="succeeded", exit_code=0)
    store.put(record)

    result = CliRunner().invoke(main, ["show", "ap-a", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == record


def test_ls_json(store):
    store.put(make_record("ap-old", submitted_at=time.time() - 200))
    store.put(make_record("ap-new"))

    result = CliRunner().invoke(main, ["ls", "--json"])

    assert result.exit_code == 0, result.output
    assert [record["id"] for record in json.loads(result.output)] == ["ap-new", "ap-old"]


def test_ls_json_empty(store):
    result = CliRunner().invoke(main, ["ls", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_logs_tail(store, saved_logs):
    store.put(make_record("ap-abc", status="succeeded", log="ap-abc.log"))
    saved_logs["ap-abc"] = b"one\ntwo\nthree\n"

    result = CliRunner().invoke(main, ["logs", "--tail", "2", "ap-abc"])

    assert result.exit_code == 0, result.output
    assert result.output == "two\nthree\n"


def test_logs_follow_running(store, saved_logs, monkeypatch):
    streamed = []
    monkeypatch.setattr(_cli, "stream_logs", streamed.append)
    store.put(make_record("ap-abc", log="ap-abc.log"))

    result = CliRunner().invoke(main, ["logs", "-f", "ap-a"])

    assert result.exit_code == 0, result.output
    assert streamed == ["ap-abc"]


def test_logs_follow_finished(store, saved_logs, monkeypatch):
    streamed = []
    monkeypatch.setattr(_cli, "stream_logs", streamed.append)
    store.put(make_record("ap-abc", status="succeeded", log="ap-abc.log"))
    saved_logs["ap-abc"] = b"done\n"

    result = CliRunner().invoke(main, ["logs", "--follow", "ap-abc"])

    assert result.exit_code == 0, result.output
    assert streamed == []
    assert result.output == "done\n"


def test_logs_follow_and_tail(store, saved_logs):
    result = CliRunner().invoke(main, ["logs", "-f", "-n", "2", "ap-abc"])

    assert result.exit_code == 2
    assert "can't be used together" in result.output


@pytest.fixture
def stopped_apps(monkeypatch):
    apps = []
    monkeypatch.setattr(_cli, "stop_app", apps.append)
    return apps


def test_stop(store, stopped_apps):
    store.put(make_record("ap-abc"))

    result = CliRunner().invoke(main, ["stop", "ap-a"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Stop ap-abc?" in result.output
    assert "Stopped ap-abc" in result.output
    assert store.get("ap-abc")["status"] == "stopped"
    assert stopped_apps == ["ap-abc"]


def test_stop_aborted(store, stopped_apps):
    store.put(make_record("ap-abc"))

    result = CliRunner().invoke(main, ["stop", "ap-abc"], input="n\n")

    assert result.exit_code == 1
    assert store.get("ap-abc")["status"] == "running"
    assert stopped_apps == []


def test_stop_already_finished(store, stopped_apps):
    store.put(make_record("ap-abc", status="succeeded"))
    store.put(make_record("ap-def"))

    result = CliRunner().invoke(main, ["stop", "--yes", "ap-abc", "ap-def"])

    assert result.exit_code == 0, result.output
    assert "ap-abc already succeeded" in result.output
    assert "Stopped ap-def" in result.output
    assert store.get("ap-abc")["status"] == "succeeded"
    assert stopped_apps == ["ap-def"]


def test_stop_old_backend(store, stopped_apps, monkeypatch):
    from modal_jobs import _backend

    registry = _backend.registry()
    del registry.stop_job
    monkeypatch.setattr(_backend, "registry", lambda: registry)
    store.put(make_record("ap-abc"))

    result = CliRunner().invoke(main, ["stop", "-y", "ap-abc"])

    assert result.exit_code == 1
    assert "modal-jobs backend deploy" in result.output
    assert stopped_apps == []


@pytest.mark.parametrize(
    "fields, exit_code",
    [
        ({"status": "succeeded", "exit_code": 0}, 0),
        ({"status": "failed", "exit_code": 3}, 3),
        ({"status": "failed", "exit_code": -9}, 1),
        ({"status": "failed", "error": "boom"}, 1),
        ({"status": "timed_out"}, 1),
        ({"status": "stopped"}, 1),
        ({"status": "unknown"}, 1),
    ],
)
def test_wait_finished(store, fields, exit_code):
    store.put(make_record("ap-abc", **fields))

    result = CliRunner().invoke(main, ["wait", "ap-a"])

    assert result.exit_code == exit_code, result.output
    assert f"Job ap-abc {fields['status']}" in result.output


def test_wait_polls_until_finished(store, monkeypatch):
    store.put(make_record("ap-abc"))
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            store.put(make_record("ap-abc", status="succeeded", exit_code=0))

    monkeypatch.setattr(_cli.time, "sleep", sleep)

    result = CliRunner().invoke(main, ["wait", "--interval", "2", "ap-abc"])

    assert result.exit_code == 0, result.output
    assert sleeps == [2, 2]
    assert "Job ap-abc succeeded" in result.output


def test_wait_timeout(store, monkeypatch):
    store.put(make_record("ap-abc"))
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(_cli.time, "sleep", sleep)
    monkeypatch.setattr(_cli.time, "monotonic", lambda: now[0])

    result = CliRunner().invoke(main, ["wait", "--timeout", "12", "ap-abc"])

    assert result.exit_code == 124
    assert "Timed out waiting for ap-abc" in result.output
    assert now[0] == 12


@pytest.fixture
def deleted_logs(monkeypatch):
    from modal_jobs import _backend

    deleted = []
    monkeypatch.setattr(_backend, "delete_logs", deleted.extend)
    return deleted


def test_rm(store, deleted_logs):
    store.put(make_record("ap-abc", status="succeeded"))
    store.put(make_record("ap-def", status="failed"))
    store.put(make_record("ap-xyz", status="failed"))

    result = CliRunner().invoke(main, ["rm", "ap-a", "ap-def", "ap-abc"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Delete ap-abc, ap-def?" in result.output
    assert "Deleted 2 jobs" in result.output
    assert [record["id"] for record in store.list()] == ["ap-xyz"]
    assert deleted_logs == ["ap-abc", "ap-def"]


def test_rm_running(store, deleted_logs):
    store.put(make_record("ap-abc"))

    result = CliRunner().invoke(main, ["rm", "-y", "ap-abc"])

    assert result.exit_code == 1
    assert "modal-jobs stop" in result.output
    assert store.get("ap-abc")["status"] == "running"
    assert deleted_logs == []


def test_prune(store, deleted_logs):
    day = 86400
    now = time.time()
    store.put(make_record("ap-old", status="succeeded", submitted_at=now - 8 * day))
    store.put(make_record("ap-old-failed", status="failed", submitted_at=now - 9 * day))
    store.put(make_record("ap-old-running", submitted_at=now - 8 * day))
    store.put(make_record("ap-new", status="succeeded", submitted_at=now - day))

    result = CliRunner().invoke(main, ["prune", "-y"])

    assert result.exit_code == 0, result.output
    assert "Deleted 2 jobs" in result.output
    remaining = {record["id"] for record in store.list()}
    assert remaining == {"ap-old-running", "ap-new"}
    assert sorted(deleted_logs) == ["ap-old", "ap-old-failed"]


def test_prune_status_and_older_than(store, deleted_logs):
    now = time.time()
    store.put(make_record("ap-a", status="succeeded", submitted_at=now - 7200))
    store.put(make_record("ap-b", status="failed", submitted_at=now - 7200))
    store.put(make_record("ap-c", status="failed", submitted_at=now - 60))

    result = CliRunner().invoke(
        main, ["prune", "--older-than", "1h", "--status", "failed"], input="y\n"
    )

    assert result.exit_code == 0, result.output
    assert "Delete 1 job?" in result.output
    assert deleted_logs == ["ap-b"]


def test_prune_dry_run(store, deleted_logs):
    store.put(make_record("ap-old", status="succeeded", submitted_at=time.time() - 30 * 86400))

    result = CliRunner().invoke(main, ["prune", "--older-than", "30d", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "ap-old" in result.output
    assert store.get("ap-old")["status"] == "succeeded"
    assert deleted_logs == []


def test_prune_nothing(store, deleted_logs):
    result = CliRunner().invoke(main, ["prune"])

    assert result.exit_code == 0, result.output
    assert "No jobs to delete" in result.output


def test_prune_rejects_running_status(store):
    result = CliRunner().invoke(main, ["prune", "--status", "running"])

    assert result.exit_code == 2


def test_parse_duration_unbounded():
    assert parse_duration("30d", bounded=False) == 30 * 86400
    with pytest.raises(ValueError, match="at least 1 second"):
        parse_duration("0", bounded=False)


def test_backend_status(store, monkeypatch):
    from modal_jobs import _backend

    monkeypatch.setattr(_backend, "logs_usage", lambda: (3, 3 * 1024 * 1024))
    monkeypatch.setattr(
        _backend, "dashboard_url", lambda: "https://me--modal-jobs-dashboard.modal.run"
    )
    store.put(make_record("ap-a"))
    store.put(make_record("ap-b", status="succeeded"))
    store.put(make_record("ap-c", status="succeeded"))

    result = CliRunner().invoke(main, ["backend", "status"])

    assert result.exit_code == 0, result.output
    assert "Backend: deployed" in result.output
    assert "Jobs: 3 (1 running, 2 succeeded)" in result.output
    assert "Saved logs: 3 files, 3M" in result.output
    assert "Dashboard: https://me--modal-jobs-dashboard.modal.run" in result.output


def test_backend_status_without_dashboard(store, monkeypatch):
    import modal.exception

    from modal_jobs import _backend

    def missing_dashboard():
        raise modal.exception.NotFoundError("missing")

    monkeypatch.setattr(_backend, "logs_usage", lambda: (0, 0))
    monkeypatch.setattr(_backend, "dashboard_url", missing_dashboard)

    result = CliRunner().invoke(main, ["backend", "status"])

    assert result.exit_code == 0, result.output
    assert "Dashboard: not deployed" in result.output


@pytest.mark.parametrize(
    "size, expected",
    [(0, "0B"), (1023, "1023B"), (2048, "2K"), (5 * 1024 * 1024, "5M"), (3 * 1024**3, "3.0G")],
)
def test_format_bytes(size, expected):
    assert _cli.format_bytes(size) == expected


def test_backend_status_not_deployed(monkeypatch):
    import modal.exception

    from modal_jobs import _backend

    def missing_registry():
        raise modal.exception.NotFoundError("missing")

    monkeypatch.setattr(_backend, "registry", missing_registry)

    result = CliRunner().invoke(main, ["backend", "status"])

    assert result.exit_code == 1
    assert "modal-jobs backend deploy" in result.output


def test_delete_logs_skips_missing(monkeypatch):
    import modal
    import modal.exception

    from modal_jobs import _backend

    class FakeVolume:
        def __init__(self):
            self.files = {"ap-a.log", "ap-b.log"}

        def listdir(self, path):
            return [type("Entry", (), {"path": name}) for name in self.files]

        def remove_file(self, path):
            if path not in self.files:
                raise modal.exception.InvalidError("No such file or directory.")
            self.files.remove(path)

    volume = FakeVolume()
    monkeypatch.setattr(modal.Volume, "from_name", lambda *args, **kwargs: volume)

    _backend.delete_logs(["ap-a", "ap-missing"])

    assert volume.files == {"ap-b.log"}


@pytest.mark.parametrize(
    "url, expected", [("https://dash", "[link=https://dash/jobs/ap-a]ap-a[/link]"), (None, "ap-a")]
)
def test_job_link(url, expected, monkeypatch):
    monkeypatch.setattr(_cli, "dashboard_url", lambda: url)

    assert _cli.job_link("ap-a") == expected


@pytest.fixture
def clear_dashboard_url():
    _cli.dashboard_url.cache_clear()
    yield
    _cli.dashboard_url.cache_clear()


@pytest.mark.parametrize("is_terminal, expected", [(True, "https://dash"), (False, None)])
def test_dashboard_url(is_terminal, expected, monkeypatch, clear_dashboard_url):
    from modal_jobs import _backend

    monkeypatch.setattr(_cli.console, "_force_terminal", is_terminal)
    monkeypatch.setattr(_backend, "dashboard_url", lambda: "https://dash/")

    assert _cli.dashboard_url() == expected


def test_dashboard_url_without_dashboard(monkeypatch, clear_dashboard_url):
    import modal.exception

    from modal_jobs import _backend

    def missing_dashboard():
        raise modal.exception.NotFoundError("missing")

    monkeypatch.setattr(_cli.console, "_force_terminal", True)
    monkeypatch.setattr(_backend, "dashboard_url", missing_dashboard)

    assert _cli.dashboard_url() is None
