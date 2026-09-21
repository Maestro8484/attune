"""Locks in export.valid_plex_url's accept/reject table (attune/src/export.py).

A Plex server address is typed by hand into Preferences, so every way of
getting it slightly wrong has to fail in a way that says so rather than being
stored and failing later as "server unreachable". In particular:

  * a bare "192.168.1.50:32400", which is what a person types, has no scheme
    and must be refused rather than silently read as scheme "192.168.1.50";
  * a copy-pasted browser URL that grew a "/web/index.html" or a "?X-Plex..."
    query is refused, because a server address is not a page address;
  * a name and password embedded in the address is refused outright -- storing
    one would put a password in settings.json where nothing expects to find
    one, and urllib would then hand "user:pass@host" to getaddrinfo and fail
    with a message about an unreachable server, which is not what went wrong;
  * a trailing slash is stripped, so no caller has to think about it.

export.py is loaded by file path rather than imported by name: it is the same
module web/app.py loads, and this keeps the test honest about which file it is
testing.
"""
from __future__ import annotations
import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
EXPORT_PY = os.path.join(os.path.dirname(HERE), "src", "export.py")

_spec = importlib.util.spec_from_file_location("attune_export_under_test", EXPORT_PY)
export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export)


ACCEPTED = [
    ("http://192.168.1.50:32400", "http://192.168.1.50:32400"),
    ("https://192.168.1.50:32400", "https://192.168.1.50:32400"),
    ("http://plexserver", "http://plexserver"),
    ("http://plex.example.com:32400", "http://plex.example.com:32400"),
    # A trailing slash, or several, is the commonest copy-paste artefact.
    ("http://192.168.1.50:32400/", "http://192.168.1.50:32400"),
    ("http://192.168.1.50:32400///", "http://192.168.1.50:32400"),
    # Edge whitespace from a paste.
    ("  http://192.168.1.50:32400  ", "http://192.168.1.50:32400"),
    # IPv6 literal, which must keep its brackets and its port.
    ("http://[fe80::1]:32400", "http://[fe80::1]:32400"),
    ("http://[::1]:32400", "http://[::1]:32400"),
]

REFUSED = [
    "",
    "   ",
    None,
    "192.168.1.50:32400",                       # no scheme: what a person types
    "plexserver",                               # no scheme, bare host
    "ftp://192.168.1.50:32400",                 # wrong scheme
    "file:///C:/plex",                          # wrong scheme
    "http://",                                  # no host
    "http:///library",                          # no host, has a path
    "http://192.168.1.50:32400/web/index.html",  # a page address, not a server address
    "http://192.168.1.50:32400/library",
    "http://192.168.1.50:32400?X-Plex-Token=abc",   # a token must never arrive this way
    "http://192.168.1.50:32400#frag",
    "http://user:pass@192.168.1.50:32400",      # credentials in the address
    "http://user@192.168.1.50:32400",
    "not a url at all",
]


@pytest.mark.parametrize("raw,expected", ACCEPTED)
def test_valid_plex_url_accepts_and_cleans(raw, expected):
    assert export.valid_plex_url(raw) == expected


@pytest.mark.parametrize("raw", REFUSED)
def test_valid_plex_url_refuses(raw):
    assert export.valid_plex_url(raw) is None


def test_a_token_in_the_query_string_is_never_carried_through():
    """Belt to the braces above: whatever else happens, nothing that looks like a
    Plex token may survive into the cleaned address."""
    cleaned = export.valid_plex_url("http://192.168.1.50:32400?X-Plex-Token=SECRET")
    assert cleaned is None
    assert cleaned is None or "SECRET" not in cleaned
