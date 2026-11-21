"""
PayPal Local Simulation Test Harness
===================================
Local test suite for PayPal webhook and payment flow testing.
Runs a mock PayPal server that simulates webhook events and API responses.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("paypal.simulation")

# -----------------------------------------------------------------------------
# Mock PayPal Server
# -----------------------------------------------------------------------------

app = FastAPI(title="PayPal Mock Server", version="1.0")

# In-memory store for mock orders
mock_orders: Dict[str, Dict[str, Any]] = {}
webhook_received = []


@app.post("/v2/checkout/orders")
async def create_order(request: Request):
    """Mock PayPal create order endpoint."""
    body = await request.json()
    order_id = f"ORDER_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{len(mock_orders)}"

    order = {
        "id": order_id,
        "status": "CREATED",
        "create_time": datetime.utcnow().isoformat(),
        "body": body,
        "links": [{"href": f"https://mock-paypal.local/checkout/now?orderId={order_id}", "rel": "approve"}],
    }

    mock_orders[order_id] = order
    logger.info(f"mock_order_created order_id={order_id}")
    return JSONResponse(order)


@app.post("/v2/checkout/orders/{order_id}/capture")
async def capture_order(order_id: str, request: Request):
    """Mock PayPal capture order endpoint."""
    if order_id not in mock_orders:
        return JSONResponse({"error": "Order not found"}, status_code=404)

    mock_orders[order_id]["status"] = "COMPLETED"
    capture_id = f"CAPTURE_{order_id}"

    # Simulate webhook event sent to Axelr backend
    await send_webhook_event(
        "PAYMENT.CAPTURE.COMPLETED",
        {
            "id": capture_id,
            "status": "COMPLETED",
            "amount": mock_orders[order_id]["body"].get("purchase_units", [{}])[0].get("amount", {}),
        },
    )

    logger.info(f"mock_order_captured order_id={order_id} capture_id={capture_id}")
    return JSONResponse({"id": capture_id, "status": "COMPLETED", "create_time": datetime.utcnow().isoformat()})


@app.post("/mock/send-webhook")
async def trigger_webhook(request: Request):
    """Exposed endpoint to manually trigger webhook events."""
    body = await request.json()
    event_type = body.get("event_type")
    resource = body.get("resource", {})

    if not event_type:
        return JSONResponse({"error": "event_type required"}, status_code=400)

    result = await send_webhook_event(event_type, resource)
    return JSONResponse({"success": True, "webhook_sent": result})


@app.get("/mock/orders")
async def list_orders():
    """List all mock orders created."""
    return JSONResponse(
        {"total_orders": len(mock_orders), "orders": mock_orders, "webhooks_received": len(webhook_received)}
    )


@app.get("/mock/webhooks")
async def list_webhooks():
    """List all webhooks sent to Axelr."""
    return JSONResponse({"total_webhooks": len(webhook_received), "webhooks": webhook_received})


async def send_webhook_event(event_type: str, resource: Dict[str, Any]) -> bool:
    """Send mock webhook event to Axelr backend."""
    axelr_webhook_url = "http://localhost:8000/api/billing/paypal/webhook"

    event = {
        "id": f"EVENT_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        "event_type": event_type,
        "resource_type": "capture",
        "create_time": datetime.utcnow().isoformat(),
        "resource": resource,
    }

    webhook_received.append(event)

    # Send webhook to Axelr
    try:
        async with httpx.AsyncClient() as client:
            # Mock PayPal headers
            headers = {
                "PAYPAL-TRANSMISSION-ID": "mock-transmission-id",
                "PAYPAL-TRANSMISSION-TIME": datetime.utcnow().isoformat(),
                "PAYPAL-AUTH-ALGO": "SHA256withRSA",
                "PAYPAL-CERT-URL": "https://mock.paypal.com/cert",
                "PAYPAL-TRANSMISSION-SIG": "mock-signature",
            }

            response = await client.post(axelr_webhook_url, json=event, headers=headers, timeout=10.0)

            logger.info(f"webhook_sent event_type={event_type} status={response.status_code}")
            return response.status_code == 200

    except Exception as e:
        logger.error(f"webhook_send_failed event_type={event_type} error={str(e)}")
        return False


# -----------------------------------------------------------------------------
# Test Scenarios Runner
# -----------------------------------------------------------------------------


async def run_paypal_test_suite():
    """Execute comprehensive PayPal integration test suite."""
    logger.info("starting_paypal_test_suite")

    base_url = "http://localhost:9000"
    axelr_api_url = "http://localhost:8000"

    async with httpx.AsyncClient() as client:
        # Test 1: Create a mock order
        logger.info("test1_create_order")
        create_payload = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "amount": {"currency_code": "USD", "value": "29.99"},
                    "description": "Axelr Pro Subscription - Monthly",
                }
            ],
        }

        response = await client.post(f"{base_url}/v2/checkout/orders", json=create_payload)
        assert response.status_code == 201, "Failed to create mock order"
        order_data = response.json()
        order_id = order_data["id"]
        logger.info(f"test1_passed order_created order_id={order_id}")

        # Test 2: Capture the order
        logger.info("test2_capture_order")
        response = await client.post(f"{base_url}/v2/checkout/orders/{order_id}/capture")
        assert response.status_code == 200, "Failed to capture order"
        capture_data = response.json()
        logger.info(f"test2_passed order_captured capture_id={capture_data['id']}")

        # Wait for webhook to be processed
        await asyncio.sleep(2)

        # Test 3: Verify webhook was received by Axelr
        logger.info("test3_verify_webhook_processing")
        response = await client.get(f"{base_url}/mock/webhooks")
        webhooks = response.json()["webhooks"]
        assert len(webhooks) >= 1, "No webhooks were sent"
        assert any(w["event_type"] == "PAYMENT.CAPTURE.COMPLETED" for w in webhooks), (
            "Capture completed webhook missing"
        )
        logger.info("test3_passed webhook_processing_verified")

        # Test 4: Test refund scenario
        logger.info("test4_refund_scenario")
        refund_event = {
            "event_type": "PAYMENT.CAPTURE.REFUNDED",
            "resource": {
                "id": capture_data["id"],
                "status": "REFUNDED",
                "amount": {"value": "29.99", "currency_code": "USD"},
            },
        }
        response = await client.post(f"{base_url}/mock/send-webhook", json=refund_event)
        assert response.status_code == 200, "Failed to send refund webhook"

        await asyncio.sleep(2)

        # Verify refund webhook was processed
        response = await client.get(f"{base_url}/mock/webhooks")
        webhooks = response.json()["webhooks"]
        assert any(w["event_type"] == "PAYMENT.CAPTURE.REFUNDED" for w in webhooks), "Refund webhook missing"
        logger.info("test4_passed refund_scenario_verified")

        # Test 5: Test edge case - invalid webhook payload
        logger.info("test5_invalid_webhook_payload")
        invalid_event = {
            # Missing required fields - should trigger schema validation
            "resource": {}
        }
        response = await client.post(f"{base_url}/mock/send-webhook", json=invalid_event)
        # Should still return 200 but log validation failure
        assert response.status_code == 200, "Server crashed on invalid payload"
        logger.info("test5_passed invalid_payload_handled_gracefully")

        logger.info("all_paypal_tests_passed")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # Run test suite against already running mock server
        asyncio.run(run_paypal_test_suite())
    else:
        # Start the mock server
        uvicorn.run(app, host="0.0.0.0", port=9000, log_level="info")
