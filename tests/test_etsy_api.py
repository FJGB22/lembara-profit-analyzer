"""Etsy API tests use a fake HTTP function, so they run without network or credentials.
Response shapes follow Etsy's OpenAPI spec (ShopReceipt, ShopReceiptTransaction, Money, Self)."""

import urllib.parse
from pathlib import Path

import pandas as pd
import pytest

from etsy_profit import analyze, load_shop
from etsy_profit.etsy_api import (
    EtsyApiError,
    EtsyClient,
    EtsyToken,
    authorization_url,
    code_challenge,
    code_from_redirect,
    exchange_code,
    fetch_etsy_orders,
    receipts_to_items,
)
from etsy_profit.etsy_import import ALREADY_IMPORTED, READY, UNKNOWN_SKU, merge_ready_items

SAMPLE = Path(__file__).parent.parent / "data" / "sample"


def usd(cents: int) -> dict:
    return {"amount": cents, "divisor": 100, "currency_code": "USD"}


def receipt(receipt_id, created, lines, shipping=0, discount=0, status="Paid"):
    return {
        "receipt_id": receipt_id,
        "create_timestamp": created,
        "status": status,
        "total_shipping_cost": usd(shipping),
        "discount_amt": usd(discount),
        "transactions": [
            {"sku": sku, "quantity": qty, "price": usd(price), "title": title}
            for sku, qty, price, title in lines
        ],
    }


NOV_3 = int(pd.Timestamp("2026-11-03 15:30").timestamp())
RECEIPTS = [
    receipt(4001, NOV_3, [("FW-001", 2, 2200, "Lavender"), ("FW-007", 1, 600, "Matchbox")], shipping=450, discount=440),
    receipt(4002, NOV_3, [("FW-003", 1, 3000, "Citrus")]),
    receipt(4003, NOV_3, [("FW-002", 1, 2400, "Cedar")], status="Canceled"),
    receipt(4004, NOV_3, [("FW-999", 1, 1000, "Mystery")]),
]


class FakeEtsy:
    def __init__(self, receipts, page_size=2):
        self.receipts = receipts
        self.page_size = page_size
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        parsed = urllib.parse.urlparse(url)
        if parsed.path.endswith("/oauth/token"):
            fields = urllib.parse.parse_qs(body.decode())
            assert fields["grant_type"][0] in {"authorization_code", "refresh_token"}
            return {"access_token": "123.new", "refresh_token": "123.refresh2", "expires_in": 3600}
        assert headers["x-api-key"] == "key:secret"
        if parsed.path.endswith("/users/me"):
            return {"user_id": 123, "shop_id": 777}
        if parsed.path.endswith("/shops/777/receipts"):
            query = urllib.parse.parse_qs(parsed.query)
            offset = int(query["offset"][0])
            return {"count": len(self.receipts), "results": self.receipts[offset:offset + self.page_size]}
        raise AssertionError(f"unexpected call {method} {url}")


def fresh_token():
    return EtsyToken("123.abc", "123.refresh", expires_at=10**12)


def test_pkce_matches_etsy_documentation_example():
    assert code_challenge("vvkdljkejllufrvbhgeiegrnvufrhvrffnkvcknjvfid") == "DSWlW2Abh-cf8CeLL8-g3hQ2WQyYdKyiu83u_s7nRhI"


def test_authorization_url_has_required_parameters():
    url = authorization_url("key", "https://example.com/cb", "s1", "chal")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert url.startswith("https://www.etsy.com/oauth/connect?")
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["transactions_r shops_r"]
    assert query["code_challenge_method"] == ["S256"]


def test_redirect_parsing_checks_state():
    assert code_from_redirect("https://example.com/cb?code=abc&state=s1", "s1") == "abc"
    with pytest.raises(EtsyApiError, match="State"):
        code_from_redirect("https://example.com/cb?code=abc&state=other", "s1")
    with pytest.raises(EtsyApiError, match="refused"):
        code_from_redirect("https://example.com/cb?error=access_denied&error_description=denied&state=s1", "s1")


def test_exchange_code_saves_user_id():
    token = exchange_code("key", "https://example.com/cb", "abc", "verifier", http=FakeEtsy([]))
    assert token.user_id == "123"
    assert not token.is_expired()


def test_receipts_are_paginated_and_cancelled_orders_skipped():
    client = EtsyClient("key", "secret", fresh_token(), http=FakeEtsy(RECEIPTS, page_size=2))
    receipts = list(client.iter_receipts(777))
    assert [r["receipt_id"] for r in receipts] == [4001, 4002, 4003, 4004]
    items = receipts_to_items(receipts)
    assert "4003" not in items["order_id"].tolist()


def test_receipt_money_shipping_and_discount_on_first_line_only():
    items = receipts_to_items(RECEIPTS, default_shipping_cost=4.2)
    first_order = items[items["order_id"] == "4001"]
    assert first_order["unit_price"].tolist() == [22.0, 6.0]
    assert first_order["qty"].tolist() == [2.0, 1.0]
    assert first_order["shipping_charged"].tolist() == [4.5, 0.0]
    assert first_order["discount"].tolist() == [4.4, 0.0]
    assert first_order["shipping_cost"].tolist() == [4.2, 0.0]
    assert first_order["date"].iloc[0] == pd.Timestamp("2026-11-03")


def test_expired_token_is_refreshed_and_saved(tmp_path):
    fake = FakeEtsy(RECEIPTS)
    token_file = tmp_path / "token.json"
    client = EtsyClient("key", "secret", EtsyToken("123.old", "123.refresh", expires_at=0), http=fake,
                        token_file=token_file)
    assert client.shop_id() == 777
    assert fake.calls[0][1].endswith("/oauth/token")
    assert EtsyToken.load(token_file).access_token == "123.new"


def test_expired_refresh_token_asks_seller_to_connect_again():
    def etsy_rejects_refresh(method, url, headers, body):
        raise EtsyApiError('Etsy API 400 on POST /oauth/token: {"error": "invalid_grant"}', status=400)

    client = EtsyClient("key", "secret", EtsyToken("123.old", "123.refresh", expires_at=0),
                        http=etsy_rejects_refresh)
    with pytest.raises(EtsyApiError, match="connect again with etsy-profit-connect"):
        client.shop_id()


def test_etsy_server_error_during_refresh_is_not_called_expired():
    def etsy_is_down(method, url, headers, body):
        raise EtsyApiError("Etsy API 503 on POST /oauth/token: unavailable", status=503)

    client = EtsyClient("key", "secret", EtsyToken("123.old", "123.refresh", expires_at=0), http=etsy_is_down)
    with pytest.raises(EtsyApiError, match="503") as caught:
        client.shop_id()
    assert "connect again" not in str(caught.value)


def test_api_orders_flow_into_the_same_report():
    shop = load_shop(SAMPLE)
    client = EtsyClient("key", "secret", fresh_token(), http=FakeEtsy(RECEIPTS))
    imported = fetch_etsy_orders(client, shop.orders, shop.products)
    assert imported["status"].tolist() == [READY, READY, READY, UNKNOWN_SKU]

    shop.orders = merge_ready_items(shop.orders, imported)
    order = analyze(shop).orders.query("order_id == '4001'")
    assert order["order_total"].round(2).tolist() == [44.10, 6.00]

    again = fetch_etsy_orders(client, shop.orders, shop.products)
    assert set(again.loc[again["sku"] != "FW-999", "status"]) == {ALREADY_IMPORTED}
