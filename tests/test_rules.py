import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.rules import DEFAULT_RULES_PATH, RulesValidationError, load_rules


@pytest.fixture
def valid_rules_data() -> dict:
    return json.loads(DEFAULT_RULES_PATH.read_text(encoding="utf-8"))


def write_rules(tmp_path: Path, rules: dict) -> Path:
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(json.dumps(rules), encoding="utf-8")
    return rules_path


def test_valid_rules_load() -> None:
    rules = load_rules()

    assert [processor.name for processor in rules.processors] == [
        "Stripe",
        "PayPal",
        "Braintree",
        "Square",
        "Shopify Payments",
        "Adyen",
        "Authorize.Net",
        "Klarna",
        "Affirm",
        "Afterpay",
        "Amazon Pay",
        "Apple Pay",
        "Google Pay",
        "Checkout.com",
        "Razorpay",
        "Mollie",
        "Worldpay",
        "2Checkout/Verifone",
        "Paddle",
    ]


def test_missing_field_is_rejected(tmp_path: Path, valid_rules_data: dict) -> None:
    malformed = deepcopy(valid_rules_data)
    del malformed["processors"][0]["signals"][0]["pattern"]

    with pytest.raises(RulesValidationError, match=r"pattern: Field required"):
        load_rules(write_rules(tmp_path, malformed))


def test_unknown_signal_type_is_rejected(tmp_path: Path, valid_rules_data: dict) -> None:
    malformed = deepcopy(valid_rules_data)
    malformed["processors"][0]["signals"][0]["type"] = "network_request"

    with pytest.raises(RulesValidationError, match=r"type: Input should be"):
        load_rules(write_rules(tmp_path, malformed))


def test_invalid_regex_is_rejected(tmp_path: Path, valid_rules_data: dict) -> None:
    malformed = deepcopy(valid_rules_data)
    malformed["processors"][0]["signals"][0]["pattern"] = "[unclosed"

    with pytest.raises(RulesValidationError, match=r"invalid regex"):
        load_rules(write_rules(tmp_path, malformed))


@pytest.mark.parametrize("weight", [0, 101])
def test_weight_out_of_range_is_rejected(
    tmp_path: Path, valid_rules_data: dict, weight: int
) -> None:
    malformed = deepcopy(valid_rules_data)
    malformed["processors"][0]["signals"][0]["weight"] = weight

    with pytest.raises(RulesValidationError, match=r"weight: Input should be"):
        load_rules(write_rules(tmp_path, malformed))
