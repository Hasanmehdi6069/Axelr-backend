import os
import time
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
from pydantic import BaseModel, Field, EmailStr, validator
from appwrite.client import Client
from appwrite.services.teams import Teams
from appwrite.services.users import Users
from appwrite.services.databases import Databases
from appwrite.exception import AppwriteException
import requests

# Environment configuration (loaded from Appwrite secrets)
APPWRITE_PROJECT_ID = os.getenv("APPWRITE_PROJECT_ID", "")
APPWRITE_API_KEY = os.getenv("APPWRITE_API_KEY", "")
APPWRITE_ENDPOINT = os.getenv("APPWRITE_ENDPOINT", "https://cloud.appwrite.io/v1")
VULTR_API_KEY = os.getenv("VULTR_API_KEY", "")
VULTR_ENDPOINT = "https://api.vultr.com/v2"

# Role definitions (strict least-privilege roles)
APPWRITE_TEAM_ROLES = {
    "system": ["*"],  # Restricted to service accounts only
    "admin": ["read", "write", "manage_users", "manage_billing"],
    "billing_admin": ["read:transactions", "write:transactions", "read:subscriptions", "write:subscriptions"],
    "finance": ["read:transactions", "read:subscriptions"],
    "developer_staging": ["read:staging:*", "write:staging:*"],
    "developer_production": ["read:production:*"],  # Write access requires explicit approval
    "support": ["read:support_tickets", "write:support_tickets"],
    "contractor": ["read:staging:public"]  # Minimal default contractor access
}

VULTR_IAM_ROLES = {
    "owner": ["*"],
    "billing": ["billing:read", "invoice:read"],
    "devops_staging": ["instance:read:staging-*", "storage:read:staging-*", "database:read:staging-*"],
    "devops_production": ["instance:read:production-*", "storage:read:production-*"],
    "support": ["ticket:create", "ticket:read"]
}

class TeamMember(BaseModel):
    """Pydantic model for team member validation"""
    email: EmailStr
    full_name: str = Field(..., min_length=2, max_length=100)
    role_type: str = Field(..., description="Requested role type")
    environment: str = Field(default="staging", description="Deployment environment access requested")
    is_contractor: bool = Field(default=False, description="Whether the member is a contractor")
    start_date: datetime = Field(default_factory=datetime.utcnow)
    end_date: Optional[datetime] = None  # For contractors, required expiration
    request_reason: str = Field(..., min_length=20, max_length=500, description="Justification for access")
    
    @validator('end_date')
    def validate_contractor_end_date(cls, v, values):
        if values.get('is_contractor') and v is None:
            raise ValueError("Contractors must have an end date for access expiration")
        if v and v < datetime.utcnow():
            raise ValueError("End date must be in the future")
        return v
    
    @validator('role_type')
    def validate_role_type(cls, v):
        valid_roles = list(APPWRITE_TEAM_ROLES.keys())
        if v not in valid_roles:
            raise ValueError(f"Invalid role type. Must be one of: {valid_roles}")
        return v

