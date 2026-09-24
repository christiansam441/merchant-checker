from pathlib import Path

import pytest

from app.checklist import build_checklist, detect_platform, load_checklist_rules
from app.fetcher import FetchedPage
from app.models import Evidence, ProcessorResult


FIXTURES = Path(__file__).parent / "fixtures"


def fixture_page(filename: str) -> FetchedPage:
    return FetchedPage(
        url="https://merchant.example/",
        html=(FIXTURES / filename).read_text(encoding="utf-8"),
        headers={},
        cookies={},
    )


def processor(name: str) -> ProcessorResult:
    return ProcessorResult(
        name=name,
        confidence="high",
        evidence=[
            Evidence(
                signal_type="script_src",
                matched_value=f"https://cdn.example/{name.casefold()}.js",
                strength=90,
                label="Processor script",
                source_page="https://merchant.example/checkout",
            )
        ],
    )


def test_compliant_merchant_passes_every_checklist_item() -> None:
    items, platform = build_checklist(
        [fixture_page("compliant_merchant.html")],
        [processor("Processor One"), processor("Processor Two")],
        load_checklist_rules(),
    )

    assert platform == "Shopify"
    assert {item.key for item in items} == {
        "refund_policy",
        "terms",
        "privacy_policy",
        "contact_information",
        "shipping_policy",
        "crypto_payments",
        "ecommerce_platform",
        "multiple_processors",
    }
    assert all(item.passed for item in items)
    assert all(item.evidence for item in items)
    assert all(evidence.source_page for item in items for evidence in item.evidence)


def test_bare_merchant_fails_every_checklist_item() -> None:
    items, platform = build_checklist(
        [fixture_page("bare_merchant.html")],
        [],
        load_checklist_rules(),
    )

    assert platform is None
    assert all(not item.passed for item in items)
    assert all(not item.evidence for item in items)


@pytest.mark.parametrize(
    ("name", "html"),
    [
        ("Shopify", '<script src="https://cdn.shopify.com/store.js"></script>'),
        ("WooCommerce", '<script src="/wp-content/plugins/woocommerce/cart.js"></script>'),
        ("BigCommerce", '<script src="https://cdn11.bigcommerce.com/store/app.js"></script>'),
        ("Wix", '<meta name="generator" content="Wix.com Website Builder">'),
        ("Squarespace", '<script src="https://static1.squarespace.com/app.js"></script>'),
        ("Magento", '<script src="/static/version123/frontend/theme/app.js"></script>'),
    ],
)
def test_platform_detection(name: str, html: str) -> None:
    detected, evidence = detect_platform(
        [FetchedPage(url="https://merchant.example/", html=html, headers={}, cookies={})],
        load_checklist_rules(),
    )

    assert detected == name
    assert evidence


def test_platform_detection_returns_none_for_bare_page() -> None:
    detected, evidence = detect_platform(
        [fixture_page("bare_merchant.html")],
        load_checklist_rules(),
    )

    assert detected is None
    assert evidence == []
