"""The Stage C offline checks run on an exported tree with no .git, where the release-archive tests have nothing
to archive. They are skipped there (named, never silently); CI has the repository and runs every one of them."""
import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NEEDS_GIT_REPOSITORY = {"test_deploy_authorization.py", "test_release_acceptance.py",
                        "test_release_archive_permissions.py", "test_release_host_hardening.py",
                        # needs `git archive HEAD`, which an exported tree cannot do; CI runs it.
                        "test_bound_pins_match_tree.py"}


def _uses_shared_postgres(module, _seen={}):
    """The module reads the test database URL itself or imports a fixture/helper from a module that does."""
    if module.__name__ not in _seen:
        _seen[module.__name__] = False
        path = getattr(module, "__file__", None)
        sources = {inspect.getmodule(value) for value in vars(module).values()} - {None, module}
        _seen[module.__name__] = bool(path and "EXTERNAL_RELEASE_TEST_DATABASE_URL" in Path(path).read_text()) or any(
            source.__name__.startswith("tests.") and _uses_shared_postgres(source) for source in sources)
    return _seen[module.__name__]


@pytest.hookimpl(tryfirst=True)  # before xdist reads the xdist_group markers
def pytest_collection_modifyitems(config, items):
    # Tests on the shared disposable PostgreSQL drop and recreate its tables; under pytest-xdist
    # (--dist loadgroup) they all run serially on one worker while the rest run in parallel.
    for item in items:
        if getattr(item, "module", None) and _uses_shared_postgres(item.module):
            item.add_marker(pytest.mark.xdist_group("shared-postgres"))
    if (ROOT / ".git").exists():
        return
    skip = pytest.mark.skip(reason="needs a git repository (Stage C checks run on an exported tree)")
    for item in items:
        if Path(str(item.fspath)).name in NEEDS_GIT_REPOSITORY:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _fake_tmpfs(monkeypatch):
    """Codex runs only on a tmpfs (app/providers.volatile_root) that cannot swap (providers.swap_off). A test
    directory named `fake-tmpfs*` counts as such a tmpfs, on every platform; nothing else changes."""
    from app import providers
    real = providers._fs_type

    def fs_type(path):
        return "tmpfs" if Path(path).name.startswith("fake-tmpfs") else real(path)
    monkeypatch.setattr(providers, "_fs_type", fs_type)
    monkeypatch.setattr(providers, "swap_off", lambda: True)   # tests with swap on set it back explicitly
    return real

