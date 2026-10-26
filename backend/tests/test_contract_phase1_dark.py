"""Item 2: contract additions for Phase 1 built dark (decisions 24, 25, 27)."""
from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app import domain as d
from app.composition import pinned_runtime_config

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


def test_saved_place_purpose_and_effect_only() -> None:
    assert d.ConsentPurpose.SAVED_PLACE == "SAVED_PLACE"
    assert {e.value for e in d.PendingActionEffect} == {"SET_REMINDER", "LAUNCH_HANDOFF", "SAVE_PLACE"}
    for later in ("CREATE_ROUTINE", "ENABLE_NOTIFICATIONS"):
        assert later not in d.PendingActionEffect.__members__
        with pytest.raises(ValueError):
            d.PendingActionEffect(later)


def test_effect_comment_cites_decision_25() -> None:
    assert "decision 25" in inspect.getsource(d.PendingActionEffect).lower()


def test_saved_place_rounds_and_redacts() -> None:
    place = d.SavedPlace(place_id="ChIJabc", user_label="Home", label_kind=d.SavedPlaceKind.HOME,
                         position=d.RoundedPosition(latitude=43.653226, longitude=-79.383184),
                         created_at=NOW)
    assert (place.position.latitude, place.position.longitude) == (43.653, -79.383)
    for text in (repr(place), str(place), repr(place.position), str(place.position)):
        assert "43.65" not in text and "79.38" not in text
    assert place.kind == "saved_place" and place.schema_version == 1
    with pytest.raises(ValidationError):
        place.user_label = "x"  # frozen
    with pytest.raises(ValidationError):
        d.SavedPlace(place_id="p", user_label="Home", label_kind="HOME", created_at=NOW,
                     position=d.RoundedPosition(latitude=1, longitude=1), extra=1)


def test_saved_place_rejects_address_like_labels_and_bad_kind() -> None:
    kw = dict(place_id="p", position=d.RoundedPosition(latitude=1, longitude=1), created_at=NOW)
    with pytest.raises(ValidationError):
        d.SavedPlace(user_label="123 Queen Street", label_kind="CUSTOM", **kw)
    with pytest.raises(ValidationError):
        d.SavedPlace(user_label="Gym", label_kind="SCHOOL", **kw)


def test_position_validation_errors_do_not_echo_input() -> None:
    with pytest.raises(ValidationError) as err:
        d.RoundedPosition(latitude=123.456789, longitude=1)
    assert "123.456789" not in str(err.value)


def _display_only(model: type) -> set[str]:
    return {n for n, f in model.model_fields.items()
            if (f.json_schema_extra or {}).get("x-display-only") is True}


def test_place_card_marks_every_maps_field_display_only() -> None:
    card = d.PlaceCard(place_id="ChIJabc", display_name="Cafe Example", open_now=True,
                       attributions=("Google Maps",), fetched_at=NOW)
    assert card.display_only is True
    maps_fields = set(d.PlaceCard.model_fields) - {
        "schema_version", "kind", "place_id", "display_only", "fetched_at"}
    assert maps_fields <= _display_only(d.PlaceCard)
    assert "attributions" in d.PlaceCard.model_fields
    assert not {"latitude", "longitude", "location", "reviews", "photos"} & set(d.PlaceCard.model_fields)


def test_route_check_modes_and_beta_warning() -> None:
    drive = d.RouteCheck(place_id="p", mode=d.RouteMode.DRIVE, duration_minutes=12,
                         distance_meters=4000, beta_warning=False, attributions=("Google Maps",),
                         fetched_at=NOW)
    assert drive.mode == "DRIVE" and drive.display_only is True
    walk = d.RouteCheck(place_id="p", mode="WALK", duration_minutes=40, distance_meters=3000,
                        beta_warning=True, attributions=("Google Maps",), fetched_at=NOW)
    assert walk.beta_warning is True
    with pytest.raises(ValidationError):
        d.RouteCheck(place_id="p", mode="WALK", duration_minutes=40, distance_meters=3000,
                     beta_warning=False, attributions=(), fetched_at=NOW)
    with pytest.raises(ValidationError):
        d.RouteCheck(place_id="p", mode="TRANSIT", duration_minutes=1, distance_meters=1,
                     beta_warning=False, attributions=(), fetched_at=NOW)
    for forbidden in ("polyline", "geometry", "steps", "origin", "legs"):
        assert forbidden not in d.RouteCheck.model_fields
    assert {"duration_minutes", "distance_meters"} <= _display_only(d.RouteCheck)


def test_outcome_checkin() -> None:
    ok = d.OutcomeCheckin(place_id="p", outcome="WENT", note="lovely")
    assert ok.outcome is d.OutcomeValue.WENT
    assert {o.value for o in d.OutcomeValue} == {"WENT", "DID_NOT_GO", "CHANGED_PLAN"}
    assert d.OutcomeCheckin(place_id="p", outcome="DID_NOT_GO").note is None
    with pytest.raises(ValidationError):
        d.OutcomeCheckin(place_id="p", outcome="WENT", note="x" * 1000)
    with pytest.raises(ValidationError):
        d.OutcomeCheckin(place_id="p", outcome="MAYBE")
    assert "lovely" not in repr(ok)


def test_account_export_models_are_strict() -> None:
    link = d.AccountLinkIntent(id="intent1", provider="GOOGLE", nonce="n" * 24, created_at=NOW,
                               expires_at=NOW + timedelta(minutes=10))
    assert link.kind == "account_link_intent"
    with pytest.raises(ValidationError):
        d.AccountLinkIntent(id="i", provider="GOOGLE", nonce="n" * 24, created_at=NOW,
                            expires_at=NOW + timedelta(days=1))
    bundle = d.ExportBundle(generated_at=NOW, consents=(), saved_places=(), outings=())
    assert bundle.kind == "export_bundle" and bundle.schema_version == 1
    with pytest.raises(ValidationError):
        d.ExportBundle(generated_at=NOW, consents=(), saved_places=(), outings=(), uid="x")


def test_runtime_config_history_flags_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    config = pinned_runtime_config()
    assert config.history_enabled is False and config.history_notice_version is None
    assert d.history_active(config) is False and d.history_active(None) is False
    on_no_notice = d.evolve(config, history_enabled=True)
    assert d.history_active(on_no_notice) is False
    on_unapproved = d.evolve(config, history_enabled=True, history_notice_version="history-notice-1")
    assert d.history_active(on_unapproved) is False  # no notice version is approved by default
    monkeypatch.setattr(d, "APPROVED_HISTORY_NOTICE_VERSIONS", frozenset({"history-notice-1"}))
    assert d.history_active(on_unapproved) is True
    assert d.history_active(d.evolve(config, history_notice_version="history-notice-1")) is False
    with pytest.raises(ValidationError):
        config.history_enabled = True  # frozen


def test_capabilities_carry_history_enabled_default_false() -> None:
    caps = d.Capabilities(can_start_turn=True, can_use_location=True, can_read=True,
                          can_export=False, can_delete=False, can_link_account=True)
    assert caps.history_enabled is False


def test_error_reasons_added() -> None:
    assert d.ErrorReason.NOT_IMPLEMENTED == "NOT_IMPLEMENTED"
    assert d.ErrorReason.DISABLED == "DISABLED"
