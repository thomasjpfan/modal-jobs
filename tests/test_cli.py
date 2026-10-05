import urllib.error

import pytest
from click.testing import CliRunner

from modal_jobs import _cli
from modal_jobs._cli import (
    JobSpec,
    build_job,
    format_job,
    is_local_path,
    main,
    parse_duration,
    parse_env_file,
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
    assert format_job(job) == "Function: python\nCommand: python -c 'print(1)'"


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
        "Function: echo\nCommand: echo hi\nImage: docker.io/ubuntu\nAdd Python: 3.12"
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
    assert format_job(job) == "Function: echo\nCommand: echo hi\nTimeout: 600s"


def test_format_job_retries():
    assert "Retries" not in format_job(build_job(["echo", "hi"]))
    job = build_job(["echo", "hi"], retries=3)
    assert format_job(job) == "Function: echo\nCommand: echo hi\nRetries: 3"


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
    assert "modal app logs ap-123" in result.output
    assert "modal app stop ap-123" in result.output
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
    assert result.output == "Function: echo\nCommand: echo hi\nDetach: yes\n"


def test_format_job_detach():
    assert "Detach" not in format_job(build_job(["echo", "hi"]))
