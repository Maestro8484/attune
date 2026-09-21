"""Locks in the three-state Status column in web/studio.py.

The column had exactly two values, "Analyzed" and "Not analyzed", and the second
was doing the work of two completely different situations: a song Attune has not
reached yet, and a song Attune tried and cannot use. They looked identical, so
the list could not separate what needs patience from what needs attention.

What is locked in here:
  * the three states are read off features.error and features.vec, which have
    always carried the distinction -- no new column, no new table;
  * a wrong-width vector is UNANALYZABLE, not pending: an older build wrote it
    and this one cannot read it, so it needs a rescan, which is attention;
  * a track that analyzed cleanly but has no sound fingerprint yet is PENDING,
    not analyzed: the work is genuinely unfinished and one more scan finishes it;
  * the reason sentence is the same sentence in the main song list and in the
    Not Mixable view, because both now come from one helper. They used to be
    built in one place only, so the main list said nothing at all;
  * the sort order is analyzed, then pending, then unanalyzable;
  * `self.analyzed` keeps its old two-state meaning, because recipes.py's three
    seed tiers, smartlists.py's "analyzed" rule and the stats footer read it and
    none of them wants a third value;
  * a row the Not Mixable view holds is never labelled "Analyzed", because
    membership in the pool is the fact and the DB columns are only a hint: the
    engine also drops verified-missing files and non-finite vectors, and neither
    leaves anything in `features.error`.

The database is built from scratch in tmp_path. Nothing here reads the
production library.
"""
from __future__ import annotations
import importlib.util
import os
import sys
import time

import numpy as np
import pytest

import db as dbm
from features import FEATURE_DIM

HERE = os.path.dirname(os.path.abspath(__file__))
ATTUNE_ROOT = os.path.dirname(HERE)
STUDIO_PY = os.path.join(ATTUNE_ROOT, "web", "studio.py")


