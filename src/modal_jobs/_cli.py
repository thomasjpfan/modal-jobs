import getpass
import json
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import click
from rich.console import Console
from rich.table import Table

from modal_jobs._runner import run_cmd

console = Console()

# Reference regex from https://packaging.python.org/en/latest/specifications/inline-script-metadata/
SCRIPT_METADATA_RE = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)


@dataclass(frozen=True)
class JobSpec:
    function_name: str
    # Name to find the job by: the image's or script's name, or the one the user gave.
    name: str
    command: list[str]
    # Script to upload and its path in the container, or None for plain commands.
    local_path: Path | None = None
    remote_path: str | None = None
    dependencies: tuple[str, ...] = ()
    # (volume name, remote path) pairs of Modal volumes to mount.
    volumes: tuple[tuple[str, str], ...] = ()
    # (local directory, remote path) pairs to add to the image.
    local_dirs: tuple[tuple[Path, str], ...] = ()
    # Names of Modal secrets to inject into the container's environment.
    secrets: tuple[str, ...] = ()
    # (name, value) pairs of local secrets to inject into the container's environment.
    local_secrets: tuple[tuple[str, str], ...] = ()
    # GPU type to request, e.g. `T4` or `H100:8`, passed to Modal as is.
    gpu: str | None = None
    # Number of CPU cores to request, or None for Modal's default.
    cpu: float | None = None
    # Memory to request in MiB, or None for Modal's default.
    memory: int | None = None
    # Maximum run time in seconds, or None for Modal's default.
    timeout: int | None = None
    # Number of times Modal retries the job after it fails.
    retries: int = 0
    # Registry image to run in, e.g. `docker.io/ubuntu`, or None for `debian_slim`.
    image: str | None = None
    # Python version to add to `image`, for images without Python.
    add_python: str | None = None
    # Return right after starting the job instead of waiting for it to finish.
    detach: bool = False


# Where the logs volume is mounted in the job's container, so users can't mount anything there.
LOGS_DIR = "/modal-jobs-logs"


def is_local_path(source: str) -> bool:
    """Return True if a volume source is a local path rather than a Modal volume name.

    Like `docker run -v`, sources starting with `.` or `~` or containing `/` are paths.
    """
    return source.startswith((".", "~")) or "/" in source


def parse_volume(value: str, cwd: Path | None = None) -> tuple[str | Path, str]:
    """Parse a `SOURCE:DEST` volume spec.

    Returns `(source, dest)` where `source` is a resolved `Path` for a local
    directory or a `str` for a Modal volume name.
    """
    source, sep, dest = value.rpartition(":")
    if not sep or not source or not dest:
        raise ValueError(f"Expected SOURCE:DEST, got {value!r}")
    if not PurePosixPath(dest).is_absolute():
        raise ValueError(f"Destination must be an absolute path, got {dest!r}")
    if PurePosixPath(dest) == PurePosixPath(LOGS_DIR):
        raise ValueError(f"Destination {dest!r} is reserved for the job's logs")
    if not is_local_path(source):
        return source, dest
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = (cwd or Path.cwd()) / path
    path = path.resolve()
    if not path.is_dir():
        raise ValueError(f"Local directory does not exist: {source}")
    return path, dest


ENV_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_secret(value: str) -> str | tuple[str, str]:
    """Parse a `NAME` or `KEY=VALUE` secret spec.

    Returns the Modal secret name as a `str`, or a `(key, value)` pair for a
    local secret.
    """
    key, sep, secret_value = value.partition("=")
    if not sep:
        if not value:
            raise ValueError("Secret name must not be empty")
        return value
    if not ENV_VAR_RE.fullmatch(key):
        raise ValueError(f"Invalid environment variable name {key!r}")
    return key, secret_value


def parse_env_file(path: Path) -> list[tuple[str, str]]:
    """Parse a `.env` file into `(key, value)` pairs.

    Each line is `KEY=VALUE`, optionally prefixed with `export`. Blank lines and
    lines starting with `#` are ignored. Values may be wrapped in single or double
    quotes, and unquoted values end at an inline ` #` comment.
    """
    pairs = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if not sep:
            raise ValueError(f"{path}:{lineno}: Expected KEY=VALUE, got {line!r}")
        if not ENV_VAR_RE.fullmatch(key):
            raise ValueError(f"{path}:{lineno}: Invalid environment variable name {key!r}")
        if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        pairs.append((key, value))
    return pairs


DURATION_RE = re.compile(r"(?:(?P<d>\d+)d)?(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?")
DURATION_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
# Modal's maximum function timeout.
MAX_TIMEOUT = 86400


def parse_duration(value: str, bounded: bool = True) -> int:
    """Parse a duration like `90`, `30s`, `10m`, `2h`, or `1h30m` into seconds.

    A plain number is in seconds. Units must be in `d`, `h`, `m`, `s` order.
    If `bounded`, the duration must be at most Modal's maximum timeout of 24 hours.
    """
    if value.isdigit():
        seconds = int(value)
    else:
        match = DURATION_RE.fullmatch(value)
        if not value or match is None:
            raise ValueError(f"Expected a duration like `90`, `10m`, or `1h30m`, got {value!r}")
        seconds = sum(int(n) * DURATION_UNITS[unit] for unit, n in match.groupdict().items() if n)
    if bounded and not 1 <= seconds <= MAX_TIMEOUT:
        raise ValueError(f"Duration must be between 1 second and 24 hours, got {value!r}")
    if seconds < 1:
        raise ValueError(f"Duration must be at least 1 second, got {value!r}")
    return seconds


