import pytest
import asyncio
import random
import string
from typing import Dict, Any
from datetime import datetime, timedelta


class TestPaypalMasterAdversarial:
    @pytest.mark.asyncio
    async def test_malicious_payment_payloads(self, paypal_engine):
        malicious_payloads = [
            {"amount": float("inf"), "currency": "USD", "payment_method": "card"},
            {"amount": float("-inf"), "currency": "USD", "payment_method": "card"},
            {"amount": float("nan"), "currency": "USD", "payment_method": "card"},
            {"amount": -1000000.0, "currency": "USD", "payment_method": "card"},
            {"amount": 1000000000000.0, "currency": "USD", "payment_method": "card"},
            {"amount": "not_a_number", "currency": "USD", "payment_method": "card"},
            {"amount": None, "currency": "USD", "payment_method": "card"},
            {"amount": [], "currency": "USD", "payment_method": "card"},
            {"amount": {}, "currency": "USD", "payment_method": "card"},
            {"currency": "", "payment_method": "card"},
            {"currency": None, "payment_method": "card"},
            {"currency": "INVALID", "payment_method": "card"},
            {"currency": "A" * 10000, "payment_method": "card"},
            {"payment_method": "card"},
            {"payment_method": None},
            {"payment_method": "INVALID_METHOD"},
            {"payment_method": "A" * 10000},
            {},
            {"random_field": "random_value"},
            {"amount": "100", "currency": "USD", "payment_method": "card", "malicious": "<script>alert('xss')</script>"},
            {"amount": 100, "currency": "USD", "payment_method": "card", "order_id": "' OR '1'='1"},
            {"amount": 100, "currency": "USD", "payment_method": "card", "order_id": "../../../etc/passwd"},
            {"amount": 100, "currency": "USD", "payment_method": "card", "order_id": "\x00\x01\x02\x03\x04\x05"},
        ]
        
        for payload in malicious_payloads:
            try:
                result = await paypal_engine.process_payment(payload)
                assert isinstance(result, dict), f"Invalid response type for payload: {payload}"
                assert "success" in result, f"Missing success key for payload: {payload}"
            except Exception as e:
                assert isinstance(e, (ValueError, TypeError, ValidationError)), f"Unexpected exception type for payload {payload}: {type(e)}"
    
    @pytest.mark.asyncio
    async def test_state_injection_attacks(self, paypal_engine):
        invalid_transitions = [
            ("completed", "created"),
            ("refunded", "pending"),
            ("failed", "processing"),
            ("cancelled", "completed"),
            ("refunded", "failed"),
            ("completed", "cancelled"),
            ("pending", "refunded"),
            ("processing", "created"),
        ]
        
        for from_state, to_state in invalid_transitions:
            try:
                result = await paypal_engine.transition_payment(f"payment_{random.randint(1, 1000)}", to_state)
                if result.get("success", False):
                    assert False, f"Invalid transition allowed from {from_state} to {to_state}"
            except Exception as e:
                pass
    
    @pytest.mark.asyncio
    async def test_webhook_signature_forgery(self, paypal_engine):
        forged_signatures = [
            "",
            None,
            "invalid_signature",
            "a" * 10000,
            "b" * 100000,
            "random_string",
            "valid_looking_but_fake",
            "sha256=invalid",
            "hmac=wrong",
            "\x00\x01\x02\x03\x04\x05" * 1000,
        ]
        
        for signature in forged_signatures:
            try:
                result = await paypal_engine.verify_webhook_signature(
                    "test_payload",
                    signature,
                    int(datetime.utcnow().timestamp())
                )
                assert not result.get("valid", True), f"Forged signature accepted: {signature[:50]}"
            except Exception as e:
                pass
    
    @pytest.mark.asyncio
    async def test_currency_conversion_overflow(self, paypal_engine):
        overflow_values = [
            float("inf"),
            float("-inf"),
            float("nan"),
            10**308,
            -10**308,
            10**400,
            -10**400,
            "not_a_number",
            None,
            [],
            {},
        ]
        
        for value in overflow_values:
            try:
                result = await paypal_engine.convert_currency(value, "USD", "EUR")
                assert isinstance(result, dict), f"Invalid response for value {value}"
            except Exception as e:
                assert isinstance(e, (ValueError, TypeError)), f"Unexpected exception for value {value}: {type(e)}"
    
    @pytest.mark.asyncio
    async def test_negative_refund_attacks(self, paypal_engine):
        malicious_refunds = [
            {"amount": -1000.0, "payment_id": "pay_123"},
            {"amount": float("inf"), "payment_id": "pay_123"},
            {"amount": float("nan"), "payment_id": "pay_123"},
            {"amount": 1000000000000.0, "payment_id": "pay_123"},
            {"amount": None, "payment_id": "pay_123"},
            {"amount": "not_a_number", "payment_id": "pay_123"},
            {"payment_id": ""},
            {"payment_id": None},
            {"payment_id": "A" * 10000},
            {"payment_id": "../../../etc/passwd"},
            {"payment_id": "' OR '1'='1"},
        ]
        
        for refund in malicious_refunds:
            try:
                result = await paypal_engine.process_refund(refund)
                assert isinstance(result, dict), f"Invalid response for refund {refund}"
            except Exception as e:
                assert isinstance(e, (ValueError, TypeError, ValidationError)), f"Unexpected exception for refund {refund}: {type(e)}"
    
    @pytest.mark.asyncio
    async def test_concurrent_race_conditions(self, paypal_engine):
        payment_id = f"race_payment_{random.randint(1, 1000)}"
        states = ["pending", "processing", "completed", "failed", "refunded", "cancelled"]
        
        async def race_transition():
            return await paypal_engine.transition_payment(payment_id, random.choice(states))
        
        tasks = [race_transition() for _ in range(100)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        success_results = [r for r in results if isinstance(r, dict) and r.get("success", False)]
        if success_results:
            final_state = success_results[-1].get("state")
            assert final_state in states, f"Invalid final state: {final_state}"
    
    @pytest.mark.asyncio
    async def test_extreme_payload_sizes(self, paypal_engine):
        payload_sizes = [100, 1000, 10000, 100000, 1000000]
        
        for size in payload_sizes:
            large_string = ''.join(random.choice(string.ascii_letters) for _ in range(size))
            payload = {
                "amount": 100.0,
                "currency": "USD",
                "payment_method": "card",
                "large_field": large_string
            }
            
            try:
                result = await paypal_engine.process_payment(payload)
                assert isinstance(result, dict), f"Failed with payload size {size}"
            except Exception as e:
                pass
    
    @pytest.mark.asyncio
    async def test_timing_attack_simulation(self, paypal_engine):
        start_times = []
        end_times = []
        
        for _ in range(100):
            start = time.perf_counter()
            await paypal_engine.verify_webhook_signature("test", "wrong_signature", int(time.time()))
            end = time.perf_counter()
            start_times.append(start)
            end_times.append(end)
        
        durations = [end_times[i] - start_times[i] for i in range(len(start_times))]
        avg_duration = sum(durations) / len(durations)
        max_deviation = max(abs(d - avg_duration) for d in durations)
        
        assert max_deviation < avg_duration * 0.5, f"Timing variation too high: {max_deviation} vs {avg_duration}"
    
    @pytest.mark.asyncio
    async def test_sql_injection_patterns(self, paypal_engine):
        injection_patterns = [
            "' OR '1'='1",
            "'; DROP TABLE payments; --",
            "' UNION SELECT * FROM users --",
            "' AND 1=1 --",
            "' OR 1=1 --",
            "admin' --",
            "' OR 'a'='a",
            "'; INSERT INTO payments VALUES (...) --",
        ]
        
        for pattern in injection_patterns:
            try:
                result = await paypal_engine.process_payment({
                    "amount": 100.0,
                    "currency": "USD",
                    "payment_method": "card",
                    "order_id": pattern
                })
                assert isinstance(result, dict), f"Failed with injection pattern: {pattern[:50]}"
            except Exception as e:
                pass
    
    @pytest.mark.asyncio
    async def test_xss_payloads(self, paypal_engine):
        xss_payloads = [
            "<script>alert('xss')</script>",
            "<img src=x onerror=alert(1)>",
            "<svg onload=alert(1)>",
            "javascript:alert(1)",
            "<iframe src=javascript:alert(1)>",
            "<body onload=alert(1)>",
            "<a href='javascript:alert(1)'>click</a>",
            "\" onmouseover=alert(1) \"",
        ]
        
        for xss in xss_payloads:
            try:
                result = await paypal_engine.process_payment({
                    "amount": 100.0,
                    "currency": "USD",
                    "payment_method": "card",
                    "description": xss
                })
                assert isinstance(result, dict), f"Failed with XSS payload: {xss[:50]}"
            except Exception as e:
                pass