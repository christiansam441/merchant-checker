import asyncio

import pytest

from app.checklist import load_checklist_rules
from app.detector import detect_processors
from app.fetcher import FetchError, FetchedPage, TargetSiteBlocked
from app.models import Evidence, ProcessorResult
from app.rules import load_rules
from app.scanner import find_commerce_links, find_scan_links, merge_processor_results, scan_events


def page(url: str, html: str) -> FetchedPage:
    return FetchedPage(url=url, html=html, headers={}, cookies={})


def test_commerce_links_are_prioritized_deduplicated_and_limited() -> None:
    homepage = page(
        "https://merchant.example/",
        """
        <a href="/about">About</a>
        <a href="/shop/cart">Cart</a>
        <a href="/pricing">Pricing</a>
        <a href="/pricing#monthly">Pricing duplicate</a>
        <a href="https://other.example/checkout">External checkout</a>
        <a href="/catalog.pdf">Store catalog PDF</a>
        <a href="/subscribe">Subscribe</a>
        <a href="/donate">Donate</a>
        <a href="/store">Store</a>
        <a href="/buy">Buy</a>
        """,
    )

    assert find_commerce_links(homepage) == [
        "https://merchant.example/shop/cart",
        "https://merchant.example/pricing",
        "https://merchant.example/subscribe",
        "https://merchant.example/donate",
        "https://merchant.example/store",
    ]


def test_evidence_is_merged_across_pages_and_confidence_is_recomputed() -> None:
    first = ProcessorResult(
        name="Example Pay",
        confidence="low",
        evidence=[
            Evidence(
                signal_type="page_text",
                matched_value="Example Pay",
                strength=30,
                label="Payment text",
                source_page="https://merchant.example/",
            )
        ],
    )
    second = ProcessorResult(
        name="Example Pay",
        confidence="medium",
        evidence=[
            Evidence(
                signal_type="script_src",
                matched_value="https://cdn.example/pay.js",
                strength=60,
                label="Payment script",
                source_page="https://merchant.example/checkout",
            )
        ],
    )

    merged = merge_processor_results([[first], [second]])

    assert len(merged) == 1
    assert merged[0].confidence == "high"
    assert {item.source_page for item in merged[0].evidence} == {
        "https://merchant.example/",
        "https://merchant.example/checkout",
    }


def test_scan_links_include_policy_and_contact_pages_with_eight_page_total_cap() -> None:
    links = "".join(
        f'<a href="/{name}">{name}</a>'
        for name in [
            "checkout",
            "cart",
            "pricing",
            "shop",
            "refund-policy",
            "terms",
            "privacy",
            "contact",
            "shipping",
        ]
    )

    selected = find_scan_links(page("https://merchant.example/", links), load_checklist_rules())

    assert len(selected) == 7
    assert "https://merchant.example/refund-policy" in selected
    assert "https://merchant.example/terms" in selected


def test_failed_secondary_page_does_not_break_scan(monkeypatch) -> None:
    homepage = page(
        "https://merchant.example/",
        '<a href="/checkout">Checkout</a><script src="https://js.stripe.com/v3/"></script>',
    )

    async def fake_validate(url: str):
        return url

    async def fake_fetch(url: str) -> FetchedPage:
        if url.endswith("/checkout"):
            raise FetchError("secondary page unavailable")
        return homepage

    monkeypatch.setattr("app.scanner.validate_public_url", fake_validate)

    async def collect_events():
        return [
            event
            async for event in scan_events(
                homepage.url,
                load_rules(),
                load_checklist_rules(),
                fake_fetch,
            )
        ]

    events = asyncio.run(collect_events())

    assert any("Skipped" in event.message for event in events)
    assert events[-1].event == "result"
    assert events[-1].result is not None
    assert events[-1].result.processors[0].name == "Stripe"
    assert detect_processors(homepage, load_rules())[0].evidence[0].source_page == homepage.url


def test_scan_stops_at_total_time_limit(monkeypatch) -> None:
    async def fake_validate(url: str):
        return url

    async def slow_fetch(url: str) -> FetchedPage:
        await asyncio.sleep(0.05)
        return page(url, "<html></html>")

    monkeypatch.setattr("app.scanner.validate_public_url", fake_validate)

    async def collect_events():
        return [
            event
            async for event in scan_events(
                "https://merchant.example/",
                load_rules(),
                load_checklist_rules(),
                slow_fetch,
                timeout_seconds=0.01,
            )
        ]

    events = asyncio.run(collect_events())

    assert events[-1].event == "error"
    assert "time limit" in events[-1].message
    assert events[-1].progress == 15


@pytest.mark.parametrize("status_code", [403, 429, 503])
def test_homepage_target_block_has_category_and_stops_at_fetch_progress(
    monkeypatch, status_code: int
) -> None:
    async def fake_validate(url: str):
        return url

    async def blocked_fetch(url: str) -> FetchedPage:
        raise TargetSiteBlocked(status_code)

    monkeypatch.setattr("app.scanner.validate_public_url", fake_validate)

    async def collect_events():
        return [
            event
            async for event in scan_events(
                "https://merchant.example/",
                load_rules(),
                load_checklist_rules(),
                blocked_fetch,
            )
        ]

    event = asyncio.run(collect_events())[-1]

    assert event.event == "error"
    assert event.error_category == "blocked_by_site"
    assert event.progress == 15
    assert f"HTTP {status_code}" in event.message


def test_repeated_weak_matches_are_counted_once_and_separated_as_mentions(monkeypatch) -> None:
    homepage = page(
        "https://merchant.example/",
        '<a href="/pricing">Pricing</a><p>Apple Pay is mentioned here.</p>',
    )
    pricing = page(
        "https://merchant.example/pricing",
        "<p>Our article also mentions Apple Pay.</p>",
    )

    async def fake_validate(url: str):
        return url

    async def fake_fetch(url: str) -> FetchedPage:
        return pricing if url.endswith("/pricing") else homepage

    monkeypatch.setattr("app.scanner.validate_public_url", fake_validate)

    async def collect_events():
        return [
            event
            async for event in scan_events(
                homepage.url,
                load_rules(),
                load_checklist_rules(),
                fake_fetch,
            )
        ]

    result = asyncio.run(collect_events())[-1].result

    assert result is not None
    assert result.processors == []
    assert len(result.possible_mentions) == 1
    assert result.possible_mentions[0].name == "Apple Pay"
    assert len(result.possible_mentions[0].evidence) == 1
