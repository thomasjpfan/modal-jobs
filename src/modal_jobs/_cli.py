import re
import shlex
import subprocess
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import click
from rich.console import Console

from modal_jobs._runner import run_cmd

console = Console()

# Reference regex from https://packaging.python.org/en/latest/specifications/inline-script-metadata/
SCRIPT_METADATA_RE = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)


@dataclass(frozen=True)
class JobSpec:
    function_name: str
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


def parse_duration(value: str) -> int:
    """Parse a duration like `90`, `30s`, `10m`, `2h`, or `1h30m` into seconds.

    A plain number is in seconds. Units must be in `d`, `h`, `m`, `s` order.
    """
    if value.isdigit():
        seconds = int(value)
    else:
        match = DURATION_RE.fullmatch(value)
        if not value or match is None:
            raise ValueError(f"Expected a duration like `90`, `10m`, or `1h30m`, got {value!r}")
        seconds = sum(int(n) * DURATION_UNITS[unit] for unit, n in match.groupdict().items() if n)
    if not 1 <= seconds <= MAX_TIMEOUT:
        raise ValueError(f"Duration must be between 1 second and 24 hours, got {value!r}")
    return seconds


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
    """
    extra = tuple(req for value in with_ for req in split_requirements(value))
    if image is None and is_script(command):
        local_path = Path(command[0]).resolve()
        remote_path = f"/root/{local_path.name}"
        metadata = parse_script_metadata(local_path.read_text())
        function_name = local_path.stem
        command = ["python", remote_path, *command[1:]]
        dependencies = (*metadata.get("dependencies", ()), *extra)
    else:
        local_path = remote_path = None
        function_name = re.sub(r"[^A-Za-z0-9_-]", "_", Path(command[0]).name)
        command = list(command)
        dependencies = extra
    modal_volumes = tuple((src, dest) for src, dest in volumes if not isinstance(src, Path))
    local_dirs = tuple((src, dest) for src, dest in volumes if isinstance(src, Path))
    modal_secrets = tuple(secret for secret in secrets if isinstance(secret, str))
    # Later local secrets override earlier ones with the same key.
    local_secrets = tuple(dict(secret for secret in secrets if not isinstance(secret, str)).items())
    return JobSpec(
        function_name=function_name,
        command=command,
        local_path=local_path,
        remote_path=remote_path,
        dependencies=dependencies,
        volumes=modal_volumes,
        local_dirs=local_dirs,
        secrets=modal_secrets,
        local_secrets=local_secrets,
        gpu=gpu,
        timeout=timeout,
        retries=retries,
        image=image,
        add_python=add_python,
        detach=detach,
    )


def format_job(job: JobSpec) -> str:
    """Format the configuration of `job` for display, masking local secret values."""
    lines = [f"Function: {job.function_name}", f"Command: {shlex.join(job.command)}"]
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
    volumes = {dest: modal.Volume.from_name(name) for name, dest in job.volumes}
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
            retries=job.retries or None,
            **timeout,
        )(run_cmd)
        # A detached app keeps running the job if the client disconnects, e.g. on Ctrl-C.
        finished = False
        with app.run(detach=True):
            call = run_cmd_local.spawn(job.command)
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
            f"Stream logs:\n  [green]modal app logs {app_id}[/green]\n\n"
            f"Stop the job:\n  [green]modal app stop {app_id}[/green]"
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
@timeout_option
@retries_option
@click.option(
    "--add-python",
    metavar="VERSION",
    help="Add the given Python version to IMAGE, e.g. `3.12`. Required if IMAGE "
    "does not have Python.",
)
@detach_option
@dry_run_option
def docker_run(
    image: str,
    command: tuple[str, ...],
    volumes: tuple[tuple[str | Path, str], ...],
    secrets: tuple[str | tuple[str, str], ...],
    env_files: tuple[tuple[str, str], ...],
    gpu: str | None,
    timeout: int | None,
    retries: int,
    add_python: str | None,
    detach: bool,
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
        image=image,
        add_python=add_python,
        timeout=timeout,
        retries=retries,
        detach=detach,
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
@timeout_option
@retries_option
@detach_option
@dry_run_option
def uv_run(
    command: tuple[str, ...],
    with_: tuple[str, ...],
    volumes: tuple[tuple[str | Path, str], ...],
    secrets: tuple[str | tuple[str, str], ...],
    env_files: tuple[tuple[str, str], ...],
    gpu: str | None,
    timeout: int | None,
    retries: int,
    detach: bool,
    dry_run: bool,
):
    """Run COMMAND on Modal with `uv run`.

    If COMMAND starts with a Python script, the script is uploaded and run with
    its inline dependencies. The script may also be a URL, which is downloaded
    first. Otherwise, COMMAND is run as is, e.g.
    `modal-jobs uv run python -c 'print("hi")'`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        if is_url(command[0]):
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
            name = path.name
        else:
            name = shlex.join(command)
        try:
            job = build_job(
                command,
                with_,
                volumes,
                (*env_files, *secrets),
                gpu,
                timeout=timeout,
                retries=retries,
                detach=detach,
            )
        except (ValueError, tomllib.TOMLDecodeError) as e:
            raise click.ClickException(f"Invalid script metadata in {name}: {e}") from e
        if dry_run:
            click.echo(format_job(job))
            return
        run_and_report(job, name)
