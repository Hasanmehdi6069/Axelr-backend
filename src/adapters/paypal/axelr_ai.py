"""AI-powered payment intent extraction from unstructured text"""
from __future__ import annotations

import re
import hashlib
import json
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, ValidationError
import httpx

from .core import get_core_module

logger = logging.getLogger("axelr.paypal.ai")

class PaymentIntentItem(BaseModel):
    name: str = Field(..., min_length=1)
    quantity: int = Field(1, ge=1)
    unit_amount: str = Field(..., description="Decimal string of unit price")

class PaymentIntent(BaseModel):
    """Structured payment intent extracted from unstructured text"""
    payer_name: str
    payer_email: Optional[str] = None
    amount: str  # Total amount as Decimal string
    currency: str = Field("USD", pattern="^[A-Z]{3}$")
    items: List[PaymentIntentItem]
    due_date: Optional[str] = None  # ISO format date string
    type: str = Field("invoice", pattern="^(invoice|subscription|payout)$")
    confidence: float = Field(..., ge=0, le=1)  # AI confidence score

# Regex patterns for fallback extraction
CURRENCY_PATTERNS = {
    r'\$': 'USD',
    r'€': 'EUR',
    r'£': 'GBP',
    r'¥': 'JPY'
}

EMAIL_PATTERN = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
AMOUNT_PATTERN = r'[\$€£¥]?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)'
DATE_PATTERNS = [
    r'(\d{4}-\d{2}-\d{2})',  # ISO
    r'(\d{2}/\d{2}/\d{4})',   # US format
    r'due\s*(?:by|on)?\s*(\w+\s+\d{1,2},?\s+\d{4})'  # Natural language
]

def _parse_date(date_str: str) -> Optional[str]:
    """Attempt to parse various date formats into ISO"""
    for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"]:
        try:
            return datetime.strptime(date_str.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None

def _extract_with_regex(text: str) -> Dict[str, Any]:
    """Fallback regex-based extraction when LLM is unavailable"""
    result = {
        "payer_name": "Unknown Payer",
        "payer_email": None,
        "amount": "0.00",
        "currency": "USD",
        "items": [],
        "due_date": None,
        "type": "invoice",
        "confidence": 0.5,  # Base confidence for regex extraction
        "raw_input": text,
        "extracted_from": "regex_fallback"
    }

    # Extract email
    email_match = re.search(EMAIL_PATTERN, text)
    if email_match:
        result["payer_email"] = email_match.group(0)
        result["confidence"] += 0.1
        # Try to extract payer name from email username
        if result["payer_email"]:
            username = result["payer_email"].split("@")[0]
            # Capitalize username for payer name
            result["payer_name"] = username.replace(".", " ").title()

    # Extract currency and amount
    for symbol, curr in CURRENCY_PATTERNS.items():
        if re.search(symbol, text):
            result["currency"] = curr
            break
    
    # Also check for currency codes in text
    currency_code_match = re.search(r'\b(USD|EUR|GBP|CAD|AUD|JPY|INR|BRL|MXN|SGD|HKD|NZD)\b', text)
    if currency_code_match:
        result["currency"] = currency_code_match.group(1)
    
    amount_matches = re.findall(AMOUNT_PATTERN, text)
    if amount_matches:
        # Take the largest amount as total
        amounts = []
        for amt_str in amount_matches:
            try:
                clean_amt = amt_str.replace(',', '')
                amounts.append(Decimal(clean_amt))
            except InvalidOperation:
                continue
        if amounts:
            total = max(amounts)
            result["amount"] = f"{total:.2f}"
            result["confidence"] += 0.2
            # Add as an item using PaymentIntentItem format
            result["items"].append(PaymentIntentItem(
                name="Invoice total",
                quantity=1,
                unit_amount=f"{total:.2f}"
            ))

    # Extract due date
    for pattern in DATE_PATTERNS:
        date_match = re.search(pattern, text, re.IGNORECASE)
        if date_match:
            iso_date = _parse_date(date_match.group(1))
            if iso_date:
                result["due_date"] = iso_date
                result["confidence"] += 0.1
                break

    # Detect subscription
    if re.search(r'subscription|monthly|yearly|recurring', text, re.IGNORECASE):
        result["type"] = "subscription"
        result["confidence"] += 0.05
        
    # Detect payout
    if re.search(r'payout|payment to|pay to', text, re.IGNORECASE):
        result["type"] = "payout"
        result["confidence"] += 0.05

    result["confidence"] = min(result["confidence"], 0.8)  # Cap regex confidence
    return result

async def _extract_with_openai(text: str) -> Optional[Dict[str, Any]]:
    """Extract payment intent using OpenAI API"""
    core = get_core_module()
    openai_key = core.settings.openai_api_key if hasattr(core.settings, 'openai_api_key') else None
    
    if not openai_key:
        logger.warning("OpenAI API key not configured")
        return None

    system_prompt = """Extract payment intent from the provided text. Return ONLY valid JSON with these fields:
- payer_name: string (name of the person who should pay)
- payer_email: string|null (email if found)
- amount: string (total amount as decimal string, e.g. "99.99")
- currency: string (3-letter ISO code, default USD)
- items: array of {name: string, quantity: number, unit_amount: string}
- due_date: string|null (ISO date if found)
- type: "invoice"|"subscription"|"payout"
- confidence: number (0-1, how confident you are in this extraction)
Be strict about types, ensure amount is always a string, never null required fields."""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {openai_key}"},
                json={
                    "model": "gpt-3.5-turbo-1106",
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": text}
                    ]
                }
            )
            response.raise_for_status()
            llm_output = response.json()
            extracted = json.loads(llm_output["choices"][0]["message"]["content"])
            # Validate with Pydantic
            validated = PaymentIntent(**extracted)
            return validated.model_dump()
    except Exception as e:
        logger.error(f"OpenAI extraction failed: {str(e)}", exc_info=True)
        raise  # Re-raise to allow failover to next provider