MEMORY_RE = re.compile(r"(?P<n>\d+)\s*(?P<unit>[MmGg]i?[Bb]?)?")
MEMORY_UNITS = {"m": 1, "g": 1024}


def parse_memory(value: str) -> int:
    """Parse a memory size like `512`, `512M`, or `4G` into MiB.

    A plain number is in MiB. `M` and `G` mean MiB and GiB, optionally written
    as `Mi`, `MB`, `Gi`, or `GB`.
    """
    match = MEMORY_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"Expected a memory size like `512`, `512M`, or `4G`, got {value!r}")
    unit = (match.group("unit") or "m")[0].lower()
    mebibytes = int(match.group("n")) * MEMORY_UNITS[unit]
    if mebibytes < 1:
        raise ValueError(f"Memory must be at least 1 MiB, got {value!r}")
    return mebibytes


def parse_script_metadata(script: str) -> dict:
    """Parse the PEP 723 `script` metadata block from a Python script."""
    matches = [m for m in SCRIPT_METADATA_RE.finditer(script) if m.group("type") == "script"]
    if len(matches) > 1:
        raise ValueError("Multiple `script` metadata blocks found")
    if not matches:
        return {}
    content = "".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in matches[0].group("content").splitlines(keepends=True)
    )
    return tomllib.loads(content)


def split_requirements(value: str) -> list[str]:
    """Split a comma-separated list of requirements like `uv run --with`.

    Commas inside extras (`pkg[a,b]`) or followed by a version operator
    (`requests>=2,<3`) are part of a single requirement.
    """
    requirements = []
    depth = 0
    start = 0
    for i, char in enumerate(value):
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0:
            if value[i + 1 :].lstrip()[:1] in ("!", "=", "<", ">", "~"):
                continue
            requirements.append(value[start:i])
            start = i + 1
    requirements.append(value[start:])
    return [req.strip() for req in requirements if req.strip()]


def is_script(command: Sequence[str]) -> bool:
    """Return True if `command` runs a Python script, like `uv run script.py`."""
    return command[0].endswith(".py")


def is_url(value: str) -> bool:
    """Return True if `value` is an HTTP(S) URL, like `uv run https://.../script.py`."""
    return value.startswith(("http://", "https://"))


def image_name(image: str) -> str:
    """Return the repository name of a registry image, without its registry, tag, or digest.

    For example, `docker.io/library/python:3.12-slim` is `python`.
    """
    image = image.partition("@")[0]
    # A `:` before the last `/` is a registry port, not a tag.
    repo, _, tag = image.rpartition(":")
    if repo and "/" not in tag:
        image = repo
    return image.rpartition("/")[2]


def fetch_script(url: str) -> str:
    """Download the script at `url`."""
    with urllib.request.urlopen(url) as response:
        return response.read().decode("utf-8")


def build_job(
    command: Sequence[str],
    with_: tuple[str, ...] = (),
    volumes: tuple[tuple[str | Path, str], ...] = (),
    secrets: tuple[str | tuple[str, str], ...] = (),
    gpu: str | None = None,
    image: str | None = None,
    add_python: str | None = None,
    timeout: int | None = None,
    retries: int = 0,
    detach: bool = False,
    cpu: float | None = None,
    memory: int | None = None,
    name: str | None = None,
) -> JobSpec:
    """Build a JobSpec for `command`.

    If `image` is None and `command` starts with a `.py` file, that script is
    uploaded and run with the remaining arguments. Otherwise, `command` is run as is.

    `volumes` holds `(source, dest)` pairs as returned by `parse_volume`, and
    `secrets` holds values as returned by `parse_secret`. `gpu` is the GPU type
    to request, passed to Modal as is. `image` is a registry image to run in, and
    `add_python` is a Python version to add to it. `timeout` is the maximum run
    time in seconds, and `retries` is the number of times to retry the job after it fails.
    If `detach` is True, the job is started without waiting for it to finish.
    `cpu` is the number of CPU cores and `memory` is the memory in MiB to request.
    `name` is the name to find the job by, which defaults to the image's name, the
    script's filename, or the command's program.
    """
    extra = tuple(req for value in with_ for req in split_requirements(value))
    if image is None and is_script(command):
        local_path = Path(command[0]).resolve()
        remote_path = f"/root/{local_path.name}"
        metadata = parse_script_metadata(local_path.read_text())
        function_name = local_path.stem
        command = ["python", remote_path, *command[1:]]
        dependencies = (*metadata.get("dependencies", ()), *extra)
        default_name = local_path.name
    else:
        local_path = remote_path = None
        function_name = re.sub(r"[^A-Za-z0-9_-]", "_", Path(command[0]).name)
        command = list(command)
        dependencies = extra
        default_name = image_name(image) if image is not None else Path(command[0]).name
    modal_volumes = tuple((src, dest) for src, dest in volumes if not isinstance(src, Path))
    local_dirs = tuple((src, dest) for src, dest in volumes if isinstance(src, Path))
    modal_secrets = tuple(secret for secret in secrets if isinstance(secret, str))
    # Later local secrets override earlier ones with the same key.
    local_secrets = tuple(dict(secret for secret in secrets if not isinstance(secret, str)).items())
    return JobSpec(
        function_name=function_name,
        name=name or default_name,
        command=command,
        local_path=local_path,
        remote_path=remote_path,
        dependencies=dependencies,
        volumes=modal_volumes,
        local_dirs=local_dirs,
        secrets=modal_secrets,
        local_secrets=local_secrets,
        gpu=gpu,
        cpu=cpu,
        memory=memory,
        timeout=timeout,
        retries=retries,
        image=image,
        add_python=add_python,
        detach=detach,
    )


