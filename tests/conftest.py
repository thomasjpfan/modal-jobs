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


@pytest.fixture(scope="session", autouse=True)
def modal_test_environment(request):
    """Run live tests in their own Modal environment, deleted when the tests finish.

    The environment holds everything the tests create, including the deployed backend
    and its volumes, so they never touch the jobs in the default environment.
    """
    if not request.config.getoption("--run-modal"):
        yield None
        return
    import uuid

    import modal

    name = f"modal-jobs-test-{uuid.uuid4().hex[:8]}"
    modal.Environment.objects.create(name)
    # Modal reads `MODAL_ENVIRONMENT` on each lookup, and `modal` subprocesses inherit it.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MODAL_ENVIRONMENT", name)
        try:
            yield name
        finally:
            modal.Environment.objects.delete(name)
