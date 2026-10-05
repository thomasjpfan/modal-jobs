import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

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


def build_job(path: Path, with_: tuple[str, ...] = ()) -> JobSpec:
    remote_path = f"/root/{path.name}"
    metadata = parse_script_metadata(path.read_text())
    extra = [req for value in with_ for req in split_requirements(value)]
    return JobSpec(
        local_path=path,
        remote_path=remote_path,
        function_name=path.stem,
        command=["python", remote_path],
        dependencies=(*metadata.get("dependencies", ()), *extra),
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
    with modal.enable_output():
        run_cmd_local = app.function(image=image, name=job.function_name)(run_cmd)
        with app.run():
            run_cmd_local.remote(job.command)


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
def run(path: Path, with_: tuple[str, ...]):
    """Run the Python script at PATH on Modal with `uv run`."""
    try:
        job = build_job(path, with_)
    except (ValueError, tomllib.TOMLDecodeError) as e:
        raise click.ClickException(f"Invalid script metadata in {path.name}: {e}") from e
    try:
        run_job(job)
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"{path.name} exited with code {e.returncode}") from e
    console.print(f"[bold green]✓[/bold green] Finished running {path.name}")