async def _extract_with_anthropic(text: str) -> Optional[Dict[str, Any]]:
    """Extract payment intent using Anthropic API"""
    core = get_core_module()
    anthropic_key = core.settings.anthropic_api_key if hasattr(core.settings, 'anthropic_api_key') else None
    
    if not anthropic_key:
        logger.warning("Anthropic API key not configured")
        return None

    system_prompt = """Extract payment intent from the provided text. Return ONLY valid JSON with these fields:
- payer_name: string (name of the person who should pay)
- payer_email: string|null (email if found)
- amount: string (total amount as decimal string, e.g. "99.99")
- currency: string (3-letter ISO code, default USD)
- items: array of {name: string, quantity: number, unit_amount: string}
- due_date: string|null (ISO date if found)
- type: "invoice"|"subscription"|"payout"
- confidence: number (0-1, how confident you are in this extraction)
Be strict about types, ensure amount is always a string, never null required fields."""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01"
                },
                json={
                    "model": "claude-3-sonnet-20240229",
                    "max_tokens": 1024,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": text}]
                }
            )
            response.raise_for_status()
            llm_output = response.json()
            extracted = json.loads(llm_output["content"][0]["text"])
            # Validate with Pydantic
            validated = PaymentIntent(**extracted)
            return validated.model_dump()
    except Exception as e:
        logger.error(f"Anthropic extraction failed: {str(e)}", exc_info=True)
        raise  # Re-raise to allow failover to next provider

async def _call_llm(text: str) -> Optional[Dict[str, Any]]:
    """Call LLM with failover between providers"""
    # Try OpenAI first
    try:
        return await _extract_with_openai(text)
    except Exception:
        # Failover to Anthropic
        try:
            return await _extract_with_anthropic(text)
        except Exception:
            # All LLMs failed
            return None

def get_text_hash(text: str) -> str:
    """Generate SHA-256 hash of raw input for audit logging"""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

async def extract_payment_intent(unstructured_text: str) -> PaymentIntent:
    """
    Main function to extract structured payment intent from unstructured text.
    First attempts LLM extraction, falls back to regex if LLM fails.
    Logs confidence and input hash for auditing.
    """
    text_hash = get_text_hash(unstructured_text)
    logger.info(f"Starting payment intent extraction, input hash: {text_hash}")

    # Try LLM first
    extracted = await _call_llm(unstructured_text)
    
    # Fall back to regex
    if not extracted:
        extracted_dict = _extract_with_regex(unstructured_text)
        # Add required fields for PaymentIntent schema
        extracted_dict["raw_input"] = unstructured_text
        extracted_dict["extracted_from"] = "regex_fallback"
        extracted_dict["raw_input_hash"] = text_hash
        extracted = PaymentIntent(**extracted_dict)
        logger.warning(f"Using regex fallback for extraction, hash: {text_hash}, confidence: {extracted.confidence}")
    else:
        # Ensure extracted is a PaymentIntent object
        if isinstance(extracted, dict):
            extracted = PaymentIntent(**extracted)
        logger.info(f"LLM extraction successful, hash: {text_hash}, confidence: {extracted.confidence}")

    # Add audit metadata as a dict field if needed, but return PaymentIntent object
    # The test expects a PaymentIntent instance, so we return it directly
    return extracted