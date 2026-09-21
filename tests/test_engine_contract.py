"""Locks in the engine result-type contract in attune/src/engine.py:
  * `Ref` is a frozen dataclass and stays one. The decorator came loose from
    the class once, when a function was inserted between the two, and the
    defect had two halves: the loud half raises at import and every sweep
    catches it, while the quiet half -- `Ref` silently ceasing to be a
    dataclass -- raises nothing until somebody runs a search. Measured
    2026-09-21 on a scratch copy with that one decorator deleted: pytest
    reported `97 passed`, the import sweep reported `IMPORT OK`, and neither
    noticed. Constructing the type then fails with
    `TypeError: Ref() takes no arguments`, which is what a person hits.
  * `_as_pool_i` still unwraps a Ref and still passes a bare int through,
    because playback and export resolve every hit through it.
"""
from __future__ import annotations
import dataclasses

import pytest

import engine


def test_ref_is_a_frozen_dataclass():
    assert dataclasses.is_dataclass(engine.Ref), (
        "engine.Ref is not a dataclass. The @dataclass decorator has come away from "
        "the class -- check nothing was inserted between the decorator and `class Ref`.")
    assert engine.Ref.__dataclass_params__.frozen is True


def test_ref_constructs_by_keyword_and_keeps_its_two_fields():
    r = engine.Ref(pool_i=7, label="Artist - Title")
    assert r.pool_i == 7
    assert r.label == "Artist - Title"
    assert [f.name for f in dataclasses.fields(engine.Ref)] == ["pool_i", "label"]


def test_ref_refuses_to_be_mutated():
    r = engine.Ref(pool_i=1, label="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.pool_i = 2


def test_as_pool_i_unwraps_a_ref_and_passes_an_int_through():
    assert engine._as_pool_i(engine.Ref(pool_i=41, label="whatever")) == 41
    assert engine._as_pool_i(41) == 41