def format_job(job: JobSpec) -> str:
    """Format the configuration of `job` for display, masking local secret values."""
    lines = [
        f"Name: {job.name}",
        f"Function: {job.function_name}",
        f"Command: {shlex.join(job.command)}",
    ]
    if job.image is not None:
        lines.append(f"Image: {job.image}")
    if job.add_python is not None:
        lines.append(f"Add Python: {job.add_python}")
    if job.local_path is not None:
        lines.append(f"Script: {job.local_path} -> {job.remote_path}")
    if job.dependencies:
        lines.append(f"Dependencies: {', '.join(job.dependencies)}")
    if job.gpu is not None:
        lines.append(f"GPU: {job.gpu}")
    if job.cpu is not None:
        lines.append(f"CPU: {job.cpu:g}")
    if job.memory is not None:
        lines.append(f"Memory: {job.memory} MiB")
    if job.timeout is not None:
        lines.append(f"Timeout: {job.timeout}s")
    if job.retries:
        lines.append(f"Retries: {job.retries}")
    if job.detach:
        lines.append("Detach: yes")
    for name, dest in job.volumes:
        lines.append(f"Volume: {name} -> {dest}")
    for local_dir, dest in job.local_dirs:
        lines.append(f"Local directory: {local_dir} -> {dest}")
    for name in job.secrets:
        lines.append(f"Secret: {name}")
    for key, _ in job.local_secrets:
        lines.append(f"Local secret: {key}=***")
    return "\n".join(lines)


def job_record(job: JobSpec, app_id: str, call_id: str) -> dict:
    """Build the record for tracking `job`, which never includes local secret values."""
    from modal_jobs._store import RECORD_VERSION, RUNNING

    return {
        "version": RECORD_VERSION,
        "id": app_id,
        "call_id": call_id,
        "name": job.name,
        "command": job.command,
        "image": job.image,
        "add_python": job.add_python,
        "dependencies": list(job.dependencies),
        "gpu": job.gpu,
        "cpu": job.cpu,
        "memory": job.memory,
        "timeout": job.timeout,
        "retries": job.retries,
        "volumes": [list(volume) for volume in job.volumes],
        "local_dirs": [[str(local_dir), dest] for local_dir, dest in job.local_dirs],
        "secrets": list(job.secrets),
        "local_secret_keys": [key for key, _ in job.local_secrets],
        "submitted_by": f"{getpass.getuser()}@{socket.gethostname()}",
        "submitted_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "status": RUNNING,
        "exit_code": None,
        "error": None,
        "log": f"{app_id}.log",
    }


def register_job(job: JobSpec, app_id: str, call_id: str) -> None:
    """Record `job` with the backend, warning instead of failing if that doesn't work."""
    from modal_jobs import _backend

    try:
        _backend.registry().create_job.remote(job_record(job, app_id, call_id))
    # Tracking must never stop the job from running.
    except Exception as e:  # noqa: BLE001
        console.print(
            f"[dim]Job not tracked ({type(e).__name__}). Run `modal-jobs backend deploy`.[/dim]"
        )


def run_job(job: JobSpec) -> str | None:
    """Run `job` on Modal.

    Returns the app ID if the job is still running, because it was detached or
    the client was interrupted, or None if the job finished.
    """
    import modal

    from modal_jobs import _runner

    app = modal.App(include_source=False)
    if job.image is not None:
        image = modal.Image.from_registry(job.image, add_python=job.add_python)
    else:
        image = modal.Image.debian_slim()
    if job.dependencies:
        image = image.uv_pip_install(*job.dependencies)
    image = image.add_local_file(_runner.__file__, "/root/modal_jobs/_runner.py")
    if job.local_path is not None:
        image = image.add_local_file(job.local_path, job.remote_path)
    for local_dir, dest in job.local_dirs:
        image = image.add_local_dir(local_dir, dest, copy=False)
    from modal_jobs._backend import LOGS_VOLUME_NAME

    volumes = {dest: modal.Volume.from_name(name) for name, dest in job.volumes}
    volumes[LOGS_DIR] = modal.Volume.from_name(LOGS_VOLUME_NAME, create_if_missing=True)
    secrets = [modal.Secret.from_name(name) for name in job.secrets]
    if job.local_secrets:
        secrets.append(modal.Secret.from_dict(dict(job.local_secrets)))
    # Modal rejects `timeout=None`, so only pass it when set.
    timeout = {"timeout": job.timeout} if job.timeout is not None else {}
    with modal.enable_output():
        run_cmd_local = app.function(
            image=image,
            name=job.function_name,
            volumes=volumes,
            secrets=secrets,
            gpu=job.gpu,
            cpu=job.cpu,
            memory=job.memory,
            retries=job.retries or None,
            **timeout,
        )(run_cmd)
        # A detached app keeps running the job if the client disconnects, e.g. on Ctrl-C.
        finished = False
        with app.run(detach=True):
            log_path = f"{LOGS_DIR}/{app.app_id}.log"
            call = run_cmd_local.spawn(job.command, log_path, LOGS_VOLUME_NAME)
            register_job(job, app.app_id, call.object_id)
            if not job.detach:
                call.get()
                finished = True
    return None if finished else app.app_id


