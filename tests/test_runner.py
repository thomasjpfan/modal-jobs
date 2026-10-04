import ast
from pathlib import Path

from modal_jobs import _runner


def test_runner_does_not_import_modal_jobs():
    # _runner.py is mounted alone into the Modal container, so it can't
    # import anything else from the modal_jobs package.
    tree = ast.parse(Path(_runner.__file__).read_text())

    bad_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                bad_imports.append(f"line {node.lineno}: relative import")
                continue
            names = [node.module]
        else:
            continue
        for name in names:
            if name == "modal_jobs" or name.startswith("modal_jobs."):
                bad_imports.append(f"line {node.lineno}: {name}")

    assert not bad_imports, f"_runner.py imports from modal_jobs: {bad_imports}"
