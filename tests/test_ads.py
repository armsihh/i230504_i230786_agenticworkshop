"""Tool logic tests. No model calls, so they cost nothing. Run: python -m pytest -q"""
import pytest

from app import ads
from app.creatives import render_svg

GOOD = {"headline": "Fresh coffee, delivered", "body": "Roasted this week in Lahore. Order by noon for same-day delivery.",
        "cta": "Order now"}


@pytest.fixture
def camp():
    return ads.create_campaign("alice-test", "Winter brew", "Coffee beans", "Office workers 25-40",
                               "meta", "sales", 20, 7)


def test_create_and_list(camp):
    assert camp["status"] == "draft"
    assert camp["id"] in [c["id"] for c in ads.list_campaigns("alice-test")]


@pytest.mark.parametrize("kw, msg", [({"platform": "myspace"}, "Unknown platform"),
                                     ({"goal": "vibes"}, "Unknown goal"),
                                     ({"daily_budget": 50000}, "between"),
                                     ({"days": 0}, "1 to 90")])
def test_create_rejects_bad_input(kw, msg):
    args = dict(name="x", product="x", audience="x", platform="meta", goal="sales", daily_budget=10, days=5) | kw
    with pytest.raises(ads.AdError, match=msg):
        ads.create_campaign("alice-test", **args)


def test_user_isolation(camp):
    """Bob can't see, read, add to, record on or launch Alice's campaign."""
    assert camp["id"] not in [c["id"] for c in ads.list_campaigns("bob-test")]
    with pytest.raises(ads.AdError, match="No campaign"):
        ads.get_campaign("bob-test", camp["id"])
    with pytest.raises(ads.AdError):
        ads.save_variants("bob-test", camp["id"], [GOOD])
    with pytest.raises(ads.AdError):
        ads.launch("bob-test", camp["id"])
    [v] = ads.save_variants("alice-test", camp["id"], [GOOD])
    with pytest.raises(ads.AdError, match="No ad variant"):
        ads.record_results("bob-test", v["id"], 100, 5, 1, 3)
    with pytest.raises(ads.AdError):
        ads.analyze("bob-test", camp["id"])


def test_copy_checks():
    assert ads.check_copy(**GOOD, platform="meta") == []
    text = " ".join(ads.check_copy("GUARANTEED #1 RESULTS!!!", "Click here", "Buy", "meta"))
    assert "guarantee" in text and "superlatives" in text and "capital" in text and "click here" in text
    assert any("chars" in p for p in ads.check_copy("x" * 31, "ok", "Go", "google_search"))


def test_save_variants_all_or_nothing(camp):
    bad = {"headline": "Guaranteed savings", "body": "ok", "cta": "Go"}
    with pytest.raises(ads.AdError, match="variant B"):
        ads.save_variants("alice-test", camp["id"], [GOOD, bad])
    assert ads.list_variants("alice-test", camp["id"]) == []
    assert [v["label"] for v in ads.save_variants("alice-test", camp["id"], [GOOD, GOOD])] == ["A", "B"]


def test_estimate():
    e = ads.estimate(20, 10, "meta")
    assert e["total_spend"] == 200
    low, high = e["clicks"]
    assert low < 200 / 0.70 < high


def test_ab_winner_and_results_validation(camp):
    a, b = ads.save_variants("alice-test", camp["id"], [GOOD, GOOD])
    ads.record_results("alice-test", a["id"], 10000, 500, 60, 100)
    ads.record_results("alice-test", b["id"], 10000, 500, 20, 100)
    assert ads.analyze("alice-test", camp["id"])["winner"]["label"] == "A"   # sales goal: conversion rate
    with pytest.raises(ads.AdError, match="exceed"):
        ads.record_results("alice-test", a["id"], 10, 50, 0, 1)


def test_ab_no_winner_with_little_data(camp):
    a, b = ads.save_variants("alice-test", camp["id"], [GOOD, GOOD])
    ads.record_results("alice-test", a["id"], 100, 10, 2, 5)
    ads.record_results("alice-test", b["id"], 100, 9, 1, 5)
    assert ads.analyze("alice-test", camp["id"])["winner"] is None


def test_launch(camp):
    with pytest.raises(ads.AdError, match="at least one"):
        ads.launch("alice-test", camp["id"])
    ads.save_variants("alice-test", camp["id"], [GOOD])
    ads.launch("alice-test", camp["id"])
    assert ads.get_campaign("alice-test", camp["id"])["status"] == "active"


def test_creative_svg_escapes_text():
    svg = render_svg("<script>alert(1)</script>", "body", "Go")
    assert svg.startswith("<svg") and "<script>" not in svg
