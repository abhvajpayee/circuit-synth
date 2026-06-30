"""Unit tests for circuit_synth.core.bus.Bus.

Bus requires an active circuit context because its member Net objects
register on the current circuit.  The `active_circuit` fixture sets one up
via set_current_circuit() without going through the full @circuit decorator,
keeping tests fast and self-contained.
"""
from __future__ import annotations

import pytest

from circuit_synth.core.bus import Bus
from circuit_synth.core.circuit import Circuit
from circuit_synth.core.decorators import set_current_circuit
from circuit_synth.core.exception import CircuitSynthError


@pytest.fixture
def active_circuit():
    c = Circuit("_test", description="", auto_comments=False)
    set_current_circuit(c)
    yield c
    set_current_circuit(None)


# ---------------------------------------------------------------------------
# Construction — plain (width-based) vector bus
# ---------------------------------------------------------------------------

def test_plain_bus_kind(active_circuit):
    b = Bus("DATA", 4)
    assert b.kind == "vector"


def test_plain_bus_width(active_circuit):
    b = Bus("DATA", 4)
    assert b.width == 4


def test_plain_bus_member_names(active_circuit):
    b = Bus("DATA", 4)
    assert b.member_names == ["DATA0", "DATA1", "DATA2", "DATA3"]


def test_plain_bus_start_offset(active_circuit):
    b = Bus("ADDR", 3, start=1)
    assert b.member_names == ["ADDR1", "ADDR2", "ADDR3"]


def test_plain_bus_lo_hi(active_circuit):
    b = Bus("DATA", 8)
    assert b.lo == 0
    assert b.hi == 7


def test_plain_bus_lo_hi_with_start(active_circuit):
    b = Bus("DATA", 4, start=2)
    assert b.lo == 2
    assert b.hi == 5


def test_plain_bus_label(active_circuit):
    b = Bus("FMC_D", 16)
    assert b.label == "FMC_D[0..15]"


def test_plain_bus_alias_names_is_none(active_circuit):
    b = Bus("DATA", 4)
    assert b.alias_names is None


def test_plain_bus_translations_empty(active_circuit):
    b = Bus("DATA", 4)
    assert b.translations == []


def test_plain_bus_width_zero_raises(active_circuit):
    with pytest.raises(ValueError):
        Bus("BAD", 0)


def test_plain_bus_width_negative_raises(active_circuit):
    with pytest.raises(ValueError):
        Bus("BAD", -1)


def test_empty_name_raises(active_circuit):
    with pytest.raises(ValueError):
        Bus("", 4)


# ---------------------------------------------------------------------------
# Construction — aliased bus (members=list-of-strings)
# ---------------------------------------------------------------------------

def test_aliased_bus_kind(active_circuit):
    b = Bus("SPI", members=["SCK", "MISO", "MOSI", "CS"])
    assert b.kind == "aliased"


def test_aliased_bus_width(active_circuit):
    b = Bus("SPI", members=["SCK", "MISO", "MOSI", "CS"])
    assert b.width == 4


def test_aliased_bus_member_names_are_positional(active_circuit):
    b = Bus("SPI", members=["SCK", "MISO", "MOSI", "CS"])
    assert b.member_names == ["SPI_0", "SPI_1", "SPI_2", "SPI_3"]


def test_aliased_bus_alias_names(active_circuit):
    b = Bus("SPI", members=["SCK", "MISO", "MOSI", "CS"])
    assert b.alias_names == ["SPI_SCK", "SPI_MISO", "SPI_MOSI", "SPI_CS"]


def test_aliased_bus_label(active_circuit):
    b = Bus("SPI", members=["SCK", "MISO", "MOSI", "CS"])
    assert b.label == "SPI_[0..3]"


def test_aliased_bus_translations(active_circuit):
    b = Bus("SPI", members=["SCK", "MISO"])
    assert b.translations == [("SPI_0", "SPI_SCK"), ("SPI_1", "SPI_MISO")]


def test_aliased_bus_empty_members_raises(active_circuit):
    with pytest.raises(ValueError):
        Bus("SPI", members=[])


# ---------------------------------------------------------------------------
# Construction — explicit Net objects
# ---------------------------------------------------------------------------

def test_explicit_net_members_kind(active_circuit):
    from circuit_synth.core.net import Net
    nets = [Net("A"), Net("B"), Net("C")]
    b = Bus("X", members=nets)
    assert b.kind == "vector"
    assert b.width == 3


def test_explicit_net_members_are_same_objects(active_circuit):
    from circuit_synth.core.net import Net
    n0, n1 = Net("P"), Net("Q")
    b = Bus("X", members=[n0, n1])
    assert b[0] is n0
    assert b[1] is n1


# ---------------------------------------------------------------------------
# Indexing and iteration
# ---------------------------------------------------------------------------

def test_int_index_returns_net(active_circuit):
    b = Bus("DATA", 4)
    net = b[0]
    assert net.name == "DATA0"


def test_int_index_last(active_circuit):
    b = Bus("DATA", 4)
    assert b[3].name == "DATA3"


def test_int_index_out_of_range_raises(active_circuit):
    b = Bus("DATA", 2)
    with pytest.raises(IndexError):
        _ = b[5]


def test_aliased_string_index_by_member_name(active_circuit):
    b = Bus("SPI", members=["SCK", "MISO", "MOSI", "CS"])
    assert b["SCK"] is b[0]
    assert b["CS"] is b[3]


def test_plain_bus_string_index_raises(active_circuit):
    b = Bus("DATA", 4)
    with pytest.raises(KeyError):
        _ = b["SCK"]


def test_aliased_unknown_string_key_raises(active_circuit):
    b = Bus("SPI", members=["SCK", "MISO"])
    with pytest.raises(KeyError):
        _ = b["NONEXISTENT"]


def test_len(active_circuit):
    b = Bus("DATA", 6)
    assert len(b) == 6


def test_iter_yields_all_members(active_circuit):
    b = Bus("DATA", 3)
    nets = list(b)
    assert [n.name for n in nets] == ["DATA0", "DATA1", "DATA2"]


def test_repr(active_circuit):
    b = Bus("FMC_D", 4)
    assert repr(b) == "Bus(FMC_D[0..3])"


# ---------------------------------------------------------------------------
# Circuit registration
# ---------------------------------------------------------------------------

def test_bus_registers_on_circuit(active_circuit):
    b = Bus("SPI", members=["SCK", "MISO"])
    assert b in active_circuit._buses


def test_bus_outside_circuit_raises():
    # No active circuit — Bus.__init__ creates Net objects which require one
    assert set_current_circuit(None) is None  # ensure no active circuit
    with pytest.raises(CircuitSynthError):
        Bus("DATA", 4)
