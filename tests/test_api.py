"""HTTP-level tests: reason codes over the wire and the end-to-end flow."""

from __future__ import annotations

import logging

logger = logging.getLogger("billing.test")


def test_missing_vs_invalid_fields_are_distinguished(client):
    client.post("/billing/accounts", json={"account_id": "acme"})
    client.post(
        "/billing/accounts/acme/subscription",
        json={"plan_code": "pro", "effective": "2026-09-01"},
    )

    base = {"event_id": "e1", "account_id": "acme", "metric": "api_calls",
            "quantity": 1, "occurred_at": "2026-09-10"}

    cases = [
        ({}, "MISSING_FIELD", "empty body"),
        ({k: v for k, v in base.items() if k != "metric"}, "MISSING_FIELD", "missing metric"),
        ({**base, "quantity": None}, "MISSING_FIELD", "null quantity"),
        ({**base, "occurred_at": "10/09/2026"}, "INVALID_VALUE", "bad date"),
        ({**base, "quantity": "abc"}, "INVALID_VALUE", "non-numeric"),
        ({**base, "quantity": -3}, "INVALID_VALUE", "negative"),
        ({**base, "occurred_at": "2026-10-10"}, "FUTURE_EVENT", "future event"),
        ({**base, "account_id": "ghost"}, "UNKNOWN_ACCOUNT", "unknown account"),
    ]
    for body, expected, label in cases:
        resp = client.post("/billing/usage", json=body)
        code = resp.json()["error"]["code"]
        logger.info("[http] body=%s -> status=%s code=%s (%s)", body, resp.status_code, code, label)
        assert resp.status_code in (400, 422)
        assert code == expected, (label, code)


def test_full_flow_over_http(client):
    r = client.post("/billing/accounts", json={"account_id": "acme"})
    assert r.status_code == 201

    r = client.post(
        "/billing/accounts/acme/subscription",
        json={"plan_code": "pro", "effective": "2026-09-01"},
    )
    assert r.status_code == 200

    r = client.post(
        "/billing/usage",
        json={"event_id": "e1", "account_id": "acme", "metric": "api_calls",
              "quantity": 50000, "occurred_at": "2026-09-10"},
    )
    assert r.status_code == 201 and r.json()["status"] == "accepted"

    # idempotent resubmit over HTTP -> duplicate, still 200
    r = client.post(
        "/billing/usage",
        json={"event_id": "e1", "account_id": "acme", "metric": "api_calls",
              "quantity": 50000, "occurred_at": "2026-09-10"},
    )
    assert r.status_code == 200 and r.json()["status"] == "duplicate"

    r = client.post("/billing/accounts/acme/bills/close?period=2026-09-15")
    assert r.status_code == 200
    bill = r.json()
    logger.info("[http] bill total=%s lines=%d", bill["total"], len(bill["usage_lines"]))
    assert bill["period"] == "2026-09"
    assert len(bill["usage_lines"]) == 1
    assert bill["usage_lines"][0]["event_id"] == "e1"

    # re-close returns the same immutable bill
    r2 = client.post("/billing/accounts/acme/bills/close?period=2026-09-15")
    assert r2.json()["total"] == bill["total"]

    r = client.get("/billing/accounts/acme/bills/2026-09")
    assert r.status_code == 200 and r.json()["total"] == bill["total"]

    r = client.get("/billing/accounts/acme/bills/1999-01")
    assert r.status_code == 404 and r.json()["error"]["code"] == "BILL_NOT_FOUND"


def test_rejections_listed_over_http(client):
    client.post("/billing/accounts", json={"account_id": "acme"})
    client.post("/billing/usage", json={
        "event_id": "x", "account_id": "acme", "metric": "api_calls",
        "quantity": 1, "occurred_at": "2026-09-10",
    })
    r = client.get("/billing/accounts/acme/rejections")
    assert r.status_code == 200
    reasons = [x["reason"] for x in r.json()["rejections"]]
    assert "NO_ACTIVE_PLAN" in reasons
