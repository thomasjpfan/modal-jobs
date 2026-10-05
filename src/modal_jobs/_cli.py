import re
import subprocess
import tomllib
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
    local_path: Path
    remote_path: str
    function_name: str
    command: list[str]
    dependencies: tuple[str, ...] = ()
    # (volume name, remote path) pairs of Modal volumes to mount.
    volumes: tuple[tuple[str, str], ...] = ()
    # (local directory, remote path) pairs to add to the image.
    local_dirs: tuple[tuple[Path, str], ...] = ()


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


def build_job(
    path: Path,
    with_: tuple[str, ...] = (),
    volumes: tuple[tuple[str | Path, str], ...] = (),
) -> JobSpec:
    """Build a JobSpec for the script at `path`.

    `volumes` holds `(source, dest)` pairs as returned by `parse_volume`.
    """
    remote_path = f"/root/{path.name}"
    metadata = parse_script_metadata(path.read_text())
    extra = [req for value in with_ for req in split_requirements(value)]
    modal_volumes = tuple((src, dest) for src, dest in volumes if not isinstance(src, Path))
    local_dirs = tuple((src, dest) for src, dest in volumes if isinstance(src, Path))
    return JobSpec(
        local_path=path,
        remote_path=remote_path,
        function_name=path.stem,
        command=["python", remote_path],
        dependencies=(*metadata.get("dependencies", ()), *extra),
        volumes=modal_volumes,
        local_dirs=local_dirs,
    )


def run_job(job: JobSpec):
    import modal

    from modal_jobs import _runner

    app = modal.App(include_source=False)
    image = modal.Image.debian_slim()
    if job.dependencies:
        image = image.uv_pip_install(*job.dependencies)
    image = image.add_local_file(_runner.__file__, "/root/modal_jobs/_runner.py")
    image = image.add_local_file(job.local_path, job.remote_path)
    for local_dir, dest in job.local_dirs:
        image = image.add_local_dir(local_dir, dest, copy=False)
    volumes = {dest: modal.Volume.from_name(name) for name, dest in job.volumes}
    with modal.enable_output():
        run_cmd_local = app.function(image=image, name=job.function_name, volumes=volumes)(run_cmd)
        with app.run():
            run_cmd_local.remote(job.command)


def parse_volumes(ctx, param, values: tuple[str, ...]) -> tuple[tuple[str | Path, str], ...]:
    try:
        return tuple(parse_volume(value) for value in values)
    except ValueError as e:
        raise click.BadParameter(str(e)) from e


@click.group()
def main():
    """Run jobs on Modal."""


@main.group()
def uv():
    """Run jobs with uv."""


@uv.command()
@click.argument(
    "path",
    type=click.Path(exists=True, dir_okay=False, resolve_path=True, path_type=Path),
)
@click.option(
    "--with",
    "with_",
    multiple=True,
    metavar="WITH",
    help="Run with the given packages installed. May be provided multiple times, "
    "or as a comma-separated list.",
)
@click.option(
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
def run(
    path: Path,
    with_: tuple[str, ...],
    volumes: tuple[tuple[str | Path, str], ...],
):
    """Run the Python script at PATH on Modal with `uv run`."""
    try:
        job = build_job(path, with_, volumes)
    except (ValueError, tomllib.TOMLDecodeError) as e:
        raise click.ClickException(f"Invalid script metadata in {path.name}: {e}") from e
    try:
        run_job(job)
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"{path.name} exited with code {e.returncode}") from e
    console.print(f"[bold green]✓[/bold green] Finished running {path.name}")