@pytest.fixture(scope="module")
def studio():
    if not os.path.exists(STUDIO_PY):
        pytest.skip(f"web/studio.py not found at {STUDIO_PY}")
    web_dir = os.path.dirname(STUDIO_PY)
    added = web_dir not in sys.path
    if added:
        sys.path.insert(0, web_dir)
    try:
        spec = importlib.util.spec_from_file_location("attune_studio_under_test",
                                                      STUDIO_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:          # pragma: no cover - environment-dependent
        pytest.skip(f"web/studio.py could not be imported: {type(e).__name__}: {e}")
    finally:
        if added and web_dir in sys.path:
            sys.path.remove(web_dir)


# path key -> (has_vec, dim, error, has_clap), one per situation that can arise
CASES = {
    "analyzed":      (True, FEATURE_DIM, None, True),
    "old_width":     (True, 40, None, True),
    "no_fingerprint": (True, FEATURE_DIM, None, False),
    "failed":        (False, 0, "too short / empty", False),
    "not_reached":   (False, 0, None, False),
}


@pytest.fixture
def library(tmp_path):
    """A five-track library covering every situation the columns can express.

    The pool this hands back is NOT what the real engine would build; the comment
    at the bottom of this fixture says exactly how it differs and why."""
    dbp = str(tmp_path / "scratch.db")
    conn = dbm.connect(dbp)
    rng = np.random.default_rng(3)
    now = int(time.time())
    paths = {}
    for key, (has_vec, dim, err, has_clap) in CASES.items():
        p = str(tmp_path / f"{key}.mp3")
        paths[key] = p
        conn.execute(
            "INSERT INTO tracks(path, artist, album, title, genre, year, seconds, "
            "bytes, mtime) VALUES(?,?,?,?,?,?,?,?,?)",
            (p, "Artist", "Album", key, "Rock", 2020, 200, 4096, now))
        vec = rng.normal(size=dim).astype(np.float32).tobytes() if has_vec else None
        conn.execute(
            "INSERT INTO features(path, vec, dim, error, src_mtime) VALUES(?,?,?,?,?)",
            (p, vec, dim if has_vec else None, err, now))
        if has_clap:
            conn.execute("INSERT INTO clap(path, vec, dim) VALUES(?,?,?)",
                         (p, rng.normal(size=512).astype(np.float32).tobytes(), 512))
    conn.commit()
    conn.close()
    # The pool is what hybrid.py would hand LibraryIndex.
    #
    # "old_width" is in it DELIBERATELY AND SYNTHETICALLY. The real engine would not
    # put it there: src/hybrid.py admits a features row to its librosa matrix only
    # when dim == 79, so a 40-wide vector never reaches the pool, and in a settled
    # library every row of the main song list therefore reads Analyzed. This fixture
    # hands LibraryIndex a pool the engine would not build, on purpose, so the
    # main-list branch of the three-state logic is exercised at all rather than being
    # dead code nobody ever proves. An earlier version of this comment claimed the
    # engine WOULD produce this pool, which was simply wrong.
    #
    # "not_reached" is in it for the sort: without a pending row in the pool there is
    # nothing to order between analyzed and unanalyzable, and the sort test passed
    # against the OLD two-state key by accident.
    pool = [paths["analyzed"], paths["old_width"], paths["not_reached"]]
    return dbp, paths, pool


def _index(studio, library):
    dbp, paths, pool = library
    meta = {p: {"artist": "Artist", "album": "Album", "title": "t", "year": 2020}
            for p in pool}
    return studio.LibraryIndex(dbp, pool, meta), paths


# ------------------------------------------------------------------ the classifier

def test_the_classifier_names_all_three(studio):
    s = studio
    assert s._status_of("", True, 79, True) == s.ANALYZED
    assert s._status_of("", False, 0, False) == s.PENDING
    assert s._status_of("too short / empty", False, 0, False) == s.UNANALYZABLE


def test_a_wrong_width_vector_needs_attention_not_patience(studio):
    assert studio._status_of("", True, 40, True) == studio.UNANALYZABLE


def test_analyzed_but_no_fingerprint_yet_is_pending(studio):
    assert studio._status_of("", True, 79, False) == studio.PENDING


def test_a_stored_error_wins_over_everything_else(studio):
    assert studio._status_of("could not decode", True, 79, True) == studio.UNANALYZABLE


def test_the_states_sort_done_then_coming_then_needs_a_decision(studio):
    order = studio.STATUS_ORDER
    assert order[studio.ANALYZED] < order[studio.PENDING] < order[studio.UNANALYZABLE]


# ----------------------------------------------------------------- the reason text

def test_an_ordinary_analyzed_track_has_nothing_to_explain(studio):
    assert studio._why_sentences("", True, 79, True) == ""


def test_the_stored_reason_is_quoted_verbatim(studio):
    assert studio._why_sentences("too short / empty", False, 0, False) == (
        "too short / empty")


def test_a_track_nothing_has_reached_says_so_in_plain_english(studio):
    why = studio._why_sentences("", False, 0, False)
    assert "Not analyzed yet" in why and "Rescan" in why
    assert "librosa" not in why.lower(), "the name of the library is not theirs to read"


def test_the_missing_fingerprint_is_only_mentioned_when_it_is_the_whole_story(studio):
    """On a track that failed to decode, both halves are missing and the decode error
    is the whole story; adding "and no fingerprint either" is a second line to read
    for no gain."""
    assert "fingerprint" in studio._why_sentences("", True, 79, False)
    assert "fingerprint" not in studio._why_sentences("could not decode", False, 0, False)


# -------------------------------------------------------------- the main song list

def test_the_main_list_names_the_third_state_instead_of_not_analyzed(studio, library):
    lib, paths = _index(studio, library)
    by_path = {lib.paths[i]: lib.row(i, with_trackno=False) for i in range(lib.n)}

    assert by_path[paths["analyzed"]]["status"] == studio.ANALYZED
    assert by_path[paths["old_width"]]["status"] == studio.UNANALYZABLE
    assert by_path[paths["not_reached"]]["status"] == studio.PENDING
    assert "Not analyzed" not in {r["status"] for r in by_path.values()}
    # All three words really do appear, so this is not three rows agreeing by accident.
    assert {r["status"] for r in by_path.values()} == {
        studio.ANALYZED, studio.PENDING, studio.UNANALYZABLE}


def test_the_row_carries_the_reason_so_the_list_can_show_it(studio, library):
    lib, paths = _index(studio, library)
    by_path = {lib.paths[i]: lib.row(i, with_trackno=False) for i in range(lib.n)}
    assert by_path[paths["analyzed"]]["why"] == ""
    assert "older version of Attune" in by_path[paths["old_width"]]["why"]


def test_the_old_two_state_flag_still_means_exactly_what_it_meant(studio, library):
    """Several sorts, the stats footer and userdata.py read `analyzed`. It must stay a
    bool meaning "a readable vector and no error", not gain a third value."""
    lib, paths = _index(studio, library)
    i_analyzed = lib.paths.index(paths["analyzed"])
    i_old = lib.paths.index(paths["old_width"])
    assert lib.analyzed[i_analyzed] is True
    # An old-width vector is still a stored vector with no error, which is what this
    # flag has always reported. The third state is carried separately, in `state`.
    assert lib.analyzed[i_old] is True
    assert all(isinstance(v, bool) for v in lib.analyzed)


def test_sorting_by_status_is_analyzed_then_pending_then_unanalyzable(studio, library):
    """All THREE, in order. The earlier version of this test asserted only first and
    last over a two-row pool whose rows tied on artist, so it passed unchanged against
    the old two-state key -- it proved nothing. Caught by the cold audit, 2026-09-21."""
    lib, _paths = _index(studio, library)
    keyf = studio.LibraryIndex.SORTS["status"]
    order = sorted(range(lib.n), key=lambda i: keyf(lib, i))
    assert [lib.state[i] for i in order] == [studio.ANALYZED, studio.PENDING,
                                             studio.UNANALYZABLE]

    # And the key the old code used cannot produce that order, which is what makes
    # the assertion above worth having.
    old_key = lambda s, i: (not s.analyzed[i], s.f_artist[i])
    old_order = sorted(range(lib.n), key=lambda i: old_key(lib, i))
    assert [lib.state[i] for i in old_order] != [studio.ANALYZED, studio.PENDING,
                                                 studio.UNANALYZABLE]


# -------------------------------------------------------------- the Not Mixable view

def test_not_mixable_separates_waiting_from_cannot_use(studio, library):
    lib, paths = _index(studio, library)
    by_path = {r["path"]: r for r in lib.failures}

    assert by_path[paths["no_fingerprint"]]["state"] == studio.PENDING
    assert by_path[paths["failed"]]["state"] == studio.UNANALYZABLE
    assert paths["analyzed"] not in by_path, "a pool track is not a failure"


def test_not_mixable_never_calls_a_row_analyzed(studio, library):
    """Membership in the pool is the fact; the columns are only a hint. hybrid.py also
    drops verified-missing files (ruling C6) and non-finite vectors, and neither leaves
    a trace in features.error, so a row can reach this view with columns that look
    perfect. Printing "Analyzed" inside a view called Not Mixable is the wrong answer,
    and the route counted such a row under "cannot use" regardless, so the word and the
    number disagreed. Caught by the cold audit, 2026-09-21."""
    lib, _paths = _index(studio, library)
    assert lib.failures, "the fixture stopped producing failure rows"
    assert studio.ANALYZED not in {r["state"] for r in lib.failures}
    assert all(r["state"] in (studio.PENDING, studio.UNANALYZABLE)
               for r in lib.failures)
    assert all(r["why"] for r in lib.failures), "every row here owes a reason"


def test_a_pool_track_with_no_features_row_gets_a_reason_not_a_blank(studio, library):
    """Such a track falls through the constructor loop entirely, so its defaults are the
    only thing that answers for it. A blank would mean the main list and Not Mixable
    disagree about one situation, which is the property the shared helper exists to
    guarantee."""
    lib, paths = _index(studio, library)
    dbp, _paths2, pool = library
    # Delete one pool track's features row and rebuild, so it has none at all.
    import sqlite3
    con = sqlite3.connect(dbp)
    con.execute("DELETE FROM features WHERE path = ?", (paths["analyzed"],))
    con.commit()
    con.close()
    meta = {p: {"artist": "Artist", "album": "Album", "title": "t"} for p in pool}
    rebuilt = studio.LibraryIndex(dbp, pool, meta)
    i = rebuilt.paths.index(paths["analyzed"])
    assert rebuilt.state[i] == studio.PENDING
    assert rebuilt.why[i].startswith("Not analyzed yet"), (
        f"a pool track with no features row got {rebuilt.why[i]!r}")
    assert rebuilt.row(i, with_trackno=False)["why"] == rebuilt.why[i]


def test_not_mixable_still_carries_the_reason_sentence(studio, library):
    lib, paths = _index(studio, library)
    by_path = {r["path"]: r for r in lib.failures}
    assert by_path[paths["failed"]]["why"] == "too short / empty"
    assert "fingerprint" in by_path[paths["no_fingerprint"]]["why"]


def test_both_views_say_the_same_sentence_about_the_same_situation(studio, library):
    """They were built in two places and only one of them was ever read, which is why
    the main list said nothing. One helper now, so they cannot drift."""
    lib, _paths = _index(studio, library)
    i_old = [i for i in range(lib.n) if lib.state[i] == studio.UNANALYZABLE][0]
    from_list = lib.row(i_old, with_trackno=False)["why"]
    direct = studio._why_sentences("", True, 40, True)
    assert from_list == direct
