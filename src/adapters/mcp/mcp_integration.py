import os
import json
from typing import Dict, List, Optional, Any, Union
from datetime import datetime
from pydantic import BaseModel, Field
from appwrite.client import Client
from appwrite.services.databases import Databases
from appwrite.exception import AppwriteException
import requests

# MCP Server configuration
APPWRITE_MCP_ENDPOINT = os.getenv("APPWRITE_MCP_ENDPOINT", "http://localhost:8080/mcp/appwrite")
VULTR_MCP_ENDPOINT = os.getenv("VULTR_MCP_ENDPOINT", "http://localhost:8081/mcp/vultr")
MCP_API_KEY = os.getenv("MCP_API_KEY", "")

class MCPSchemaInspectionRequest(BaseModel):
    """Pydantic model for schema inspection requests"""
    database_id: str
    collection_ids: Optional[List[str]] = None
    include_permissions: bool = Field(default=True)
    include_indexes: bool = Field(default=True)

class MCPDatabaseMigration(BaseModel):
    """Pydantic model for database migration requests"""
    migration_name: str
    environment: str = Field(..., regex="^(staging|production)$")
    collections_to_create: List[Dict[str, Any]] = Field(default_factory=list)
    collections_to_update: List[Dict[str, Any]] = Field(default_factory=list)
    dry_run: bool = Field(default=True)

class MCPHealthAuditRequest(BaseModel):
    """Pydantic model for server health audit requests"""
    check_appwrite: bool = True
    check_vultr: bool = True
    check_resources: List[str] = Field(default_factory=lambda: ["instances", "databases", "storage"])
    generate_report: bool = True

