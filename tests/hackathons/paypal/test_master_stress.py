import asyncio
import random
import time
from datetime import datetime

import pytest


class TestPaypalMasterStress:
    @pytest.mark.asyncio
    async def test_concurrent_payment_processing_stress(self, paypal_engine):
        concurrency_levels = [10, 50, 100, 250, 500]
        results = []

        for concurrency in concurrency_levels:
            start_time = time.perf_counter()
            tasks = []

            for i in range(concurrency):
                task = paypal_engine.process_payment({
                    "amount": random.uniform(1.0, 10000.0),
                    "currency": random.choice(["USD", "EUR", "GBP", "INR", "JPY"]),
                    "payment_method": random.choice(["card", "paypal", "bank_transfer"]),
                    "order_id": f"stress_order_{i}_{int(time.time())}"
                })
                tasks.append(task)

            responses = await asyncio.gather(*tasks, return_exceptions=True)
            end_time = time.perf_counter()

            success_count = sum(1 for r in responses if not isinstance(r, Exception) and r.get("success", False))
            error_count = sum(1 for r in responses if isinstance(r, Exception) or not r.get("success", False))

            results.append({
                "concurrency": concurrency,
                "duration": end_time - start_time,
                "success_count": success_count,
                "error_count": error_count,
                "throughput": concurrency / (end_time - start_time),
                "success_rate": success_count / concurrency if concurrency > 0 else 0
            })

        for result in results:
            assert result["success_rate"] >= 0.95, f"Success rate too low at concurrency {result['concurrency']}: {result['success_rate']}"
            assert result["throughput"] >= 100, f"Throughput too low at concurrency {result['concurrency']}: {result['throughput']} req/s"

    @pytest.mark.asyncio
    async def test_payment_state_machine_stress(self, paypal_engine):
        total_transitions = 10000
        states = ["created", "pending", "processing", "completed", "failed", "refunded", "cancelled"]

        async def transition_payment():
            payment_id = f"payment_{random.randint(1, total_transitions)}"
            current_state = random.choice(states[:3])

            for _ in range(random.randint(1, 5)):
                next_state = random.choice(states)
                result = await paypal_engine.transition_payment(payment_id, next_state)
                if result.get("success", False):
                    current_state = next_state

            return current_state

        tasks = [transition_payment() for _ in range(total_transitions)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid_states = sum(1 for r in results if r in states)
        assert valid_states / total_transitions >= 0.99, f"Invalid state transitions: {valid_states / total_transitions}"

    @pytest.mark.asyncio
    async def test_soak_payment_processing(self, paypal_engine):
        duration = 60
        start_time = time.perf_counter()
        processed = 0
        errors = 0

        while time.perf_counter() - start_time < duration:
            batch_size = random.randint(10, 50)
            tasks = []

            for i in range(batch_size):
                task = paypal_engine.process_payment({
                    "amount": random.uniform(10.0, 5000.0),
                    "currency": "USD",
                    "payment_method": "card",
                    "order_id": f"soak_order_{processed + i}_{int(time.time())}"
                })
                tasks.append(task)

            responses = await asyncio.gather(*tasks, return_exceptions=True)
            processed += batch_size
            errors += sum(1 for r in responses if isinstance(r, Exception) or not r.get("success", False))

            await asyncio.sleep(random.uniform(0.01, 0.1))

        error_rate = errors / processed if processed > 0 else 0
        assert error_rate <= 0.01, f"Error rate too high during soak test: {error_rate}"
        assert processed > 1000, f"Not enough payments processed during soak: {processed}"

    @pytest.mark.asyncio
    async def test_rate_limiting_stress(self, paypal_engine):
        burst_sizes = [100, 500, 1000, 2000]

        for burst_size in burst_sizes:
            start_time = time.perf_counter()
            tasks = []

            for i in range(burst_size):
                task = paypal_engine.verify_webhook_signature(
                    f"payload_{i}",
                    f"signature_{i}",
                    int(time.time())
                )
                tasks.append(task)

            results = await asyncio.gather(*tasks, return_exceptions=True)
            end_time = time.perf_counter()

            rate_limit_hits = sum(1 for r in results if isinstance(r, dict) and r.get("rate_limited", False))
            success_count = sum(1 for r in results if not isinstance(r, Exception) and r.get("success", False))

            assert rate_limit_hits < burst_size * 0.1, f"Too many rate limit hits at burst {burst_size}: {rate_limit_hits}"
            assert success_count / burst_size >= 0.9, f"Success rate too low at burst {burst_size}: {success_count / burst_size}"

    @pytest.mark.asyncio
    async def test_refund_processing_stress(self, paypal_engine):
        concurrent_refunds = 500
        tasks = []

        for i in range(concurrent_refunds):
            task = paypal_engine.process_refund({
                "payment_id": f"payment_{random.randint(1, 10000)}",
                "amount": random.uniform(1.0, 1000.0),
                "reason": random.choice(["customer_request", "duplicate", "fraud", "other"])
            })
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        success_count = sum(1 for r in results if not isinstance(r, Exception) and r.get("success", False))
        assert success_count / concurrent_refunds >= 0.95, f"Refund success rate too low: {success_count / concurrent_refunds}"

    @pytest.mark.asyncio
    async def test_currency_conversion_stress(self, paypal_engine):
        conversion_pairs = [
            ("USD", "EUR"), ("EUR", "GBP"), ("GBP", "INR"), ("INR", "JPY"),
            ("JPY", "USD"), ("USD", "GBP"), ("EUR", "INR"), ("GBP", "JPY")
        ]

        concurrent_conversions = 1000
        tasks = []

        for i in range(concurrent_conversions):
            from_curr, to_curr = random.choice(conversion_pairs)
            task = paypal_engine.convert_currency(
                random.uniform(1.0, 10000.0),
                from_curr,
                to_curr
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid_conversions = sum(1 for r in results if not isinstance(r, Exception) and r.get("rate", 0) > 0)
        assert valid_conversions / concurrent_conversions >= 0.99, f"Conversion success rate too low: {valid_conversions / concurrent_conversions}"

    @pytest.mark.asyncio
    async def test_webhook_processing_stress(self, paypal_engine):
        webhook_count = 2000
        event_types = ["payment.created", "payment.completed", "payment.failed", "refund.created", "dispute.created"]

        tasks = []
        for i in range(webhook_count):
            event_type = random.choice(event_types)
            task = paypal_engine.process_webhook_event({
                "id": f"evt_{i}_{int(time.time())}",
                "type": event_type,
                "created_at": datetime.utcnow().isoformat(),
                "data": {"payment_id": f"pay_{random.randint(1, 10000)}"}
            })
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        processed = sum(1 for r in results if not isinstance(r, Exception) and r.get("processed", False))
        assert processed / webhook_count >= 0.98, f"Webhook processing success rate too low: {processed / webhook_count}"
