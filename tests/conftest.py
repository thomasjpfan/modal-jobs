import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-modal",
        action="store_true",
        default=False,
        help="Run tests that run against live Modal",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "modal: tests that run against live Modal")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-modal"):
        return
    skip_modal = pytest.mark.skip(reason="needs --run-modal")
    for item in items:
        if "modal" in item.keywords:
            item.add_marker(skip_modal)