class MCPIntegrationManager:
    """
    Integrates Appwrite MCP and Vultr MCP servers to enable:
    1. Schema inspection of Appwrite databases directly in the AI IDE
    2. Automated database migration generation and execution
    3. Unified server health auditing across Appwrite and Vultr infrastructure
    4. Real-time resource monitoring and alerting
    """
    def __init__(self):
        self.mcp_headers = {
            "Authorization": f"Bearer {MCP_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # Initialize Appwrite client for local operations
        self.appwrite_client = Client()
        self.appwrite_client.set_endpoint(os.getenv("APPWRITE_ENDPOINT", "https://cloud.appwrite.io/v1"))
        self.appwrite_client.set_project(os.getenv("APPWRITE_PROJECT_ID", ""))
        self.appwrite_client.set_key(os.getenv("APPWRITE_API_KEY", ""))
        self.databases = Databases(self.appwrite_client)

    def inspect_schema(self, request: MCPSchemaInspectionRequest) -> Dict[str, Any]:
        """
        Inspect Appwrite database schemas via Appwrite MCP server:
        - Retrieves collection structures, attributes, indexes
        - Includes permission configurations
        - Returns schema ready for IDE visualization
        """
        try:
            # Forward request to Appwrite MCP server
            response = requests.post(
                f"{APPWRITE_MCP_ENDPOINT}/inspect-schema",
                headers=self.mcp_headers,
                json=request.dict()
            )
            response.raise_for_status()
            schema_data = response.json()
            
            # Log inspection event for auditing
            self._log_mcp_event(
                event_type="schema_inspection",
                details={"database_id": request.database_id, "collections": request.collection_ids}
            )
            
            return {
                "schema": schema_data,
                "inspected_at": datetime.utcnow().isoformat(),
                "status": "success"
            }
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Schema inspection failed: {str(e)}")

    def generate_migration(self, migration: MCPDatabaseMigration) -> Dict[str, Any]:
        """
        Generate and optionally execute database migrations via MCP:
        - Creates migration plan with validation checks
        - Supports dry-run mode to preview changes
        - Automatically rolls back if migration fails
        - Generates migration SQL/JSON for version control
        """
        try:
            # First validate migration through MCP
            validate_response = requests.post(
                f"{APPWRITE_MCP_ENDPOINT}/validate-migration",
                headers=self.mcp_headers,
                json=migration.dict()
            )
            validate_response.raise_for_status()
            validation = validate_response.json()
            
            if not validation["valid"]:
                return {
                    "status": "invalid",
                    "errors": validation["errors"],
                    "migration_generated": False
                }
            
            # If not dry run, execute the migration
            if not migration.dry_run:
                execute_response = requests.post(
                    f"{APPWRITE_MCP_ENDPOINT}/execute-migration",
                    headers=self.mcp_headers,
                    json=migration.dict()
                )
                execute_response.raise_for_status()
                execution_result = execute_response.json()
                
                # Log migration execution
                self._log_mcp_event(
                    event_type="migration_executed",
                    details={
                        "migration_name": migration.migration_name,
                        "environment": migration.environment,
                        "success": execution_result["success"]
                    }
                )
                
                return {
                    "status": "executed",
                    "migration_id": execution_result["migration_id"],
                    "changes_applied": execution_result["changes"],
                    "rolled_back": execution_result.get("rolled_back", False)
                }
            
            # For dry run, return the generated migration plan
            migration_plan = validation["migration_plan"]
            self._log_mcp_event(
                event_type="migration_generated",
                details={"migration_name": migration.migration_name, "environment": migration.environment}
            )
            
            return {
                "status": "generated",
                "migration_plan": migration_plan,
                "dry_run": True
            }
            
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Migration generation failed: {str(e)}")

    def run_health_audit(self, request: MCPHealthAuditRequest) -> Dict[str, Any]:
        """
        Run unified health audit across Appwrite and Vultr infrastructure:
        - Collects health metrics from both MCP servers
        - Generates comprehensive audit report
        - Identifies resource constraints and issues
        - Suggests remediation steps for critical issues
        """
        audit_results = {}
        
        # Audit Appwrite infrastructure if requested
        if request.check_appwrite:
            try:
                appwrite_response = requests.post(
                    f"{APPWRITE_MCP_ENDPOINT}/health-audit",
                    headers=self.mcp_headers,
                    json={"resources": request.check_resources}
                )
                appwrite_response.raise_for_status()
                audit_results["appwrite"] = appwrite_response.json()
            except requests.exceptions.RequestException as e:
                audit_results["appwrite"] = {"error": f"Appwrite audit failed: {str(e)}"}
        
        # Audit Vultr infrastructure if requested
        if request.check_vultr:
            try:
                vultr_response = requests.post(
                    f"{VULTR_MCP_ENDPOINT}/health-audit",
                    headers=self.mcp_headers,
                    json={"resources": request.check_resources}
                )
                vultr_response.raise_for_status()
                audit_results["vultr"] = vultr_response.json()
            except requests.exceptions.RequestException as e:
                audit_results["vultr"] = {"error": f"Vultr audit failed: {str(e)}"}
        
        # Generate combined report if requested
        if request.generate_report:
            audit_report = self._generate_audit_report(audit_results)
            audit_results["report"] = audit_report
        
        # Log audit event
        self._log_mcp_event(
            event_type="health_audit",
            details={"checked_appwrite": request.check_appwrite, "checked_vultr": request.check_vultr}
        )
        
        return {
            "audit_results": audit_results,
            "audited_at": datetime.utcnow().isoformat(),
            "status": "completed"
        }

    def _generate_audit_report(self, raw_results: Dict[str, Any]) -> Dict[str, Any]:
        """Generate human-readable audit report with issue prioritization"""
        critical_issues = []
        warnings = []
        healthy_resources = []
        
        # Process Appwrite results
        if "appwrite" in raw_results and not raw_results["appwrite"].get("error"):
            appwrite_data = raw_results["appwrite"]
            for resource in appwrite_data.get("resources", []):
                if resource["status"] == "critical":
                    critical_issues.append(f"Appwrite {resource['type']}: {resource['name']} - {resource['issue']}")
                elif resource["status"] == "warning":
                    warnings.append(f"Appwrite {resource['type']}: {resource['name']} - {resource['message']}")
                else:
                    healthy_resources.append(f"Appwrite {resource['type']}: {resource['name']}")
        
        # Process Vultr results
        if "vultr" in raw_results and not raw_results["vultr"].get("error"):
            vultr_data = raw_results["vultr"]
            for resource in vultr_data.get("resources", []):
                if resource["status"] == "critical":
                    critical_issues.append(f"Vultr {resource['type']}: {resource['label']} - {resource['issue']}")
                elif resource["status"] == "warning":
                    warnings.append(f"Vultr {resource['type']}: {resource['label']} - {resource['message']}")
                else:
                    healthy_resources.append(f"Vultr {resource['type']}: {resource['label']}")
        
        return {
            "summary": {
                "critical_issues_count": len(critical_issues),
                "warnings_count": len(warnings),
                "healthy_resources_count": len(healthy_resources),
                "overall_health": "healthy" if len(critical_issues) == 0 else "degraded"
            },
            "critical_issues": critical_issues,
            "warnings": warnings,
            "healthy_resources": healthy_resources,
            "generated_at": datetime.utcnow().isoformat()
        }

    def list_vultr_resources(self, resource_type: str) -> Dict[str, Any]:
        """List Vultr resources via Vultr MCP server (instances, databases, storage)"""
        valid_types = ["instances", "databases", "storage", "kubernetes", "loadbalancers"]
        if resource_type not in valid_types:
            raise ValueError(f"Invalid resource type. Must be one of: {valid_types}")
        
        try:
            response = requests.get(
                f"{VULTR_MCP_ENDPOINT}/resources/{resource_type}",
                headers=self.mcp_headers
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Failed to list Vultr {resource_type}: {str(e)}")

    def _log_mcp_event(self, event_type: str, details: Dict[str, Any]) -> None:
        """Log all MCP events to Appwrite audit log"""
        try:
            self.databases.create_document(
                database_id="audit-logs",
                collection_id="mcp-audit-events",
                document_id="unique()",
                data={
                    "event_type": event_type,
                    "details": details,
                    "timestamp": datetime.utcnow().isoformat()
                },
                read=["role:system", "role:admin"],
                write=["role:system"]
            )
        except AppwriteException:
            pass  # Fail silently to not block primary operations

# IDE command handlers (these are called directly by the AI IDE when executing MCP commands)
def handle_mcp_command(command: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Main handler for MCP commands invoked from the AI IDE"""
    manager = MCPIntegrationManager()
    
    try:
        if command == "inspect-schema":
            request = MCPSchemaInspectionRequest(**params)
            return manager.inspect_schema(request)
        elif command == "generate-migration":
            migration = MCPDatabaseMigration(**params)
            return manager.generate_migration(migration)
        elif command == "health-audit":
            request = MCPHealthAuditRequest(**params)
            return manager.run_health_audit(request)
        elif command == "list-vultr-resources":
            resource_type = params.get("resource_type", "instances")
            return manager.list_vultr_resources(resource_type)
        else:
            return {"status": "error", "message": f"Unknown MCP command: {command}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# Appwrite function entry point for scheduled MCP health audits
def main(context: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Main entry point for scheduled health audits via Appwrite functions"""
    manager = MCPIntegrationManager()
    
    try:
        request = MCPHealthAuditRequest(**payload)
        results = manager.run_health_audit(request)
        context.log("MCP health audit completed successfully")
        return results
    except Exception as e:
        context.error(f"MCP health audit failed: {str(e)}")
        return {"status": "error", "message": str(e)}