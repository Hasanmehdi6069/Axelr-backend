"""Appwrite database migration scripts to create required collections for payments and support"""

import logging
import os
from typing import Any, Dict, List

from appwrite.client import Client
from appwrite.exception import AppwriteException
from appwrite.services.databases import Databases

logger = logging.getLogger("axelr.db_migrations")


class DatabaseMigrator:
    """
    Handles creation and configuration of all required Appwrite collections
    for our payment automation and support triage systems. Creates collections
    with gateway-agnostic schemas that support future migration to Juspay/Hyperswitch.
    """

    def __init__(self):
        """Initialize Appwrite client"""
        self.client = Client()
        self.client.set_project(os.getenv("APPWRITE_PROJECT_ID"))
        self.client.set_key(os.getenv("APPWRITE_API_KEY"))
        self.client.set_endpoint(os.getenv("APPWRITE_ENDPOINT", "https://cloud.appwrite.io/v1"))

        self.databases = Databases(self.client)
        self.database_id = os.getenv("APPWRITE_DATABASE_ID")

        # Collection IDs that will be created
        self.collections = {
            "transactions": "transactions",
            "subscriptions": "subscriptions",
            "support_tickets": "support_tickets",
            "audit_logs": "audit_logs",
        }

    def get_transactions_attributes(self) -> List[Dict[str, Any]]:
        """Define attributes for the transactions collection - gateway-agnostic design"""
        return [
            {"key": "transaction_id", "type": "string", "required": True, "array": False, "size": 255},
            {"key": "user_id", "type": "string", "required": False, "array": False, "size": 255},
            {"key": "user_email", "type": "string", "required": True, "array": False, "size": 255},
            {"key": "amount", "type": "double", "required": True, "array": False},
            {"key": "currency", "type": "string", "required": True, "array": False, "size": 10, "default": "INR"},
            {
                "key": "payment_gateway",
                "type": "string",
                "required": True,
                "array": False,
                "size": 50,
            },  # Supports: paytm_upi, jio, hyperswitch, etc.
            {"key": "status", "type": "string", "required": True, "array": False, "size": 32, "default": "pending"},
            {"key": "transaction_timestamp", "type": "datetime", "required": True, "array": False},
            {"key": "email_received_timestamp", "type": "datetime", "required": True, "array": False},
            {"key": "verified_at", "type": "datetime", "required": False, "array": False},
            {
                "key": "payer_details",
                "type": "string",
                "required": False,
                "array": False,
                "size": 65535,
            },  # JSON stored as string
            {
                "key": "verification_metadata",
                "type": "string",
                "required": False,
                "array": False,
                "size": 65535,
            },  # JSON stored as string
            {
                "key": "gateway_metadata",
                "type": "string",
                "required": False,
                "array": False,
                "size": 65535,
            },  # Future: gateway-specific data
        ]

    def get_subscriptions_attributes(self) -> List[Dict[str, Any]]:
        """Define attributes for the subscriptions collection"""
        return [
            {"key": "user_id", "type": "string", "required": True, "array": False, "size": 255},
            {"key": "credits_added", "type": "double", "required": True, "array": False},
            {"key": "subscription_tier", "type": "string", "required": True, "array": False, "size": 50},
            {"key": "valid_until", "type": "datetime", "required": True, "array": False},
            {"key": "transaction_id", "type": "string", "required": True, "array": False, "size": 255},
            {"key": "updated_at", "type": "datetime", "required": True, "array": False},
        ]

    def get_support_tickets_attributes(self) -> List[Dict[str, Any]]:
        """Define attributes for the support_tickets collection"""
        return [
            {"key": "ticket_id", "type": "string", "required": True, "array": False, "size": 255},
            {"key": "user_id", "type": "string", "required": False, "array": False, "size": 255},
            {"key": "user_email", "type": "string", "required": True, "array": False, "size": 255},
            {"key": "subject", "type": "string", "required": True, "array": False, "size": 255},
            {"key": "content", "type": "string", "required": True, "array": False, "size": 1048576},
            {"key": "category", "type": "string", "required": True, "array": False, "size": 50},
            {"key": "priority", "type": "string", "required": True, "array": False, "size": 20, "default": "medium"},
            {"key": "status", "type": "string", "required": True, "array": False, "size": 32, "default": "open"},
            {"key": "created_at", "type": "datetime", "required": True, "array": False},
            {"key": "assigned_to", "type": "string", "required": False, "array": False, "size": 255},
            {
                "key": "metadata",
                "type": "string",
                "required": False,
                "array": False,
                "size": 65535,
            },  # JSON stored as string
        ]

    def get_audit_logs_attributes(self) -> List[Dict[str, Any]]:
        """Define attributes for the audit_logs collection (security and operations tracking)"""
        return [
            {"key": "event_type", "type": "string", "required": True, "array": False, "size": 128},
            {"key": "timestamp", "type": "datetime", "required": True, "array": False},
            {"key": "actor_id", "type": "string", "required": False, "array": False, "size": 255},
            {"key": "actor_email", "type": "string", "required": False, "array": False, "size": 255},
            {"key": "resource_type", "type": "string", "required": False, "array": False, "size": 50},
            {"key": "resource_id", "type": "string", "required": False, "array": False, "size": 255},
            {"key": "ip_address", "type": "string", "required": False, "array": False, "size": 45},
            {"key": "metadata", "type": "string", "required": False, "array": False, "size": 65535},
            {"key": "success", "type": "boolean", "required": True, "array": False, "default": True},
        ]

    def collection_exists(self, collection_id: str) -> bool:
        """Check if a collection already exists in the database"""
        try:
            self.databases.get_collection(database_id=self.database_id, collection_id=collection_id)
            return True
        except AppwriteException as e:
            if e.code == 404:
                return False
            raise  # Re-raise other errors

    def create_collection(self, collection_id: str, collection_name: str, attributes: List[Dict[str, Any]]) -> None:
        """Create a collection with all required attributes"""
        if self.collection_exists(collection_id):
            logger.info(f"Collection {collection_id} already exists, skipping creation")
            return

        logger.info(f"Creating collection: {collection_id}")

        # Create the collection
        self.databases.create_collection(
            database_id=self.database_id,
            collection_id=collection_id,
            name=collection_name,
            permission="document",
            read=["role:system"],  # Strict least-privilege: only system can read by default
            write=["role:system"],  # Strict least-privilege: only system can write by default
        )

        # Add all attributes
        for attr in attributes:
            logger.debug(f"Adding attribute {attr['key']} to {collection_id}")
            try:
                if attr["type"] == "string":
                    self.databases.create_string_attribute(
                        database_id=self.database_id,
                        collection_id=collection_id,
                        key=attr["key"],
                        required=attr["required"],
                        array=attr["array"],
                        size=attr.get("size", 255),
                        default=attr.get("default"),
                    )
                elif attr["type"] == "double":
                    self.databases.create_float_attribute(
                        database_id=self.database_id,
                        collection_id=collection_id,
                        key=attr["key"],
                        required=attr["required"],
                        array=attr["array"],
                        min=attr.get("min"),
                        max=attr.get("max"),
                    )
                elif attr["type"] == "datetime":
                    self.databases.create_datetime_attribute(
                        database_id=self.database_id,
                        collection_id=collection_id,
                        key=attr["key"],
                        required=attr["required"],
                        array=attr["array"],
                        default=attr.get("default"),
                    )
                elif attr["type"] == "boolean":
                    self.databases.create_boolean_attribute(
                        database_id=self.database_id,
                        collection_id=collection_id,
                        key=attr["key"],
                        required=attr["required"],
                        array=attr["array"],
                        default=attr.get("default"),
                    )
            except AppwriteException as e:
                logger.warning(f"Error creating attribute {attr['key']}: {e}")
                continue

    def create_indexes(self, collection_id: str) -> None:
        """Create necessary indexes for query performance"""
        indexes = [
            # Common indexes for all collections
            {"key": "created_at_idx", "type": "key", "attributes": ["$createdAt"], "orders": ["DESC"]},
        ]

        # Collection-specific indexes
        if collection_id == "transactions":
            indexes.extend(
                [
                    {
                        "key": "transaction_id_idx",
                        "type": "unique",
                        "attributes": ["transaction_id"],
                        "orders": ["ASC"],
                    },
                    {"key": "user_id_idx", "type": "key", "attributes": ["user_id"], "orders": ["ASC"]},
                    {"key": "user_email_idx", "type": "key", "attributes": ["user_email"], "orders": ["ASC"]},
                    {"key": "status_idx", "type": "key", "attributes": ["status"], "orders": ["ASC"]},
                    {
                        "key": "transaction_timestamp_idx",
                        "type": "key",
                        "attributes": ["transaction_timestamp"],
                        "orders": ["DESC"],
                    },
                ]
            )
        elif collection_id == "subscriptions":
            indexes.extend(
                [
                    {"key": "user_id_idx", "type": "key", "attributes": ["user_id"], "orders": ["ASC"]},
                    {
                        "key": "transaction_id_idx",
                        "type": "unique",
                        "attributes": ["transaction_id"],
                        "orders": ["ASC"],
                    },
                    {"key": "valid_until_idx", "type": "key", "attributes": ["valid_until"], "orders": ["ASC"]},
                ]
            )
        elif collection_id == "support_tickets":
            indexes.extend(
                [
                    {"key": "ticket_id_idx", "type": "unique", "attributes": ["ticket_id"], "orders": ["ASC"]},
                    {"key": "user_email_idx", "type": "key", "attributes": ["user_email"], "orders": ["ASC"]},
                    {"key": "category_idx", "type": "key", "attributes": ["category"], "orders": ["ASC"]},
                    {"key": "priority_idx", "type": "key", "attributes": ["priority"], "orders": ["ASC"]},
                    {"key": "status_idx", "type": "key", "attributes": ["status"], "orders": ["ASC"]},
                ]
            )
        elif collection_id == "audit_logs":
            indexes.extend(
                [
                    {"key": "event_type_idx", "type": "key", "attributes": ["event_type"], "orders": ["ASC"]},
                    {"key": "actor_id_idx", "type": "key", "attributes": ["actor_id"], "orders": ["ASC"]},
                    {"key": "timestamp_idx", "type": "key", "attributes": ["timestamp"], "orders": ["DESC"]},
                    {"key": "resource_id_idx", "type": "key", "attributes": ["resource_id"], "orders": ["ASC"]},
                ]
            )

        # Create all indexes
        for idx in indexes:
            try:
                self.databases.create_index(
                    database_id=self.database_id,
                    collection_id=collection_id,
                    key=idx["key"],
                    type=idx["type"],
                    attributes=idx["attributes"],
                    orders=idx["orders"],
                )
                logger.debug(f"Created index {idx['key']} on {collection_id}")
            except AppwriteException as e:
                if "already exists" in str(e).lower() or e.code == 409:
                    logger.debug(f"Index {idx['key']} already exists")
                else:
                    logger.warning(f"Failed to create index {idx['key']}: {e}")

    def setup_permissions(self, collection_id: str) -> None:
        """
        Configure least-privilege permissions for each collection.
        Implements zero-trust access model: only specific roles can access.
        """
        # Base permissions - system role always has full access
        read_permissions = ["role:system"]
        write_permissions = ["role:system"]

        # Add appropriate roles based on collection
        if collection_id in ["transactions", "subscriptions"]:
            # Finance and billing team can read payment data
            read_permissions.extend(["role:finance", "role:billing"])
            # Only system and specific admins can modify payment data
            write_permissions.extend(["role:billing_admin"])
        elif collection_id == "support_tickets":
            # Support team can read and write all tickets
            read_permissions.extend(["role:support", "role:customer_success"])
            write_permissions.extend(["role:support", "role:support_lead"])
        elif collection_id == "audit_logs":
            # Only security and compliance can access audit logs
            read_permissions.extend(["role:security", "role:compliance"])
            # No one can modify audit logs - immutable
            write_permissions = ["role:system"]  # Only system can write, no modifications

        # Update collection permissions
        try:
            self.databases.update_collection(
                database_id=self.database_id,
                collection_id=collection_id,
                read=read_permissions,
                write=write_permissions,
            )
            logger.info(f"Updated permissions for {collection_id}: read={read_permissions}, write={write_permissions}")
        except AppwriteException as e:
            logger.error(f"Failed to update permissions for {collection_id}: {e}")

    def run_migrations(self) -> Dict[str, Any]:
        """Execute all database migrations"""
        results = {"success": True, "collections_created": [], "collections_skipped": [], "errors": []}

        # Collections to create with their configurations
        collection_configs = [
            {
                "id": self.collections["transactions"],
                "name": "Transactions",
                "attributes": self.get_transactions_attributes(),
            },
            {
                "id": self.collections["subscriptions"],
                "name": "Subscriptions",
                "attributes": self.get_subscriptions_attributes(),
            },
            {
                "id": self.collections["support_tickets"],
                "name": "Support Tickets",
                "attributes": self.get_support_tickets_attributes(),
            },
            {
                "id": self.collections["audit_logs"],
                "name": "Audit Logs",
                "attributes": self.get_audit_logs_attributes(),
            },
        ]

        for config in collection_configs:
            try:
                if not self.collection_exists(config["id"]):
                    self.create_collection(config["id"], config["name"], config["attributes"])
                    self.create_indexes(config["id"])
                    self.setup_permissions(config["id"])
                    results["collections_created"].append(config["id"])
                    logger.info(f"Successfully set up collection: {config['id']}")
                else:
                    # Even if collection exists, update permissions and indexes
                    self.create_indexes(config["id"])
                    self.setup_permissions(config["id"])
                    results["collections_skipped"].append(config["id"])
                    logger.info(f"Collection {config['id']} exists, updated indexes/permissions")
            except Exception as e:
                error_msg = f"Failed to set up {config['id']}: {str(e)}"
                logger.error(error_msg)
                results["errors"].append(error_msg)
                results["success"] = False

        return results


# CLI entry point for running migrations
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    migrator = DatabaseMigrator()
    results = migrator.run_migrations()

    if results["success"]:
        logger.info("All migrations completed successfully!")
        logger.info(f"Created: {results['collections_created']}")
        logger.info(f"Skipped: {results['collections_skipped']}")
    else:
        logger.error("Some migrations failed:")
        for error in results["errors"]:
            logger.error(f"  - {error}")
        exit(1)
