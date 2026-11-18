"""Appwrite Inbound Email Function for automated Paytm/UPI payment verification
Processes incoming payment confirmation emails, extracts transaction details,
validates them, and updates user subscriptions/credits in Appwrite.
"""
from __future__ import annotations

import os
import re
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple
from appwrite.client import Client
from appwrite.services.databases import Databases
from appwrite.id import ID
from pydantic import ValidationError

from .schemas import Transaction, TransactionStatus, PaymentGateway, Subscription
from .retry import with_exponential_backoff

logger = logging.getLogger("axelr.payment_processor")

# Environment variables - configured in Appwrite Function
REQUIRED_ENV_VARS = {
    "APPWRITE_ENDPOINT",
    "APPWRITE_PROJECT_ID", 
    "APPWRITE_API_KEY",
    "APPWRITE_DATABASE_ID",
    "TRANSACTIONS_COLLECTION_ID",
    "SUBSCRIPTIONS_COLLECTION_ID",
    "USERS_COLLECTION_ID",
    "PAYTM_ALLOWED_SENDERS",  # Comma-separated: noreply@paytm.com,alerts@paytm.com
    "UPI_ALLOWED_SENDERS",    # Comma-separated: alerts@icicibank.com,notify@sbi.co.in
    "MERCHANT_VPA",          # Our registered UPI VPA to verify payments are to us
}

# Regex patterns for extracting data from Paytm/UPI emails
UTR_PATTERN = re.compile(r'UTR\s*[:-]?\s*([A-Z0-9]{10,20})', re.IGNORECASE)
AMOUNT_PATTERN = re.compile(r'₹\s*([\d,]+\.?\d{0,2})', re.IGNORECASE)
PAYER_VPA_PATTERN = re.compile(r'from\s*(\w+@\w+)', re.IGNORECASE)
TIMESTAMP_PATTERNS = [
    re.compile(r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s+\d{1,2}:\d{2})', re.IGNORECASE),
    re.compile(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})'),
]

def init_appwrite_client() -> Tuple[Client, Databases]:
    """Initialize and return Appwrite client and databases service"""
    missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing_vars:
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing_vars)}")

    client = Client()
    client.set_endpoint(os.getenv("APPWRITE_ENDPOINT"))
    client.set_project(os.getenv("APPWRITE_PROJECT_ID"))
    client.set_key(os.getenv("APPWRITE_API_KEY"))
    
    return client, Databases(client)