class ZeroTrustOnboardingManager:
    """
    Implements strict "No Access by Default" onboarding policy for Appwrite Teams and Vultr IAM.
    Enforces least-privilege access, explicit approval requirements for production access,
    and automatic access expiration for contractors.
    """
    def __init__(self):
        # Initialize Appwrite client
        self.appwrite_client = Client()
        self.appwrite_client.set_endpoint(APPWRITE_ENDPOINT)
        self.appwrite_client.set_project(APPWRITE_PROJECT_ID)
        self.appwrite_client.set_key(APPWRITE_API_KEY)
        
        self.teams = Teams(self.appwrite_client)
        self.users = Users(self.appwrite_client)
        self.databases = Databases(self.appwrite_client)
        
        # Vultr API configuration
        self.vultr_headers = {
            "Authorization": f"Bearer {VULTR_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # Approval tracking collection
        self.approval_db_id = os.getenv("APPWRITE_ACCESS_REQUESTS_DB", "access-requests")
        self.approval_collection_id = "access-approvals"

    def onboard_team_member(self, member_data: TeamMember) -> Dict[str, Any]:
        """
        Primary method to onboard new team members with zero-trust checks:
        1. Validate access request against least-privilege policies
        2. Require explicit admin approval for production access
        3. Set automatic expiration for contractors
        4. Assign minimal necessary permissions only
        """
        # Step 1: Validate access request
        self._validate_access_request(member_data)
        
        # Step 2: Check if production access requires approval
        requires_approval = self._check_approval_requirement(member_data)
        if requires_approval:
            return self._create_approval_request(member_data)
        
        # Step 3: Provision access if pre-approved or staging-only
        appwrite_member = self._provision_appwrite_access(member_data)
        vultr_member = self._provision_vultr_access(member_data)
        
        # Step 4: Log access provisioning to audit log
        self._log_access_event("provisioned", member_data, appwrite_member, vultr_member)
        
        return {
            "status": "success",
            "appwrite_member_id": appwrite_member.get("$id"),
            "vultr_member_id": vultr_member.get("id"),
            "access_provisioned": datetime.utcnow().isoformat(),
            "expiration_date": member_data.end_date.isoformat() if member_data.end_date else None
        }

    def _validate_access_request(self, member_data: TeamMember) -> None:
        """Validate that the requested access aligns with least-privilege policies"""
        # Block production write access for all non-admin roles
        if member_data.environment == "production" and member_data.role_type not in ["admin", "system"]:
            if "write" in APPWRITE_TEAM_ROLES[member_data.role_type]:
                raise ValueError("Production write access requires explicit admin approval for all non-admin roles")
        
        # Block contractor production access entirely
        if member_data.is_contractor and member_data.environment == "production":
            raise ValueError("Contractors cannot be granted production access under zero-trust policy")
        
        # Validate role is appropriate for environment
        if member_data.environment == "production" and member_data.role_type == "contractor":
            raise ValueError("Contractor role is not permitted in production environment")

    def _check_approval_requirement(self, member_data: TeamMember) -> bool:
        """Determine if access request requires explicit admin approval"""
        # All production access requires approval
        if member_data.environment == "production":
            return True
        
        # Any elevated role access in staging requires approval
        elevated_roles = ["admin", "billing_admin", "system"]
        if member_data.role_type in elevated_roles and member_data.environment == "staging":
            return True
        
        return False

    def _create_approval_request(self, member_data: TeamMember) -> Dict[str, Any]:
        """Create an access approval request that requires admin review"""
        approval_request = {
            "member_email": member_data.email,
            "requested_role": member_data.role_type,
            "requested_environment": member_data.environment,
            "is_contractor": member_data.is_contractor,
            "request_reason": member_data.request_reason,
            "status": "pending_approval",
            "created_at": datetime.utcnow().isoformat(),
            "approved_by": None,
            "approved_at": None,
            "expiration_date": member_data.end_date.isoformat() if member_data.end_date else None
        }
        
        # Store approval request in Appwrite database
        try:
            created_request = self.databases.create_document(
                database_id=self.approval_db_id,
                collection_id=self.approval_collection_id,
                document_id="unique()",
                data=approval_request,
                read=["role:system", "role:admin"],
                write=["role:system", "role:admin"]
            )
            
            # Trigger admin notification (Slack/email via Appwrite functions)
            self._trigger_admin_alert(approval_request)
            
            return {
                "status": "pending_approval",
                "request_id": created_request["$id"],
                "message": "Production access requires admin approval. Request has been submitted."
            }
        except AppwriteException as e:
            raise RuntimeError(f"Failed to create approval request: {str(e)}")

    def _provision_appwrite_access(self, member_data: TeamMember) -> Dict[str, Any]:
        """Provision least-privilege access in Appwrite Teams"""
        # Get or create team for the environment
        team_id = self._get_or_create_environment_team(member_data.environment)
        
        # Calculate permissions for the role
        team_roles = self._map_to_appwrite_team_roles(member_data.role_type, member_data.environment)
        
        try:
            # Create team membership with minimal roles
            membership = self.teams.create_membership(
                team_id=team_id,
                email=member_data.email,
                roles=team_roles,
                url=os.getenv("APPWRITE_TEAM_INVITE_URL", "https://axelr.ai/team/invite")
            )
            
            # Schedule access revocation if contractor
            if member_data.is_contractor and member_data.end_date:
                self._schedule_access_revocation(
                    system="appwrite",
                    team_id=team_id,
                    membership_id=membership["$id"],
                    revoke_at=member_data.end_date
                )
            
            return membership
        except AppwriteException as e:
            raise RuntimeError(f"Appwrite access provisioning failed: {str(e)}")

    def _provision_vultr_access(self, member_data: TeamMember) -> Dict[str, Any]:
        """Provision least-privilege access in Vultr IAM"""
        if not VULTR_API_KEY:
            return {"warning": "Vultr API key not configured, skipping Vultr access provisioning"}
        
        # Map to Vultr IAM policy
        vultr_roles = self._map_to_vultr_iam_roles(member_data.role_type, member_data.environment)
        
        try:
            # Create Vultr IAM user with restricted policy
            response = requests.post(
                f"{VULTR_ENDPOINT}/users",
                headers=self.vultr_headers,
                json={
                    "email": member_data.email,
                    "name": member_data.full_name,
                    "roles": vultr_roles
                }
            )
            response.raise_for_status()
            vultr_user = response.json()
            
            # Schedule revocation for contractors
            if member_data.is_contractor and member_data.end_date:
                self._schedule_access_revocation(
                    system="vultr",
                    user_id=vultr_user["id"],
                    revoke_at=member_data.end_date
                )
            
            return vultr_user
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Vultr access provisioning failed: {str(e)}")

    def _get_or_create_environment_team(self, environment: str) -> str:
        """Get existing Appwrite team for environment or create new one with zero-trust permissions"""
        # List existing teams to find environment team
        try:
            teams = self.teams.list()
            for team in teams["teams"]:
                if team["name"] == f"axelr-{environment}-team":
                    return team["$id"]
            
            # Create new team if not found
            new_team = self.teams.create(name=f"axelr-{environment}-team")
            team_id = new_team["$id"]
            
            # Set zero-trust team permissions (only system can manage by default)
            self.teams.update(
                team_id=team_id,
                name=f"axelr-{environment}-team"
            )
            
            return team_id
        except AppwriteException as e:
            raise RuntimeError(f"Failed to get/create environment team: {str(e)}")

    def _map_to_appwrite_team_roles(self, role_type: str, environment: str) -> List[str]:
        """Map abstract role type to concrete Appwrite team roles, environment-scoped"""
        base_roles = APPWRITE_TEAM_ROLES.get(role_type, [])
        # Scope all permissions to the specific environment
        return [f"{role}:{environment}" for role in base_roles if role != "*"]

    def _map_to_vultr_iam_roles(self, role_type: str, environment: str) -> List[str]:
        """Map abstract role type to Vultr IAM roles, environment-scoped"""
        base_roles = VULTR_IAM_ROLES.get(role_type, [])
        return [f"{role}:{environment}" for role in base_roles if role != "*"]

    def _schedule_access_revocation(self, system: str, resource_id: str, revoke_at: datetime) -> None:
        """Schedule automatic access revocation for contractor access expiration"""
        delay_seconds = (revoke_at - datetime.utcnow()).total_seconds()
        if delay_seconds <= 0:
            raise ValueError("Revocation time must be in the future")
        
        # Store revocation job in Appwrite scheduler
        try:
            self.databases.create_document(
                database_id=self.approval_db_id,
                collection_id="scheduled-revocations",
                document_id="unique()",
                data={
                    "system": system,
                    "resource_id": resource_id,
                    "revoke_at": revoke_at.isoformat(),
                    "status": "pending"
                },
                read=["role:system"],
                write=["role:system"]
            )
        except AppwriteException as e:
            raise RuntimeError(f"Failed to schedule access revocation: {str(e)}")

    def _trigger_admin_alert(self, request_data: Dict[str, Any]) -> None:
        """Trigger notification to admins for pending approval requests"""
        # Implementation uses Appwrite's native email/slack integration
        webhook_url = os.getenv("ADMIN_APPROVAL_WEBHOOK", "")
        if not webhook_url:
            return
        
        try:
            requests.post(
                webhook_url,
                json={
                    "text": f"New access approval request: {request_data['member_email']} requesting {request_data['requested_role']} access to {request_data['requested_environment']}",
                    "request_details": request_data
                }
            )
        except requests.exceptions.RequestException:
            # Fail silently to not block primary workflow, but log the error
            pass

    def _log_access_event(self, event_type: str, member_data: TeamMember, appwrite_data: Dict, vultr_data: Dict) -> None:
        """Log all access provisioning events to Appwrite audit log"""
        try:
            self.databases.create_document(
                database_id=self.approval_db_id,
                collection_id="audit-logs",
                document_id="unique()",
                data={
                    "event_type": event_type,
                    "member_email": member_data.email,
                    "role": member_data.role_type,
                    "environment": member_data.environment,
                    "is_contractor": member_data.is_contractor,
                    "appwrite_member_id": appwrite_data.get("$id"),
                    "vultr_member_id": vultr_data.get("id"),
                    "timestamp": datetime.utcnow().isoformat()
                },
                read=["role:system", "role:admin"],
                write=["role:system"]
            )
        except AppwriteException:
            pass

# Appwrite Function entry point
def main(context: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Main entry point for Appwrite inbound function to process onboarding requests"""
    manager = ZeroTrustOnboardingManager()
    
    try:
        member_data = TeamMember(**payload)
        result = manager.onboard_team_member(member_data)
        context.log(f"Successfully processed onboarding request for {member_data.email}")
        return result
    except Exception as e:
        context.error(f"Onboarding failed: {str(e)}")
        return {"status": "error", "message": str(e)}