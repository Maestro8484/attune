"""Every state-changing route refuses a caller from another machine. ISSUES.md row 4.

Measured 2026-09-21: seventeen of the app's thirty-six POST routes let a foreign caller
into the handler, because the guard was a line each route had to remember. It is now one
before_request hook in web/app.py. This test does not keep a list of routes: it walks the
app's own URL map, so a route added tomorrow is covered the day it is added, and a route
that somehow escapes the hook fails here by name.

It reuses the scratch-library fixture from test_settings_routes.py, which redirects
APPDATA, XDG_CONFIG_HOME and ATTUNE_ENV so nothing of the operator's is ever opened.
"""
from __future__ import annotations

import re

from test_settings_routes import FOREIGN, LOCAL, client  # noqa: F401  (pytest fixture)

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _concrete(rule):
    """Fill any <converter:name> part with a harmless value so the URL can be called."""
    return re.sub(r"<(?:[^:<>]+:)?[^<>]+>", "0", rule.rule)


def _write_routes(app):
    out = []
    for rule in app.url_map.iter_rules():
        for m in sorted((rule.methods or set()) & set(WRITE_METHODS)):
            out.append((m, _concrete(rule), rule.endpoint))
    return out


def test_there_are_state_changing_routes_to_check(client):
    # The 2026-09-21 count was 36 POST routes; far fewer means the walk found the wrong app.
    assert len(_write_routes(client.application)) >= 30


def test_every_state_changing_route_refuses_another_machine(client):
    let_in = []
    for method, url, endpoint in _write_routes(client.application):
        r = client.open(url, method=method, json={}, environ_base=FOREIGN)
        if r.status_code != 403:
            let_in.append(f"{method} {url} ({endpoint}) -> {r.status_code}")
    assert not let_in, "reachable from another machine:\n" + "\n".join(let_in)


def test_the_guard_does_not_refuse_this_machine(client):
    # Control: the same calls from 127.0.0.1 must never come back with the guard's 403.
    # (A route may still refuse bad input with 400, or 403 for its own reason; the
    # guard's refusal is recognised by its exact message.)
    wrongly_refused = []
    for method, url, endpoint in _write_routes(client.application):
        if url.startswith(("/api/scan/start", "/api/lib/reload", "/api/lib/verify",
                           "/api/lib/audioinfo", "/api/export/copy", "/api/plexsync",
                           "/api/reveal", "/api/fs/", "/api/track/delete",
                           "/api/track/tags", "/api/plex/", "/api/export/plex",
                           "/api/help/plex-key", "/api/settings")):
            continue    # these start real work or touch the disk or network; the refusal
                        # path above is the part under test, and settings has its own tests
        r = client.open(url, method=method, json={}, environ_base=LOCAL)
        body = r.get_json(silent=True) or {}
        if r.status_code == 403 and "Attune machine itself" in str(body.get("error", "")):
            wrongly_refused.append(f"{method} {url}")
    assert not wrongly_refused, "refused from 127.0.0.1:\n" + "\n".join(wrongly_refused)


def test_reads_stay_open_to_another_machine(client):
    r = client.get("/api/lib/stats", environ_base=FOREIGN)
    assert r.status_code == 200
