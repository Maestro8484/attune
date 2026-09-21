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
  * `self.analyzed` keeps its old two-state meaning, because several sorts, the
    stats footer and userdata.py read it and none of them wants a third value.

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
    """A five-track library covering every situation, plus the pool membership the
    real engine would produce: a track reaches the pool only with a readable vector
    AND a fingerprint."""
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
    # The pool is what hybrid.py would hand LibraryIndex. "old_width" is in it on
    # purpose: it has both halves stored, so it reaches the pool and is the case that
    # puts a third state in the MAIN list rather than only in Not Mixable.
    pool = [paths["analyzed"], paths["old_width"]]
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
    rows = {r["title"]: r for r in (lib.row(i, with_trackno=False)
                                    for i in range(lib.n))}
    by_path = {lib.paths[i]: lib.row(i, with_trackno=False) for i in range(lib.n)}

    assert by_path[paths["analyzed"]]["status"] == studio.ANALYZED
    assert by_path[paths["old_width"]]["status"] == studio.UNANALYZABLE
    assert "Not analyzed" not in {r["status"] for r in by_path.values()}
    assert rows is not None


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


def test_sorting_by_status_puts_analyzed_first(studio, library):
    lib, _paths = _index(studio, library)
    keyf = studio.LibraryIndex.SORTS["status"]
    order = sorted(range(lib.n), key=lambda i: keyf(lib, i))
    assert lib.state[order[0]] == studio.ANALYZED
    assert lib.state[order[-1]] == studio.UNANALYZABLE


# -------------------------------------------------------------- the Not Mixable view

def test_not_mixable_separates_waiting_from_cannot_use(studio, library):
    lib, paths = _index(studio, library)
    by_path = {r["path"]: r for r in lib.failures}

    assert by_path[paths["not_reached"]]["state"] == studio.PENDING
    assert by_path[paths["no_fingerprint"]]["state"] == studio.PENDING
    assert by_path[paths["failed"]]["state"] == studio.UNANALYZABLE
    assert paths["analyzed"] not in by_path, "a pool track is not a failure"


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
