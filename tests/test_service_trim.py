"""`service` is trimmed, so it stays a usable join key with simsys-logevent.

An operator is expected to pivot from
``simsys_http_requests_total{service="portal"}`` in Prometheus to
``{service="portal"} | json`` in Loki using the same label value. Both
packages therefore have to agree on what that value IS. simsys-logevent
trims its own copy; before this, metrics did not, so ``"  portal  "`` here
and ``"portal"`` there were two identities and the pivot returned nothing --
silently, because an empty Loki result looks exactly like a quiet service.

Trimming was made a non-deferred change on measured evidence rather than
assumption: all 36 services reporting ``simsys_build_info`` were queried
from live Prometheus on 2026-08-22 and none carried leading or trailing
whitespace, so no deployed series is renamed by this.
"""

from __future__ import annotations

import logging

import pytest

from simsys_metrics import get_service, set_service
from simsys_metrics._baseline import _reset_for_tests


@pytest.fixture(autouse=True)
def _clean():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_surrounding_whitespace_is_stripped():
    set_service("  portal  ")
    assert get_service() == "portal"


def test_padded_and_unpadded_are_the_same_identity():
    """The actual join-breaking scenario, asserted directly."""
    set_service("  portal  ")
    padded = get_service()
    _reset_for_tests()
    set_service("portal")
    assert padded == get_service(), (
        "A padded and an unpadded spelling of the same service must resolve "
        "to one identity, or the Prometheus->Loki pivot on `service` breaks."
    )


def test_internal_whitespace_is_preserved():
    """Only the ends are trimmed.

    Negative control: if this passed while stripping internal spaces too, the
    trim would be silently rewriting service names rather than normalizing
    their edges.
    """
    set_service("  my service  ")
    assert get_service() == "my service"


def test_ordinary_service_name_is_untouched():
    """The no-op case -- what every one of the 36 live services looks like."""
    set_service("portal")
    assert get_service() == "portal"


def test_whitespace_padding_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="simsys_metrics"):
        set_service("  portal  ")
    assert any("whitespace" in r.getMessage() for r in caplog.records), (
        f"expected a whitespace warning, got: {[r.getMessage() for r in caplog.records]}"
    )


def test_all_whitespace_service_warns_but_is_still_set(caplog):
    """Warn-now, raise-in-the-next-major.

    Asserting the value is STILL SET is the point: it pins the current
    lenient behavior so that promoting this to an exception later is a
    deliberate edit to this test, not an accident.
    """
    with caplog.at_level(logging.WARNING, logger="simsys_metrics"):
        set_service("   ")
    assert get_service() == ""
    assert any("empty after stripping" in r.getMessage() for r in caplog.records), (
        f"expected an empty-after-strip warning, got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_clearing_with_none_is_unaffected(caplog):
    """install() rollback passes None; it must not warn or crash."""
    set_service("portal")
    with caplog.at_level(logging.WARNING, logger="simsys_metrics"):
        set_service(None)
    assert not caplog.records, (
        f"clearing the service must be silent, got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    with pytest.raises(RuntimeError):
        get_service()
