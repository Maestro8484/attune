"""Locks in plexmatch.resolve_title's byte-identity contract (attune/src/plexmatch.py).

The `strip` parameter is not a tidying option; it is the reason existing export
filenames did not all change when playlist_name_template was introduced.

  * Every caller that names a playlist by hand -- the CLI and the Plex folder
    mirror -- passes strip=True (the default), because stray edge whitespace
    in a hand-typed name is always a slip.
  * The .m3u8 export routes pass strip=False. The name they have always
    written caps an over-long seed title at 60 characters WITHOUT re-stripping,
    so a cap landing inside a run of spaces legitimately leaves the name ending
    in a space. Four titles in the operator's own 21,255-track library do
    exactly that (measured 2026-09-20). Stripping here would silently rename
    four of his existing exports.

Also locked in: an unknown token is left standing as literal text rather than
blanked, so a mistyped template says so on screen instead of quietly producing
a shorter name.
"""
from __future__ import annotations
import datetime
import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PLEXMATCH_PY = os.path.join(os.path.dirname(HERE), "src", "plexmatch.py")

_spec = importlib.util.spec_from_file_location("attune_plexmatch_under_test", PLEXMATCH_PY)
plexmatch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plexmatch)

WHEN = datetime.datetime(2026, 9, 21, 23, 59, 0)

# web/app.py's _sanitize_stem, reproduced exactly (web/app.py:424-432). It lives inside
# create_app as a closure, so it cannot be imported; if that filter or that cap ever
# changes, this line has to change with it and the space-ending case below is the alarm.
_NAME_OK = lambda c: c.isalnum() or c in " -_"


def _sanitize_stem(s, cap=60):
    safe = "".join(c for c in (s or "") if _NAME_OK(c)).strip()[:cap]
    return safe or "mix"


# The real title that found this, from the operator's own library.
CAP_LANDS_ON_SPACES = ("Mendelssohn - Violin Concerto in E minor, Op. 64 - "
                       "Vivace Non Troppo")


def test_the_case_that_found_it_still_ends_in_a_space_when_strip_is_false():
    """This is the whole point of the parameter. If this test starts failing, four
    playlist files in the operator's library are about to be renamed."""
    seed = _sanitize_stem(CAP_LANDS_ON_SPACES)
    assert seed.endswith(" "), (
        "the fixture no longer exercises the bug: the 60-character cap must land "
        f"inside a run of spaces, and here it gave {seed!r}")

    kept = plexmatch.resolve_title("like-{seed}", when=WHEN, extra={"seed": seed},
                                   strip=False)
    assert kept == "like-" + seed
    assert kept.endswith(" "), "strip=False must leave the trailing space alone"

    tidied = plexmatch.resolve_title("like-{seed}", when=WHEN, extra={"seed": seed},
                                     strip=True)
    assert not tidied.endswith(" ")
    assert tidied != kept, (
        "strip=True and strip=False produced the same name, so this test is no longer "
        "proving anything")


def test_the_cap_is_applied_to_the_seed_not_to_the_assembled_name():
    """Capping the assembled name instead would truncate the literal "like-" prefix off
    a title already at the limit -- five characters nobody exporting today has lost."""
    seed = _sanitize_stem("A" * 200)
    assert len(seed) == 60
    name = plexmatch.resolve_title("like-{seed}", when=WHEN, extra={"seed": seed},
                                   strip=False)
    assert name.startswith("like-")
    assert len(name) == 65


def test_default_template_reproduces_the_name_attune_has_always_written():
    seed = _sanitize_stem("Radiohead - Paranoid Android")
    assert plexmatch.resolve_title("like-{seed}", when=WHEN, extra={"seed": seed},
                                   strip=False) == "like-Radiohead - Paranoid Android"


@pytest.mark.parametrize("template,expected", [
    ("{date}", "26-09-21"),
    ("{ymd}", "2026-09-21"),
    ("{year}", "2026"),
    ("{month}", "September"),
    ("Drive {ymd} mix", "Drive 2026-09-21 mix"),
])
def test_date_tokens_expand_from_the_caller_supplied_moment(template, expected):
    assert plexmatch.resolve_title(template, when=WHEN) == expected


def test_an_unknown_token_is_left_standing_rather_than_blanked():
    assert plexmatch.resolve_title("mix-{nosuchtoken}", when=WHEN) == "mix-{nosuchtoken}"


def test_an_empty_template_falls_back_to_the_default():
    assert plexmatch.resolve_title("", when=WHEN) == "DrivingTunesUSB (26-09-21)"
    assert plexmatch.resolve_title(None, when=WHEN) == "DrivingTunesUSB (26-09-21)"


def test_strip_defaults_to_true_for_every_hand_named_caller():
    assert plexmatch.resolve_title("  Road trip  ", when=WHEN) == "Road trip"


def test_the_moment_is_stamped_once_by_the_caller():
    """A run started at 23:59 must not preview one name and create another. The caller
    supplies `when`, so the same call twice gives the same answer regardless of the
    clock."""
    a = plexmatch.resolve_title("{ymd} {date}", when=WHEN)
    b = plexmatch.resolve_title("{ymd} {date}", when=WHEN)
    assert a == b == "2026-09-21 26-09-21"
