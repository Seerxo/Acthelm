"""Repository-wide pytest guard (root conftest: applies to tests/ and evals/ alike).

Stage C checks run pytest with -rp, one `PASSED <node id>` line per test, into a launcher stdout bound of 1 MiB that
fails closed when it overflows. A parametrize value used as its own id (a 1 MiB string) once made every checks run
overflow, so collection refuses oversized ids here."""
import pytest

MAX_NODE_ID = 8192                 # characters in one node id
MAX_PASSED_LINES = 512 * 1024      # bytes of all `PASSED <id>\n` lines: half the launcher's stdout bound


@pytest.hookimpl(trylast=True)     # after xdist and other plugins have finished rewriting node ids
def pytest_collection_modifyitems(config, items):
    if long := [item.nodeid[:80] for item in items if len(item.nodeid) > MAX_NODE_ID]:
        raise pytest.UsageError(f"test ids longer than {MAX_NODE_ID} chars (give parametrize ids=): {long[:3]}")
    total = sum(len(("PASSED " + item.nodeid + "\n").encode()) for item in items)
    if total > MAX_PASSED_LINES:
        raise pytest.UsageError(f"`PASSED <id>` lines total {total} bytes, over {MAX_PASSED_LINES}: shorten ids")


@pytest.fixture(autouse=True)
def _stage_c_publication_on(monkeypatch):
    """Production default holds approved Stage C results (STAGE_C_PUBLISH_ENABLED unset). The publication tests
    exercise the push path, so it is on here; tests of the hold delete it explicitly. Root conftest, so
    evals/ (which call publish() directly) get it too."""
    monkeypatch.setenv("STAGE_C_PUBLISH_ENABLED", "true")
