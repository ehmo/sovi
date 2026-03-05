"""Tests for the canonical BUNDLE_IDS map and its consumers."""

from __future__ import annotations

from sovi.device.wda_client import BUNDLE_IDS


def test_all_platforms_have_bundle_ids():
    """Every platform in the DB enum should have a bundle ID."""
    required = {"tiktok", "instagram", "youtube_shorts", "reddit", "x_twitter", "facebook", "linkedin"}
    assert required.issubset(BUNDLE_IDS.keys())


def test_bundle_ids_are_reverse_dns():
    """Bundle IDs should be valid reverse-DNS format."""
    for name, bid in BUNDLE_IDS.items():
        assert "." in bid, f"{name}: {bid} is not reverse-DNS"
        assert bid.startswith("com."), f"{name}: {bid} doesn't start with com."


def test_aliases_point_to_same_bundle():
    """DB aliases should resolve to the same bundle as their canonical name."""
    assert BUNDLE_IDS["youtube_shorts"] == BUNDLE_IDS["youtube"]
    assert BUNDLE_IDS["x_twitter"] == BUNDLE_IDS["twitter"]


def test_app_lifecycle_bundles_is_same_object():
    """app_lifecycle.BUNDLES should be the same dict as BUNDLE_IDS."""
    from sovi.device.app_lifecycle import BUNDLES
    assert BUNDLES is BUNDLE_IDS
