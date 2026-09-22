"""A library reload cannot slip in beside a scan any more (ISSUES.md row 2).

web/libreload.py and web/scanjob.py each held their own start lock, so each was safe
against itself and neither against the other: the reload route checked "is a scan
running", then started, as two separate steps, and a scan could begin in between. The
reload route now does both under the scan job's own start lock, which ScanJob exposes
as `start_lock` because the two modules are loaded by file path and cannot import each
other.

Two checks, against the real app stood up over a scratch library (the fixture from
tests/test_settings_routes.py): a reload while the scan job says it is running is
refused with 409; and a reload asked for while something else HOLDS the scan job's
start lock waits for it rather than proceeding, which is the property the lock adds.
"""
from __future__ import annotations
import threading
import time

from test_settings_routes import client  # noqa: F401  (pytest fixture, reused)

LOCAL = {"REMOTE_ADDR": "127.0.0.1"}


def _scan_job(c):
    job = c.application.extensions.get("attune_scan_job")
    assert job is not None, "app.extensions no longer carries the scan job"
    return job


def test_reload_is_refused_while_a_scan_is_running(client):  # noqa: F811
    job = _scan_job(client)
    assert hasattr(job, "start_lock"), "ScanJob no longer exposes start_lock"
    was = job.running
    job.running = True
    try:
        r = client.post("/api/lib/reload", environ_base=LOCAL)
    finally:
        job.running = was
    assert r.status_code == 409
    assert "scan is running" in r.get_json()["error"]


def test_reload_waits_for_the_scan_start_lock(client):  # noqa: F811
    """Hold the scan job's start lock from the test; the reload request must block until
    it is released, then go through. Without the shared lock it returns immediately."""
    job = _scan_job(client)
    results = []

    def ask():
        results.append(client.post("/api/lib/reload", environ_base=LOCAL).status_code)

    job.start_lock.acquire()
    try:
        t = threading.Thread(target=ask, daemon=True)
        t.start()
        t.join(timeout=1.0)
        assert t.is_alive(), "the reload did not wait for the scan job's start lock"
        assert results == []
    finally:
        job.start_lock.release()
    t.join(timeout=10.0)
    assert not t.is_alive()
    assert results and results[0] in (200, 409), results
    # 409 here would only mean the fixture's reload job was mid-flight from another
    # test; either way the request went through the lock rather than around it.
    time.sleep(0.2)
