"""Edge cases and hostile inputs for the tool logic, plus property-based fuzzing."""
import math
import xml.etree.ElementTree as ET

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app import ads
from app.creatives import SIZES, THEMES, render_svg

U = "edge-user"
GOOD = {"headline": "Fresh coffee, delivered", "body": "Roasted this week. Order by noon.", "cta": "Order now"}


def new_camp(**kw):
    args = dict(name="C", product="Coffee", audience="Office workers", platform="meta", goal="sales",
                daily_budget=20, days=7) | kw
    return ads.create_campaign(U, **args)


# ── numbers that break naive validation ──

@pytest.mark.parametrize("budget", [float("nan"), float("inf"), -5, 0, 0.5, 1000.01])
def test_bad_budgets_rejected(budget):
    with pytest.raises(ads.AdError):
        new_camp(daily_budget=budget)


@pytest.mark.parametrize("days", [0, -1, 91, 10**9])
def test_bad_days_rejected(days):
    with pytest.raises(ads.AdError):
        new_camp(days=days)


@pytest.mark.parametrize("budget, days", [(float("nan"), 5), (20, float("nan")), (float("inf"), 5), (-1, 5)])
def test_estimate_rejects_bad_numbers(budget, days):
    with pytest.raises(ads.AdError):
        ads.estimate(budget, days, "meta")


@pytest.mark.parametrize("nums", [
    (float("nan"), 10, 1, 5), (100, 10, 1, float("nan")), (100, 10, 1, float("inf")),
    (-1, 0, 0, 0), (100, 10, 1, -5), (100, 101, 0, 1), (100, 10, 11, 1),
])
def test_bad_results_rejected(nums):
    c = new_camp()
    [v] = ads.save_variants(U, c["id"], [GOOD])
    with pytest.raises(ads.AdError):
        ads.record_results(U, v["id"], *nums)


def test_all_zero_results_dont_crash_analysis():
    c = new_camp()
    a, b = ads.save_variants(U, c["id"], [GOOD, GOOD])
    ads.record_results(U, a["id"], 0, 0, 0, 0)
    ads.record_results(U, b["id"], 0, 0, 0, 0)
    report = ads.analyze(U, c["id"])
    assert report["winner"] is None


def test_identical_variants_have_no_winner():
    c = new_camp()
    a, b = ads.save_variants(U, c["id"], [GOOD, GOOD])
    for v in (a, b):
        ads.record_results(U, v["id"], 50000, 2000, 100, 300)
    assert ads.analyze(U, c["id"])["winner"] is None


def test_winner_needs_enough_data_even_with_big_gap():
    c = new_camp()
    a, b = ads.save_variants(U, c["id"], [GOOD, GOOD])
    ads.record_results(U, a["id"], 20, 4, 2, 1)
    ads.record_results(U, b["id"], 20, 4, 0, 1)
    assert ads.analyze(U, c["id"])["winner"] is None   # 2 sales vs 0 is noise, not a winner


def test_awareness_goal_judged_on_ctr():
    c = new_camp(goal="awareness")
    a, b = ads.save_variants(U, c["id"], [GOOD, GOOD])
    ads.record_results(U, a["id"], 20000, 200, 0, 50)
    ads.record_results(U, b["id"], 20000, 600, 0, 50)
    assert ads.analyze(U, c["id"])["winner"]["label"] == "B"


# ── text that breaks naive storage ──

def test_huge_text_fields_rejected():
    with pytest.raises(ads.AdError):
        new_camp(name="x" * 10_000)
    with pytest.raises(ads.AdError):
        new_camp(product="x" * 10_000)


@pytest.mark.parametrize("field", ["name", "product", "audience"])
def test_blank_text_fields_rejected(field):
    with pytest.raises(ads.AdError):
        new_camp(**{field: "   "})


def test_sql_injection_in_ids_is_harmless():
    new_camp()
    for evil in ["x' OR '1'='1", "'; DROP TABLE campaigns; --", "%", "_"]:
        with pytest.raises(ads.AdError):
            ads.get_campaign(U, evil)
    assert ads.list_campaigns(U)   # table still there


def test_unicode_and_emoji_copy_round_trips():
    c = new_camp()
    copy = {"headline": "گرم کافی ☕ آج", "body": "تازہ کافی، اسی ہفتے روسٹ کی گئی 🔥", "cta": "ابھی آرڈر کریں"}
    [v] = ads.save_variants(U, c["id"], [copy])
    stored = ads.get_variant(U, v["id"])
    assert (stored["headline"], stored["body"], stored["cta"]) == (copy["headline"], copy["body"], copy["cta"])


def test_variant_count_per_campaign_is_capped():
    c = new_camp()
    for _ in range(2):
        ads.save_variants(U, c["id"], [GOOD] * 5)
    with pytest.raises(ads.AdError):
        ads.save_variants(U, c["id"], [GOOD])   # an 11th variant: labels would run past A-J
    assert [v["label"] for v in ads.list_variants(U, c["id"])] == list("ABCDEFGHIJ")


def test_platform_and_goal_normalised():
    c = new_camp(platform="  Instagram ", goal="SALES")
    assert (c["platform"], c["goal"]) == ("instagram", "sales")


def test_cannot_launch_twice_or_launch_empty():
    c = new_camp()
    with pytest.raises(ads.AdError):
        ads.launch(U, c["id"])
    ads.save_variants(U, c["id"], [GOOD])
    ads.launch(U, c["id"])
    with pytest.raises(ads.AdError, match="already"):
        ads.launch(U, c["id"])


# ── policy check edge cases ──

@pytest.mark.parametrize("headline", [
    "G u a r a n t e e d results", "Guaranteed!", "GUARANTEE", "#1 coffee", "No.1 coffee", "Number one coffee",
    "Cures headaches", "100% organic results", "Risk-free trial",
])
def test_banned_claims_caught(headline):
    assert ads.check_copy(headline, "Fresh coffee.", "Shop now", "meta"), headline


@pytest.mark.parametrize("headline", ["IKEA-style shelves", "New iPhone cases", "Fresh coffee in G-11",
                                      "Best seller this month?", "Our 1st anniversary sale"])
def test_normal_copy_not_flagged(headline):
    assert ads.check_copy(headline, "Visit our store in Islamabad.", "Shop now", "meta") == [], headline


# ── fuzzing ──

copy_text = st.text(max_size=300)


@settings(max_examples=300, deadline=None)
@given(copy_text, copy_text, copy_text, st.sampled_from(list(ads.PLATFORMS) + ["nope", ""]))
def test_check_copy_never_crashes(h, b, c, p):
    assert isinstance(ads.check_copy(h, b, c, p), list)


@settings(max_examples=300, deadline=None)
@given(copy_text, copy_text, st.text(max_size=40), st.sampled_from(list(SIZES)), st.sampled_from(list(THEMES)))
def test_creative_is_always_valid_svg(h, b, c, size, theme):
    ET.fromstring(render_svg(h, b, c, size, theme))   # raises if the XML is broken


@settings(max_examples=200, deadline=None)
@given(st.floats(min_value=1, max_value=1000), st.integers(min_value=1, max_value=90),
       st.sampled_from(list(ads.PLATFORMS)))
def test_estimate_is_sane(budget, days, platform):
    e = ads.estimate(budget, days, platform)
    assert math.isclose(e["total_spend"], round(budget * days, 2))
    for key in ("impressions", "clicks", "conversions"):
        low, high = e[key]
        assert 0 <= low <= high
    assert e["clicks"][1] <= e["impressions"][1]