def parse_email_content(subject: str, body: str, from_email: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Extract transaction details from email content. Returns (transaction_data, error)"""
    transaction_data = {
        "email_source": from_email,
        "email_subject": subject,
        "metadata": {}
    }

    # Determine gateway from sender email
    paytm_senders = set(os.getenv("PAYTM_ALLOWED_SENDERS", "").split(","))
    upi_senders = set(os.getenv("UPI_ALLOWED_SENDERS", "").split(","))
    
    if from_email in paytm_senders:
        transaction_data["gateway"] = PaymentGateway.PAYTM_QR
    elif from_email in upi_senders:
        transaction_data["gateway"] = PaymentGateway.UPI
    else:
        return None, f"Unauthorized sender email: {from_email}"

    # Extract UTR
    utr_match = UTR_PATTERN.search(body)
    if not utr_match:
        return None, "Could not extract UTR from email"
    transaction_data["utr"] = utr_match.group(1).strip()
    transaction_data["transaction_id"] = transaction_data["utr"]  # Use UTR as unique transaction ID

    # Extract amount
    amount_match = AMOUNT_PATTERN.search(body)
    if not amount_match:
        return None, "Could not extract amount from email"
    amount_str = amount_match.group(1).replace(",", "")
    try:
        transaction_data["amount"] = float(amount_str)
    except ValueError:
        return None, f"Invalid amount format: {amount_str}"

    # Extract payer VPA
    payer_match = PAYER_VPA_PATTERN.search(body)
    if payer_match:
        transaction_data["payer_vpa"] = payer_match.group(1).strip()
    
    # Verify our merchant VPA is in the email (payment was to us)
    merchant_vpa = os.getenv("MERCHANT_VPA")
    if merchant_vpa and merchant_vpa not in body:
        return None, f"Merchant VPA {merchant_vpa} not found in payment - possible misdirected payment"
    transaction_data["payee_vpa"] = merchant_vpa

    # Extract transaction timestamp
    transaction_time = None
    for pattern in TIMESTAMP_PATTERNS:
        ts_match = pattern.search(body)
        if ts_match:
            try:
                # Parse timestamp - this is simplified, would need proper parsing for different formats
                transaction_time = datetime.strptime(ts_match.group(1), "%d %b %Y %H:%M").replace(tzinfo=timezone.utc)
                break
            except ValueError:
                try:
                    transaction_time = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
    
    if not transaction_time:
        # Fallback to current time if we can't parse
        transaction_time = datetime.now(timezone.utc)
    transaction_data["transaction_timestamp"] = transaction_time

    # Try to identify user - this would need more sophisticated user matching in production
    # For now, we'll leave user_id to be associated later or implement proper matching
    transaction_data["user_id"] = "unidentified_user"  # Will be updated in production

    return transaction_data, None

@with_exponential_backoff(max_retries=3)
async def check_duplicate_transaction(databases: Databases, utr: str) -> bool:
    """Check if transaction with this UTR already exists to prevent duplicates"""
    try:
        result = databases.list_documents(
            database_id=os.getenv("APPWRITE_DATABASE_ID"),
            collection_id=os.getenv("TRANSACTIONS_COLLECTION_ID"),
            queries=[f"utr={utr}", "limit=1"]
        )
        return len(result.get("documents", [])) > 0
    except Exception as e:
        logger.error(f"Error checking for duplicate transaction: {e}")
        raise  # Let retry handle it

@with_exponential_backoff(max_retries=3)
async def create_transaction(databases: Databases, transaction_data: Dict[str, Any]) -> str:
    """Create transaction document in Appwrite"""
    try:
        # Validate with Pydantic
        transaction = Transaction(**transaction_data)
        
        # Create in Appwrite
        result = databases.create_document(
            database_id=os.getenv("APPWRITE_DATABASE_ID"),
            collection_id=os.getenv("TRANSACTIONS_COLLECTION_ID"),
            document_id=ID.unique(),
            data=transaction.model_dump()
        )
        return result["$id"]
    except ValidationError as e:
        logger.error(f"Transaction validation failed: {e}")
        raise ValueError(f"Invalid transaction data: {e}")
    except Exception as e:
        logger.error(f"Error creating transaction: {e}")
        raise

@with_exponential_backoff(max_retries=3)
async def mark_transaction_verified(databases: Databases, document_id: str) -> None:
    """Mark transaction as verified and set verified_at timestamp"""
    try:
        databases.update_document(
            database_id=os.getenv("APPWRITE_DATABASE_ID"),
            collection_id=os.getenv("TRANSACTIONS_COLLECTION_ID"),
            document_id=document_id,
            data={
                "status": TransactionStatus.VERIFIED,
                "verified_at": datetime.now(timezone.utc).isoformat()
            }
        )
        logger.info(f"Transaction {document_id} marked as verified")
    except Exception as e:
        logger.error(f"Error updating transaction status: {e}")
        raise

@with_exponential_backoff(max_retries=3)
async def update_user_subscription(databases: Databases, user_id: str, transaction: Transaction) -> None:
    """Update user's subscription or credits based on the verified payment"""
    if user_id == "unidentified_user":
        logger.warning(f"Cannot update subscription for unidentified user, transaction: {transaction.transaction_id}")
        return

    try:
        # Find user's current subscription
        result = databases.list_documents(
            database_id=os.getenv("APPWRITE_DATABASE_ID"),
            collection_id=os.getenv("SUBSCRIPTIONS_COLLECTION_ID"),
            queries=[f"user_id={user_id}", "limit=1"]
        )

        if result.get("documents"):
            # Update existing subscription
            subscription_doc = result["documents"][0]
            subscription = Subscription(**subscription_doc)
            
            # Example logic: add credits or extend subscription based on amount
            # In production, this would map amount to specific plans/credits
            credits_to_add = transaction.amount / 100  # ₹100 = 1 credit - example mapping
            current_credits = subscription.credits_remaining or 0
            updated_credits = current_credits + credits_to_add
            
            databases.update_document(
                database_id=os.getenv("APPWRITE_DATABASE_ID"),
                collection_id=os.getenv("SUBSCRIPTIONS_COLLECTION_ID"),
                document_id=subscription_doc["$id"],
                data={
                    "credits_remaining": updated_credits,
                    "last_payment_transaction_id": transaction.transaction_id,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }
            )
            logger.info(f"Updated credits for user {user_id}: +{credits_to_add}, new balance: {updated_credits}")
        else:
            # Create new subscription for user
            new_subscription = Subscription(
                user_id=user_id,
                plan="payg",  # Pay-as-you-go
                credits_remaining=transaction.amount / 100,
                last_payment_transaction_id=transaction.transaction_id,
                end_date=datetime.now(timezone.utc).isoformat()  # No expiration for paygo
            )
            
            databases.create_document(
                database_id=os.getenv("APPWRITE_DATABASE_ID"),
                collection_id=os.getenv("SUBSCRIPTIONS_COLLECTION_ID"),
                document_id=ID.unique(),
                data=new_subscription.model_dump()
            )
            logger.info(f"Created new subscription for user {user_id}")

    except Exception as e:
        logger.error(f"Error updating user subscription: {e}")
        raise

# Main Appwrite Function entry point
async def main(context: Any, email_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Appwrite Inbound Email Function entry point
    email_payload structure: https://appwrite.io/docs/functions/email#payload
    """
    logger.info("Received incoming email for payment processing")
    
    try:
        # Initialize Appwrite client
        _, databases = init_appwrite_client()
        
        # Extract email data
        from_email = email_payload.get("from", "")
        subject = email_payload.get("subject", "")
        body = email_payload.get("html", "") or email_payload.get("text", "")
        
        if not body:
            logger.error("Empty email body received")
            return {"success": False, "error": "Empty email body"}

        # Parse transaction details from email
        transaction_data, error = parse_email_content(subject, body, from_email)
        if error:
            logger.error(f"Failed to parse email: {error}")
            return {"success": False, "error": error}

        # Check for duplicates
        is_duplicate = await check_duplicate_transaction(databases, transaction_data["utr"])
        if is_duplicate:
            logger.warning(f"Duplicate transaction detected for UTR: {transaction_data['utr']}")
            transaction_data["status"] = TransactionStatus.DUPLICATE
            await create_transaction(databases, transaction_data)
            return {"success": False, "error": "Duplicate transaction detected"}

        # Create transaction record
        transaction_id = await create_transaction(databases, transaction_data)
        logger.info(f"Created new transaction record: {transaction_id}")

        # Mark transaction as verified
        await mark_transaction_verified(databases, transaction_id)
        
        # Retrieve the full transaction object
        transaction_doc = databases.get_document(
            database_id=os.getenv("APPWRITE_DATABASE_ID"),
            collection_id=os.getenv("TRANSACTIONS_COLLECTION_ID"),
            document_id=transaction_id
        )
        transaction = Transaction(**transaction_doc)

        # Update user's subscription/credits
        await update_user_subscription(databases, transaction.user_id, transaction)

        logger.info(f"Successfully processed payment: {transaction.utr}, amount: ₹{transaction.amount}")
        return {
            "success": True,
            "transaction_id": transaction_id,
            "utr": transaction.utr,
            "amount": transaction.amount
        }

    except Exception as e:
        logger.error(f"Fatal error processing payment: {str(e)}", exc_info=True)
        context.error(f"Payment processing failed: {str(e)}")
        return {"success": False, "error": str(e)}