def parse_volumes(ctx, param, values: tuple[str, ...]) -> tuple[tuple[str | Path, str], ...]:
    try:
        return tuple(parse_volume(value) for value in values)
    except ValueError as e:
        raise click.BadParameter(str(e)) from e


def parse_timeout(ctx, param, value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return parse_duration(value)
    except ValueError as e:
        raise click.BadParameter(str(e)) from e


def parse_memory_option(ctx, param, value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return parse_memory(value)
    except ValueError as e:
        raise click.BadParameter(str(e)) from e


def parse_env_files(ctx, param, values: tuple[Path, ...]) -> tuple[tuple[str, str], ...]:
    try:
        return tuple(pair for value in values for pair in parse_env_file(value))
    except ValueError as e:
        raise click.BadParameter(str(e)) from e


def parse_secrets(ctx, param, values: tuple[str, ...]) -> tuple[str | tuple[str, str], ...]:
    try:
        return tuple(parse_secret(value) for value in values)
    except ValueError as e:
        raise click.BadParameter(str(e)) from e


volume_option = click.option(
    "-v",
    "--volume",
    "volumes",
    multiple=True,
    metavar="SOURCE:DEST",
    callback=parse_volumes,
    help="Mount the Modal volume named SOURCE at DEST, or add the local directory "
    "SOURCE to the image at DEST. SOURCE is a local directory if it starts with "
    "`.` or `~` or contains `/`. May be provided multiple times.",
)
secret_option = click.option(
    "-s",
    "--secret",
    "--secrets",
    "secrets",
    multiple=True,
    metavar="NAME|KEY=VALUE",
    callback=parse_secrets,
    help="Add the Modal secret named NAME to the container's environment, or set "
    "the environment variable KEY to VALUE as a secret. May be provided multiple times.",
)
env_file_option = click.option(
    "--env-file",
    "env_files",
    multiple=True,
    metavar="PATH",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    callback=parse_env_files,
    help="Set the environment variables in the `.env` file PATH as secrets. "
    "`--secret KEY=VALUE` takes precedence. May be provided multiple times.",
)
gpu_option = click.option(
    "--gpu",
    metavar="GPU",
    help="Run on a GPU, e.g. `T4`, `A100-80GB`, or `H100:8` for multiple GPUs.",
)
cpu_option = click.option(
    "--cpu",
    type=click.FloatRange(min=0, min_open=True),
    metavar="CORES",
    help="Request CORES CPU cores, e.g. `4` or `0.5`. Defaults to Modal's default.",
)
memory_option = click.option(
    "--memory",
    metavar="SIZE",
    callback=parse_memory_option,
    help="Request SIZE of memory, e.g. `512` (MiB), `512M`, or `4G`. Defaults to Modal's default.",
)
timeout_option = click.option(
    "--timeout",
    metavar="DURATION",
    callback=parse_timeout,
    help="Stop the job after DURATION, e.g. `90` (seconds), `10m`, `2h`, or `1h30m`. "
    "Defaults to Modal's default of 5 minutes, up to a maximum of 24 hours.",
)
# Modal's maximum number of retries.
MAX_RETRIES = 10
retries_option = click.option(
    "--retries",
    type=click.IntRange(0, MAX_RETRIES),
    default=0,
    metavar="N",
    help=f"Retry the job up to N times if it fails. Defaults to 0, up to a maximum of {MAX_RETRIES}.",
)
detach_option = click.option(
    "-d",
    "--detach",
    is_flag=True,
    help="Start the job and return without waiting for it to finish. "
    "The job keeps running in the background.",
)


def parse_name(ctx, param, value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise click.BadParameter("Name must not be empty")
    return value


name_option = click.option(
    "--name",
    metavar="NAME",
    callback=parse_name,
    help="Name the job, to find it with `modal-jobs ls --name`. "
    "Defaults to the image's name or the script's filename.",
)
dry_run_option = click.option(
    "--dry-run",
    is_flag=True,
    help="Print the configuration of the job without running it.",
)
COMMAND_CONTEXT_SETTINGS = {"ignore_unknown_options": True, "allow_interspersed_args": False}


def run_and_report(job: JobSpec, name: str):
    try:
        app_id = run_job(job)
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"{name} exited with code {e.returncode}") from e
    if app_id is None:
        console.print(f"[bold green]✓[/bold green] Finished running {name}")
    elif job.detach:
        console.print(
            f"Started {name} in the background.\n\n"
            f"Stream logs:\n  [green]modal-jobs logs --follow {app_id}[/green]\n\n"
            f"Wait for it:\n  [green]modal-jobs wait {app_id}[/green]\n\n"
            f"Stop the job:\n  [green]modal-jobs stop {app_id}[/green]"
        )
    # Otherwise the client was interrupted, and Modal already printed how to track the job.


@click.group()
def main():
    """Run jobs on Modal."""


@main.command("run", context_settings=COMMAND_CONTEXT_SETTINGS)
@click.argument("image")
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@volume_option
@secret_option
@env_file_option
@gpu_option
@cpu_option
@memory_option
@timeout_option
@retries_option
@click.option(
    "--add-python",
    metavar="VERSION",
    help="Add the given Python version to IMAGE, e.g. `3.12`. Required if IMAGE "
    "does not have Python.",
)
@detach_option
@name_option
@dry_run_option
def docker_run(
    image: str,
    command: tuple[str, ...],
    volumes: tuple[tuple[str | Path, str], ...],
    secrets: tuple[str | tuple[str, str], ...],
    env_files: tuple[tuple[str, str], ...],
    gpu: str | None,
    cpu: float | None,
    memory: int | None,
    timeout: int | None,
    retries: int,
    add_python: str | None,
    detach: bool,
    name: str | None,
    dry_run: bool,
):
    """Run COMMAND in the registry image IMAGE on Modal, like `docker run`.

    For example, `modal-jobs run --add-python 3.12 docker.io/ubuntu echo hi`.
    """
    job = build_job(
        command,
        volumes=volumes,
        secrets=(*env_files, *secrets),
        gpu=gpu,
        cpu=cpu,
        memory=memory,
        image=image,
        add_python=add_python,
        timeout=timeout,
        retries=retries,
        detach=detach,
        name=name,
    )
    if dry_run:
        click.echo(format_job(job))
        return
    import modal.exception

    try:
        run_and_report(job, shlex.join(command))
    except modal.exception.ConflictError as e:
        if add_python is not None or "version of Python" not in str(e):
            raise
        raise click.ClickException(
            f"Could not find Python in {image}. Add it with `--add-python`, "
            "e.g. `--add-python 3.12`."
        ) from e


@main.group()
def uv():
    """Run jobs with uv."""


@uv.command("run", context_settings=COMMAND_CONTEXT_SETTINGS)
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@click.option(
    "--with",
    "with_",
    multiple=True,
    metavar="WITH",
    help="Run with the given packages installed. May be provided multiple times, "
    "or as a comma-separated list.",
)
@volume_option
@secret_option
@env_file_option
@gpu_option
@cpu_option
@memory_option
@timeout_option
@retries_option
@detach_option
@name_option
@dry_run_option
def uv_run(
    command: tuple[str, ...],
    with_: tuple[str, ...],
    volumes: tuple[tuple[str | Path, str], ...],
    secrets: tuple[str | tuple[str, str], ...],
    env_files: tuple[tuple[str, str], ...],
    gpu: str | None,
    cpu: float | None,
    memory: int | None,
    timeout: int | None,
    retries: int,
    detach: bool,
    name: str | None,
    dry_run: bool,
):
    """Run COMMAND on Modal with `uv run`.

    If COMMAND starts with a Python script, the script is uploaded and run with
    its inline dependencies. The script may also be a URL, which is downloaded
    first, or `-` to read it from stdin. Otherwise, COMMAND is run as is, e.g.
    `modal-jobs uv run python -c 'print("hi")'`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        if command[0] == "-":
            script = click.get_text_stream("stdin").read()
            if not script.strip():
                raise click.BadParameter("No script provided on stdin.", param_hint="COMMAND")
            local_path = Path(tmp) / "stdin.py"
            local_path.write_text(script)
            command = (str(local_path), *command[1:])
        elif is_url(command[0]):
            url = command[0]
            if not is_script(command):
                raise click.BadParameter(
                    f"URL {url!r} must point to a .py script.", param_hint="COMMAND"
                )
            try:
                script = fetch_script(url)
            except (urllib.error.URLError, UnicodeDecodeError) as e:
                raise click.ClickException(f"Could not download {url}: {e}") from e
            local_path = Path(tmp) / PurePosixPath(urllib.parse.urlparse(url).path).name
            local_path.write_text(script)
            command = (str(local_path), *command[1:])
        if is_script(command):
            path = Path(command[0])
            if not path.is_file():
                raise click.BadParameter(
                    f"File {str(path)!r} does not exist.", param_hint="COMMAND"
                )
            display_name = path.name
        else:
            display_name = shlex.join(command)
        try:
            job = build_job(
                command,
                with_,
                volumes,
                (*env_files, *secrets),
                gpu,
                cpu=cpu,
                memory=memory,
                timeout=timeout,
                retries=retries,
                detach=detach,
                name=name,
            )
        except (ValueError, tomllib.TOMLDecodeError) as e:
            raise click.ClickException(f"Invalid script metadata in {display_name}: {e}") from e
        if dry_run:
            click.echo(format_job(job))
            return
        run_and_report(job, display_name)


STATUS_STYLES = {
    "running": "yellow",
    "succeeded": "green",
    "failed": "red",
    "timed_out": "red",
    "stopped": "dim",
    "unknown": "dim",
}


def format_duration(seconds: float) -> str:
    """Format `seconds` like `45s`, `3m12s`, `2h5m`, or `1d3h`."""
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{seconds}s"
    return f"{seconds}s"


def record_duration(record: dict, now: float | None = None) -> str:
    """Return how long the job of `record` ran, or has been running, or `-` if unknown."""
    started_at = record.get("started_at")
    if record["status"] == "running":
        # The start time is only known once the job finishes, so count from submission.
        started_at = record["submitted_at"]
        finished_at = time.time() if now is None else now
    else:
        finished_at = record.get("finished_at")
    if started_at is None or finished_at is None:
        return "-"
    return format_duration(max(finished_at - started_at, 0))


def format_status(status: str) -> str:
    style = STATUS_STYLES.get(status, "")
    return f"[{style}]{status}[/{style}]" if style else status


def format_timestamp(timestamp: float | None) -> str:
    if timestamp is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def format_memory(mebibytes: float) -> str:
    """Format a memory size in MiB like `512M` or `3.9G`."""
    if mebibytes < 1024:
        return f"{mebibytes:.0f}M"
    return f"{mebibytes / 1024:.1f}G"


def format_bytes(size: int) -> str:
    """Format a size in bytes like `512B`, `3K`, `512M`, or `3.9G`."""
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f}K"
    return format_memory(size / (1024 * 1024))


def format_exit_code(record: dict) -> str:
    """Format the exit code of `record`, naming the signal that killed the job, if any."""
    code = record["exit_code"]
    if code >= 0:
        return str(code)
    try:
        name = signal.Signals(-code).name
    except ValueError:
        return f"{code} (killed by signal {-code})"
    # The kernel's out-of-memory killer sends SIGKILL. `--memory` is only a request,
    # so peak memory can't confirm it.
    if name == "SIGKILL":
        return f"{code} (killed by {name}, possibly out of memory)"
    return f"{code} (killed by {name})"


def format_usage(record: dict) -> list[str]:
    """Format the resource usage and location of the job of `record`."""
    lines = []
    peak = record.get("peak_memory_mib")
    if peak is not None:
        line = f"Peak memory: {format_memory(peak)}"
        if record.get("memory"):
            line += (
                f" ({peak / record['memory']:.0%} of {format_memory(record['memory'])} requested)"
            )
        lines.append(line)
    cpu_seconds = record.get("cpu_seconds")
    if cpu_seconds is not None:
        line = f"CPU time: {format_duration(cpu_seconds)}"
        wall = (record.get("finished_at") or 0) - (record.get("started_at") or 0)
        if wall > 0:
            line += f" ({cpu_seconds / wall:.1f} cores on average"
            line += f" of {record['cpu']:g})" if record.get("cpu") else ")"
        lines.append(line)
    if record.get("task_id"):
        where = ", ".join(value for value in (record.get("region"), record.get("cloud")) if value)
        lines.append(f"Container: {record['task_id']}" + (f" ({where})" if where else ""))
    return lines


def format_record(record: dict) -> str:
    """Format a job record for display, in the same style as `format_job`."""
    lines = [
        f"ID: {record['id']}",
        f"Name: {record['name']}",
        f"Status: {format_status(record['status'])}",
    ]
    if record.get("exit_code") is not None:
        lines.append(f"Exit code: {format_exit_code(record)}")
    if record.get("error"):
        lines.append(f"Error: {record['error']}")
    lines += format_usage(record)
    lines.append(f"Command: {shlex.join(record['command'])}")
    if record.get("image"):
        lines.append(f"Image: {record['image']}")
    if record.get("add_python"):
        lines.append(f"Add Python: {record['add_python']}")
    if record.get("dependencies"):
        lines.append(f"Dependencies: {', '.join(record['dependencies'])}")
    if record.get("gpu"):
        lines.append(f"GPU: {record['gpu']}")
    if record.get("cpu") is not None:
        lines.append(f"CPU: {record['cpu']:g}")
    if record.get("memory") is not None:
        lines.append(f"Memory: {record['memory']} MiB")
    if record.get("timeout") is not None:
        lines.append(f"Timeout: {record['timeout']}s")
    if record.get("retries"):
        lines.append(f"Retries: {record['retries']}")
    for name, dest in record.get("volumes", ()):
        lines.append(f"Volume: {name} -> {dest}")
    for local_dir, dest in record.get("local_dirs", ()):
        lines.append(f"Local directory: {local_dir} -> {dest}")
    for name in record.get("secrets", ()):
        lines.append(f"Secret: {name}")
    for key in record.get("local_secret_keys", ()):
        lines.append(f"Local secret: {key}=***")
    lines += [
        f"Submitted by: {record['submitted_by']}",
        f"Submitted: {format_timestamp(record['submitted_at'])}",
        f"Started: {format_timestamp(record.get('started_at'))}",
        f"Finished: {format_timestamp(record.get('finished_at'))}",
        f"Duration: {record_duration(record)}",
    ]
    return "\n".join(lines)


def get_registry():
    """Return the deployed `Registry`, or fail with a hint to deploy it."""
    import modal.exception

    from modal_jobs import _backend

    try:
        return _backend.registry()
    except modal.exception.NotFoundError as e:
        raise click.ClickException(
            "The modal-jobs backend is not deployed. Deploy it with `modal-jobs backend deploy`."
        ) from e


json_option = click.option("--json", "as_json", is_flag=True, help="Print the job records as JSON.")


@main.command("ls")
@click.option(
    "-n",
    "--limit",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    help="Show at most this many jobs, newest first.",
)
@click.option(
    "--status",
    type=click.Choice(list(STATUS_STYLES)),
    help="Only show jobs with this status.",
)
@click.option("--name", metavar="NAME", help="Only show jobs with this name.")
@json_option
def ls(limit: int, status: str | None, name: str | None, as_json: bool):
    """List recent jobs."""
    # Only pass `name` when set, so `ls` works with backends deployed before it existed.
    filters = {"name": name} if name is not None else {}
    records = get_registry().list_jobs.remote(limit, status, **filters)
    if as_json:
        click.echo(json.dumps(records, indent=2))
        return
    if not records:
        console.print("No jobs found.")
        return
    now = time.time()
    rows = [
        (
            record["id"],
            record["name"],
            record["status"],
            f"{format_duration(now - record['submitted_at'])} ago",
            record_duration(record, now),
            format_memory(record["peak_memory_mib"]) if record.get("peak_memory_mib") else "-",
            record.get("gpu") or "-",
        )
        for record in records
    ]
    headers = ("ID", "NAME", "STATUS", "SUBMITTED", "DURATION", "MEM", "GPU")
    widths = [max(len(header), *(len(row[i]) for row in rows)) for i, header in enumerate(headers)]
    # Shorten only the name when space is tight, since IDs are needed in full for `show`.
    gaps = 2 * (len(headers) - 1)
    others = sum(widths) - widths[1]
    widths[1] = max(min(widths[1], 40, console.width - others - gaps), len("NAME"))
    table = Table(box=None, pad_edge=False)
    for header, width in zip(headers, widths):
        table.add_column(header, no_wrap=True, overflow="ellipsis", width=width)
    for row in rows:
        table.add_row(row[0], row[1], format_status(row[2]), *row[3:])
    console.print(table)


def get_record(job_id: str) -> dict:
    """Return the record of the job `job_id`, which may be a unique prefix."""
    try:
        return get_registry().get_job.remote(job_id)
    except KeyError as e:
        raise click.ClickException(f"No job found with ID {job_id!r}.") from e
    except ValueError as e:
        raise click.ClickException(str(e)) from e


def registry_method(name: str):
    """Return the method `name` of the deployed `Registry`, or fail with a hint to update it."""
    try:
        return getattr(get_registry(), name)
    # Backends deployed before the method existed don't have it.
    except AttributeError as e:
        raise click.ClickException(
            "The modal-jobs backend is out of date. Update it with `modal-jobs backend deploy`."
        ) from e


@main.command("show")
@click.argument("job_id")
@json_option
def show(job_id: str, as_json: bool):
    """Show the details of the job JOB_ID, which may be a unique prefix."""
    record = get_record(job_id)
    if as_json:
        click.echo(json.dumps(record, indent=2))
        return
    console.print(format_record(record), highlight=False)
    running = record["status"] == "running"
    if running:
        console.print(
            f"\nStream logs:\n  [green]modal-jobs logs --follow {record['id']}[/green]",
            highlight=False,
        )
    if record.get("log"):
        console.print(
            f"\nShow saved logs:\n  [green]modal-jobs logs {record['id']}[/green]", highlight=False
        )
    elif not running:
        console.print(
            f"\nShow logs:\n  [green]modal app logs {record['id']}[/green]", highlight=False
        )
    if running:
        console.print(f"\nStop the job:\n  [green]modal-jobs stop {record['id']}[/green]")


def stream_logs(app_id: str) -> None:
    """Stream the logs of the app `app_id` until it stops."""
    subprocess.run([sys.executable, "-m", "modal", "app", "logs", "--follow", app_id], check=False)


@main.command("logs")
@click.argument("job_id")
@click.option(
    "-f",
    "--follow",
    is_flag=True,
    help="Stream the output of a running job until it finishes.",
)
@click.option(
    "-n",
    "--tail",
    type=click.IntRange(min=1),
    metavar="N",
    help="Only print the last N lines of the saved output.",
)
def logs(job_id: str, follow: bool, tail: int | None):
    """Print the saved output of the job JOB_ID, which may be a unique prefix."""
    import modal.exception

    from modal_jobs import _backend

    if follow and tail is not None:
        raise click.UsageError("--follow and --tail can't be used together.")
    record = get_record(job_id)
    if follow and record["status"] == "running":
        stream_logs(record["id"])
        return
    if not record.get("log"):
        raise click.ClickException(
            f"Job {record['id']} was run before logs were saved. "
            f"Try `modal app logs {record['id']}`."
        )
    try:
        output = _backend.read_log(record["id"])
    except (FileNotFoundError, modal.exception.NotFoundError) as e:
        raise click.ClickException(
            f"No output was saved for job {record['id']} yet. "
            f"Try `modal-jobs logs --follow {record['id']}`."
        ) from e
    if tail is not None:
        output = b"".join(output.splitlines(keepends=True)[-tail:])
    click.echo(output, nl=False)


yes_option = click.option("-y", "--yes", is_flag=True, help="Don't ask for confirmation.")


def stop_app(app_id: str) -> None:
    """Stop the Modal app `app_id`, ignoring failures, e.g. when it already stopped."""
    subprocess.run(
        [sys.executable, "-m", "modal", "app", "stop", "--yes", app_id],
        capture_output=True,
        check=False,
    )


@main.command("stop")
@click.argument("job_ids", nargs=-1, required=True, metavar="JOB_ID...")
@yes_option
def stop(job_ids: tuple[str, ...], yes: bool):
    """Stop the running jobs JOB_ID..., which may be unique prefixes."""
    stop_job = registry_method("stop_job")
    records = [get_record(job_id) for job_id in job_ids]
    running = [record for record in records if record["status"] == "running"]
    for record in records:
        if record["status"] != "running":
            console.print(f"{record['id']} already {format_status(record['status'])}")
    if not running:
        return
    if not yes:
        ids = ", ".join(record["id"] for record in running)
        click.confirm(f"Stop {ids}?", abort=True)
    stopped_by = f"{getpass.getuser()}@{socket.gethostname()}"
    for record in running:
        record = stop_job.remote(record["id"], stopped_by)
        stop_app(record["id"])
        if record["status"] == "stopped":
            console.print(f"[bold green]✓[/bold green] Stopped {record['id']}")
        else:
            # The job finished before it could be stopped.
            console.print(f"{record['id']} already {format_status(record['status'])}")


def job_exit_code(record: dict) -> int:
    """Return the exit code for `modal-jobs wait` to exit with for the finished job `record`."""
    if record["status"] == "succeeded":
        return 0
    if record["status"] == "failed" and (record.get("exit_code") or 0) > 0:
        return record["exit_code"]
    return 1


# Like `timeout(1)`, exit with 124 when the wait times out.
WAIT_TIMEOUT_EXIT_CODE = 124


@main.command("wait")
@click.argument("job_id")
@click.option(
    "--timeout",
    metavar="DURATION",
    callback=parse_timeout,
    help="Give up after DURATION, e.g. `90` (seconds), `10m`, or `2h`, and exit with "
    f"{WAIT_TIMEOUT_EXIT_CODE}. Defaults to waiting until the job finishes.",
)
@click.option(
    "--interval",
    type=click.FloatRange(min=0, min_open=True),
    default=5,
    show_default=True,
    metavar="SECONDS",
    help="Check the job's status every SECONDS.",
)
def wait(job_id: str, timeout: int | None, interval: float):
    """Wait for the job JOB_ID to finish, and exit with its exit code.

    Exits with 0 if the job succeeded, the job's exit code if it failed, and 1 if it
    timed out or was stopped.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    record = get_record(job_id)
    while record["status"] == "running":
        if deadline is not None and time.monotonic() >= deadline:
            console.print(f"Timed out waiting for {record['id']}", highlight=False)
            sys.exit(WAIT_TIMEOUT_EXIT_CODE)
        time.sleep(interval if deadline is None else min(interval, deadline - time.monotonic()))
        record = get_record(record["id"])
    lines = [f"Job {record['id']} {format_status(record['status'])}"]
    if record.get("exit_code") is not None:
        lines.append(f"Exit code: {format_exit_code(record)}")
    if record.get("error"):
        lines.append(f"Error: {record['error']}")
    console.print("\n".join(lines), highlight=False)
    sys.exit(job_exit_code(record))


def delete_jobs(records: list[dict]) -> None:
    """Delete the records and saved output of the finished jobs `records`."""
    from modal_jobs import _backend

    ids = [record["id"] for record in records]
    try:
        registry_method("delete_jobs").remote(ids)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    _backend.delete_logs(ids)
    noun = "job" if len(ids) == 1 else "jobs"
    console.print(f"[bold green]✓[/bold green] Deleted {len(ids)} {noun}")


@main.command("rm")
@click.argument("job_ids", nargs=-1, required=True, metavar="JOB_ID...")
@yes_option
def rm(job_ids: tuple[str, ...], yes: bool):
    """Delete the finished jobs JOB_ID... and their saved output.

    JOB_ID may be a unique prefix.
    """
    records = [get_record(job_id) for job_id in job_ids]
    running = [record["id"] for record in records if record["status"] == "running"]
    if running:
        raise click.ClickException(
            f"Can't delete running jobs: {', '.join(running)}. "
            "Stop them first with `modal-jobs stop`."
        )
    # Prefixes may resolve to the same job.
    records = list({record["id"]: record for record in records}.values())
    if not yes:
        click.confirm(f"Delete {', '.join(record['id'] for record in records)}?", abort=True)
    delete_jobs(records)


def parse_age(ctx, param, value: str) -> int:
    try:
        return parse_duration(value, bounded=False)
    except ValueError as e:
        raise click.BadParameter(str(e)) from e


@main.command("prune")
@click.option(
    "--older-than",
    metavar="DURATION",
    default="7d",
    show_default=True,
    callback=parse_age,
    help="Only delete jobs submitted more than DURATION ago, e.g. `12h` or `30d`.",
)
@click.option(
    "--status",
    type=click.Choice([status for status in STATUS_STYLES if status != "running"]),
    help="Only delete jobs with this status.",
)
@click.option("--dry-run", is_flag=True, help="Print the jobs to delete without deleting them.")
@yes_option
def prune(older_than: int, status: str | None, dry_run: bool, yes: bool):
    """Delete finished jobs and their saved output."""
    cutoff = time.time() - older_than
    records = [
        record
        for record in get_registry().list_jobs.remote(None, status)
        if record["status"] != "running" and record["submitted_at"] < cutoff
    ]
    if not records:
        console.print("No jobs to delete.")
        return
    if dry_run:
        for record in records:
            click.echo(f"{record['id']}  {record['name']}  {record['status']}")
        return
    if not yes:
        noun = "job" if len(records) == 1 else "jobs"
        click.confirm(f"Delete {len(records)} {noun}?", abort=True)
    delete_jobs(records)


@main.group()
def backend():
    """Manage the modal-jobs backend that tracks jobs."""


@backend.command("deploy")
def backend_deploy():
    """Deploy or update the backend app that tracks jobs."""
    import modal

    from modal_jobs import _backend

    with modal.enable_output():
        _backend.app.deploy()
    console.print(f"[bold green]✓[/bold green] Deployed the {_backend.APP_NAME} backend")


@backend.command("status")
def backend_status():
    """Show whether the backend is deployed, and how many jobs and logs it holds."""
    from modal_jobs import _backend

    records = get_registry().list_jobs.remote(None, None)
    counts = Counter(record["status"] for record in records)
    by_status = ", ".join(
        f"{counts[status]} {format_status(status)}" for status in STATUS_STYLES if counts[status]
    )
    log_count, log_bytes = _backend.logs_usage()
    lines = [
        f"Backend: [green]deployed[/green] ({_backend.APP_NAME})",
        f"Jobs: {len(records)}" + (f" ({by_status})" if by_status else ""),
        f"Saved logs: {log_count} files, {format_bytes(log_bytes)}",
    ]
    console.print("\n".join(lines), highlight=False)
