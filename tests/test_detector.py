from pathlib import Path

import pytest

from app.detector import detect_processors
from app.fetcher import FetchedPage
from app.rules import load_rules


FIXTURES = Path(__file__).parent / "fixtures"


def fixture_page(filename: str) -> FetchedPage:
    return FetchedPage(
        url="https://merchant.example/",
        html=(FIXTURES / filename).read_text(encoding="utf-8"),
        headers={},
        cookies={},
    )


@pytest.mark.parametrize(
    ("filename", "processor_name", "confidence"),
    [
        ("stripe.html", "Stripe", "high"),
        ("paypal.html", "PayPal", "high"),
        ("braintree.html", "Braintree", "high"),
        ("square.html", "Square", "high"),
        ("shopify_payments.html", "Shopify Payments", "low"),
        ("adyen.html", "Adyen", "high"),
        ("authorize_net.html", "Authorize.Net", "high"),
        ("klarna.html", "Klarna", "high"),
        ("affirm.html", "Affirm", "high"),
        ("afterpay.html", "Afterpay", "high"),
        ("amazon_pay.html", "Amazon Pay", "high"),
        ("apple_pay.html", "Apple Pay", "high"),
        ("google_pay.html", "Google Pay", "high"),
        ("checkout_com.html", "Checkout.com", "high"),
        ("razorpay.html", "Razorpay", "high"),
        ("mollie.html", "Mollie", "high"),
        ("worldpay.html", "Worldpay", "high"),
        ("two_checkout.html", "2Checkout/Verifone", "high"),
        ("paddle.html", "Paddle", "high"),
    ],
)
def test_processor_fixture_is_detected(
    filename: str, processor_name: str, confidence: str
) -> None:
    results = detect_processors(fixture_page(filename), load_rules())

    assert len(results) == 1
    assert results[0].name == processor_name
    assert results[0].confidence == confidence
    assert results[0].evidence
    assert results[0].evidence[0].matched_value


def test_page_without_processor_signals_returns_no_results() -> None:
    assert detect_processors(fixture_page("no_processors.html"), load_rules()) == []


def test_generic_accept_and_javascript_terms_do_not_detect_authorize_net() -> None:
    names = {
        result.name
        for result in detect_processors(fixture_page("generic_terms.html"), load_rules())
    }

    assert "Authorize.Net" not in names
    assert "Checkout.com" not in names
    assert "Square" not in names


def test_apple_pay_text_mention_is_low_confidence_only() -> None:
    results = detect_processors(fixture_page("apple_pay_mention.html"), load_rules())

    assert len(results) == 1
    assert results[0].name == "Apple Pay"
    assert results[0].confidence == "low"


def test_klarna_text_mention_is_not_a_confident_detection() -> None:
    results = detect_processors(fixture_page("klarna_mention.html"), load_rules())

    assert not results or all(result.confidence == "low" for result in results)


def test_blog_post_about_stripe_is_not_a_confident_detection() -> None:
    results = detect_processors(fixture_page("stripe_blog.html"), load_rules())

    assert not results or all(result.confidence == "low" for result in results)
