"""Axelr PayPal AI layer - payment intent extraction from unstructured text.

Implements LLM-based extraction with regex fallback, per Goal 3 requirements.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any
import json

from pydantic import BaseModel, Field

from .core import get_core_module, log_audit, AuditEvent
from .schemas import PaymentIntent, OrderItem

# Regex patterns for fallback extraction
AMOUNT_PATTERNS = [
    r'\$(\d{1,3}(?:,\d{3})*\.\d{2})',  # $100.00
    r'€(\d{1,3}(?:,\d{3})*\.\d{2})',   # €100.00
    r'£(\d{1,3}(?:,\d{3})*\.\d{2})',   # £100.00
    r'(\d{1,3}(?:,\d{3})*\.\d{2})\s*(USD|EUR|GBP)',  # 100.00 USD
    r'(\d+)\s*(JPY|CNY)',  # 1000 JPY
]

NAME_PATTERNS = [
    r'from\s+([A-Z][a-z]+\s+[A-Z][a-z]+)',  # from John Doe
    r'bill to\s+([A-Z][a-z]+\s+[A-Z][a-z]+)',  # bill to Jane Smith
    r'to\s+([A-Z][a-z]+\s+[A-Z][a-z]+)',  # to Alex Johnson
]

EMAIL_PATTERN = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
DATE_PATTERNS = [
    r'due by\s+(\d{4}-\d{2}-\d{2})',  # due by 2024-12-31
    r'pay by\s+(\d{4}-\d{2}-\d{2})',  # pay by 2024-11-15
    r'due\s+(\d{1,2}/\d{1,2}/\d{4})',  # due 12/31/2024
]

TYPE_PATTERNS = {
    "invoice": [r"invoice\s*#\d+", r"invoice"],
    "subscription": [r"monthly subscription", r"recurring", r"subscription"],
    "payout": [r"payout", r"pay out", r"payment to contractor"]
}


async def _extract_with_openai(text: str) -> Dict[str, Any]:
    """Extract payment intent using OpenAI GPT with JSON mode"""
    core = get_core_module()
    if not core.settings.openai_api_key:
        raise ValueError("OpenAI API key not configured")
    
    import openai
    client = openai.AsyncOpenAI(api_key=core.settings.openai_api_key)
    
    prompt = """Extract payment intent from the following text. Return ONLY valid JSON with these exact fields:
    {
        "payer_name": "full name of payer",
        "payer_email": "email if present, null otherwise",
        "amount": "string representation of decimal amount (e.g. "150.00")",
        "currency": "3-letter ISO code (USD, EUR, etc.)",
        "items": [{"name": "item name", "quantity": 1, "unit_amount": "decimal string"}],
        "due_date": "ISO 8601 date string if present, null otherwise",
        "type": "invoice|subscription|payout",
        "confidence": 0.95  # float between 0 and 1
    }
    
    Text to process: """ + text
    
    response = await client.chat.completions.create(
        model="gpt-3.5-turbo-1106",
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}]
    )
    
    return json.loads(response.choices[0].message.content)


async def _extract_with_anthropic(text: str) -> Dict[str, Any]:
    """Extract payment intent using Anthropic Claude"""
    core = get_core_module()
    if not core.settings.anthropic_api_key:
        raise ValueError("Anthropic API key not configured")
    
    import anthropic
    client = anthropic.Anthropic(api_key=core.settings.anthropic_api_key)
    
    prompt = f"""Extract payment intent from the following text. Return ONLY valid JSON with these exact fields:
    {{
        "payer_name": "full name of payer",
        "payer_email": "email if present, null otherwise",
        "amount": "string representation of decimal amount (e.g. "150.00")",
        "currency": "3-letter ISO code (USD, EUR, etc.)",
        "items": [{{"name": "item name", "quantity": 1, "unit_amount": "decimal string"}}],
        "due_date": "ISO 8601 date string if present, null otherwise",
        "type": "invoice|subscription|payout",
        "confidence": 0.95  # float between 0 and 1
    }}
    
    Text to process: {text}"""
    
    response = await client.messages.create(
        model="claude-3-sonnet-20240229",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    
    return json.loads(response.content[0].text)


def _regex_fallback_extraction(text: str, raw_input_hash: str) -> PaymentIntent:
    """Fallback regex extraction when LLM is unavailable"""
    amount = "0.00"
    currency = "USD"
    payer_name = "Unknown Payer"
    payer_email = None
    due_date = None
    payment_type = "invoice"
    confidence = 0.7  # Lower confidence for regex fallback
    items = []

    # Extract amount and currency
    for pattern in AMOUNT_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            amount_str = match.group(1).replace(",", "")
            try:
                Decimal(amount_str)
                amount = amount_str
                # Check for currency in the match
                if len(match.groups()) > 1 and match.group(2) in ["USD", "EUR", "GBP", "JPY", "CNY"]:
                    currency = match.group(2)
                break
            except InvalidOperation:
                continue

    # Extract payer name
    for pattern in NAME_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            payer_name = match.group(1)
            break

    # Extract email
    email_match = re.search(EMAIL_PATTERN, text)
    if email_match:
        payer_email = email_match.group(0)

    # Extract due date
    for pattern in DATE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            due_date_candidate = match.group(1)
            # Convert MM/DD/YYYY to ISO if needed
            if "/" in due_date_candidate:
                try:
                    month, day, year = due_date_candidate.split("/")
                    due_date = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
                except:
                    due_date = due_date_candidate
            else:
                due_date = due_date_candidate
            break

    # Extract payment type
    for ptype, patterns in TYPE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                payment_type = ptype
                break
        else:
            continue
        break

    # Create single item for the total amount
    items.append(OrderItem(
        name="Payment for services",
        quantity=1,
        unit_amount=amount
    ))

    return PaymentIntent(
        payer_name=payer_name,
        payer_email=payer_email,
        amount=amount,
        currency=currency,
        items=items,
        due_date=due_date,
        type=payment_type,
        confidence=confidence,
        raw_input_hash=raw_input_hash
    )


async def extract_payment_intent(unstructured_text: str) -> PaymentIntent:
    """Main extraction function: tries LLM first, falls back to regex"""
    # Create hash of raw input for audit logging
    raw_input_hash = hashlib.sha256(unstructured_text.encode()).hexdigest()
    
    # Try LLM providers first
    core = get_core_module()
    extracted = None
    
    # Try OpenAI first if configured
    if core.settings.openai_api_key:
        try:
            llm_result = await _extract_with_openai(unstructured_text)
            extracted = PaymentIntent(**llm_result, raw_input_hash=raw_input_hash)
        except Exception as e:
            core.logger.warning(f"OpenAI extraction failed: {str(e)}, trying Anthropic")
    
    # Try Anthropic if OpenAI failed or not configured
    if not extracted and core.settings.anthropic_api_key:
        try:
            llm_result = await _extract_with_anthropic(unstructured_text)
            extracted = PaymentIntent(**llm_result, raw_input_hash=raw_input_hash)
        except Exception as e:
            core.logger.warning(f"Anthropic extraction failed: {str(e)}, falling back to regex")
    
    # Fall back to regex if all LLMs failed
    if not extracted:
        extracted = _regex_fallback_extraction(unstructured_text, raw_input_hash)
    
    # Log extraction for audit
    await log_audit(AuditEvent(
        event_type="ai.payment_intent.extracted",
        user_id="system",
        timestamp=datetime.now().isoformat(),
        metadata={
            "confidence": extracted.confidence,
            "raw_input_hash": raw_input_hash,
            "extraction_method": "llm" if core.settings.openai_api_key or core.settings.anthropic_api_key else "regex"
        }
    ))
    
    return extracted