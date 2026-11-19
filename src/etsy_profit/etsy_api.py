"""Pull orders straight from a seller's Etsy shop with the Etsy Open API v3.

Endpoints, fields and the auth flow follow https://developers.etsy.com/documentation
and Etsy's published OpenAPI spec (checked 12 Nov 2026).
"""

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator

import pandas as pd

from .etsy_import import mark_import_status

AUTHORIZE_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
API_BASE = "https://api.etsy.com/v3/application"
SCOPES = ("transactions_r", "shops_r")
PAGE_SIZE = 100
SKIPPED_RECEIPT_STATUSES = {"canceled"}

HttpFunction = Callable[[str, str, dict, bytes | None], dict]


class EtsyApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def default_http(method: str, url: str, headers: dict, body: bytes | None) -> dict:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        raise EtsyApiError(f"Etsy API {error.code} on {method} {url}: {detail}", status=error.code) from error


@dataclass
class EtsyToken:
    access_token: str
    refresh_token: str
    expires_at: float

    @classmethod
    def from_response(cls, data: dict, now: float | None = None) -> "EtsyToken":
        now = time.time() if now is None else now
        return cls(data["access_token"], data["refresh_token"], now + int(data["expires_in"]))

    @property
    def user_id(self) -> str:
        return self.access_token.split(".", 1)[0]

    def is_expired(self, now: float | None = None, margin: int = 60) -> bool:
        now = time.time() if now is None else now
        return now >= self.expires_at - margin

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "EtsyToken":
        return cls(**json.loads(Path(path).read_text()))


def make_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    return verifier, code_challenge(verifier)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorization_url(keystring: str, redirect_uri: str, state: str, challenge: str) -> str:
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": keystring,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }, quote_via=urllib.parse.quote)
    return f"{AUTHORIZE_URL}?{query}"


def code_from_redirect(redirect_url: str, expected_state: str) -> str:
    params = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_url.strip()).query)
    if "error" in params:
        raise EtsyApiError(f"Etsy refused access: {params.get('error_description', params['error'])[0]}")
    if params.get("state", [None])[0] != expected_state:
        raise EtsyApiError("State in the redirect does not match. Start the connection again.")
    if "code" not in params:
        raise EtsyApiError("No authorization code in that URL. Paste the full address you were sent to.")
    return params["code"][0]


def _token_request(fields: dict, http: HttpFunction) -> EtsyToken:
    body = urllib.parse.urlencode(fields).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    return EtsyToken.from_response(http("POST", TOKEN_URL, headers, body))


def exchange_code(keystring: str, redirect_uri: str, code: str, verifier: str,
                  http: HttpFunction = default_http) -> EtsyToken:
    return _token_request({
        "grant_type": "authorization_code",
        "client_id": keystring,
        "redirect_uri": redirect_uri,
        "code": code,
        "code_verifier": verifier,
    }, http)


def refresh_token(keystring: str, token: EtsyToken, http: HttpFunction = default_http) -> EtsyToken:
    return _token_request({
        "grant_type": "refresh_token",
        "client_id": keystring,
        "refresh_token": token.refresh_token,
    }, http)


class EtsyClient:
    def __init__(self, keystring: str, shared_secret: str, token: EtsyToken,
                 http: HttpFunction = default_http, token_file: str | Path | None = None):
        self.keystring = keystring
        self.shared_secret = shared_secret
        self.token = token
        self.http = http
        self.token_file = token_file

    def _ensure_fresh_token(self) -> None:
        if self.token.is_expired():
            try:
                self.token = refresh_token(self.keystring, self.token, self.http)
            except EtsyApiError as error:
                if error.status not in (400, 401):
                    raise
                raise EtsyApiError(
                    "Etsy no longer accepts the saved connection. This happens when the shop was not read "
                    "for more than 90 days, or when the seller removed access in their Etsy account. "
                    "Ask the seller to connect again with etsy-profit-connect. "
                    f"(Etsy said: {error})", status=error.status) from error
            if self.token_file:
                self.token.save(self.token_file)

    def get(self, path: str, params: dict | None = None) -> dict:
        self._ensure_fresh_token()
        url = f"{API_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        headers = {
            "x-api-key": f"{self.keystring}:{self.shared_secret}",
            "Authorization": f"Bearer {self.token.access_token}",
        }
        return self.http("GET", url, headers, None)

    def shop_id(self) -> int:
        me = self.get("/users/me")
        if not me.get("shop_id"):
            raise EtsyApiError("This Etsy account has no shop.")
        return me["shop_id"]

    def iter_receipts(self, shop_id: int, min_created: int | None = None,
                      max_created: int | None = None) -> Iterator[dict]:
        offset = 0
        while True:
            page = self.get(f"/shops/{shop_id}/receipts", {
                "min_created": min_created, "max_created": max_created,
                "limit": PAGE_SIZE, "offset": offset,
            })
            results = page.get("results", [])
            yield from results
            offset += len(results)
            if not results or offset >= page.get("count", 0):
                break


def _money(value: dict | None) -> float:
    if not value:
        return 0.0
    return value["amount"] / value["divisor"]


def receipts_to_items(receipts: list[dict], default_shipping_cost: float = 0.0) -> pd.DataFrame:
    rows = []
    for receipt in receipts:
        if str(receipt.get("status", "")).lower() in SKIPPED_RECEIPT_STATUSES:
            continue
        for position, transaction in enumerate(receipt.get("transactions", [])):
            first = position == 0
            rows.append({
                "date": pd.Timestamp(receipt["create_timestamp"], unit="s").normalize(),
                "order_id": str(receipt["receipt_id"]),
                "channel": "Etsy",
                "sku": (transaction.get("sku") or "").strip(),
                "qty": float(transaction["quantity"]),
                "unit_price": _money(transaction.get("price")),
                "shipping_charged": _money(receipt.get("total_shipping_cost")) if first else 0.0,
                "shipping_cost": float(default_shipping_cost) if first else 0.0,
                "discount": _money(receipt.get("discount_amt")) if first else 0.0,
                "item_name": transaction.get("title", ""),
            })
    columns = ["date", "order_id", "channel", "sku", "qty", "unit_price", "shipping_charged",
               "shipping_cost", "discount", "item_name"]
    return pd.DataFrame(rows, columns=columns)


def fetch_etsy_orders(client: EtsyClient, existing_orders: pd.DataFrame, products: pd.DataFrame,
                      since: pd.Timestamp | None = None, default_shipping_cost: float = 0.0,
                      item_names: pd.DataFrame | None = None) -> pd.DataFrame:
    shop_id = client.shop_id()
    min_created = int(since.timestamp()) if since is not None else None
    receipts = list(client.iter_receipts(shop_id, min_created=min_created))
    items = receipts_to_items(receipts, default_shipping_cost)
    return mark_import_status(items, existing_orders, products, item_names)
