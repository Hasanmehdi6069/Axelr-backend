import os
import json
import random
from locust import HttpUser, task, between, tag

# Test data for realistic load
TEST_PROMPTS = [
    "Analyze this sales data and create a summary",
    "Build a responsive navigation bar with dark mode",
    "Process this CSV file and extract key metrics",
    "Create a pricing card component for a SaaS product",
    "Debug this Python function that's throwing an error",
    "Generate a SQL query to analyze user engagement",
    "Build a todo app with React hooks",
    "Analyze customer feedback data for trends"
]

TEST_FILES = [
    {"filename": "sales_data.csv", "mimetype": "text/csv"},
    {"filename": "user_data.xlsx", "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    {"filename": "design_mockup.png", "mimetype": "image/png"},
    {"filename": "requirements.txt", "mimetype": "text/plain"}
]

class AxelrAPIUser(HttpUser):
    """
    Load test for AXELR AI backend - simulates real user behavior
    Target: Validate 20-30 concurrent users, 1000+ daily active users
    """
    wait_time = between(3, 15)  # More realistic think time (3-15s between requests)
    session_token = None
    user_id = None
    
    def on_start(self):
        """Initialize user session before any tasks"""
        # Create guest session
        response = self.client.post("/api/guest/session")
        if response.status_code == 200:
            data = response.json()
            self.session_token = data.get("token")
            self.user_id = data.get("userId")
    
    @tag("core", "health")
    @task(5)
    def health_check(self):
        """Basic health check - lightweight, runs frequently"""
        self.client.get("/api/health")
    
    @tag("streaming", "ai")
    @task(3)
    def stream_ai_extraction(self):
        """Test the main streaming extraction endpoint - core AI workload"""
        if not self.session_token:
            return
            
        prompt = random.choice(TEST_PROMPTS)
        test_file = random.choice(TEST_FILES)
        
        # Create multipart form data like real client
        files = [
            ("files", (test_file["filename"], b"sample,csv,data\n1,2,3\n4,5,6", test_file["mimetype"]))
        ]
        
        data = {
            "command": prompt,
            "workspace": "auto",  # Tests auto-detection
            "task_type": "auto"
        }
        
        headers = {"Authorization": f"Bearer {self.session_token}"}
        
        # Stream the response to properly simulate client behavior
        with self.client.post(
            "/api/extract_stream",
            files=files,
            data=data,
            headers=headers,
            stream=True,
            catch_response=True
        ) as response:
            if response.status_code != 200:
                response.failure(f"Streaming failed with status {response.status_code}")
                return
                
            # Consume the entire stream to measure full latency
            full_response = ""
            for line in response.iter_lines():
                if line:
                    full_response += line.decode('utf-8')
                    
            response.success()
    
    @tag("sync", "ai")
    @task(2)
    def sync_ai_extraction(self):
        """Test synchronous extraction endpoint"""
        if not self.session_token:
            return
            
        prompt = random.choice(TEST_PROMPTS)
        
        data = {
            "command": prompt,
            "workspace": "core",
            "task_type": "structuring"
        }
        
        headers = {"Authorization": f"Bearer {self.session_token}"}
        
        with self.client.post(
            "/api/extract",
            data=data,
            headers=headers,
            catch_response=True
        ) as response:
            if response.status_code != 200:
                response.failure(f"Sync extraction failed: {response.status_code}")
            else:
                response.success()
    
    @tag("session", "history")
    @task(1)
    def get_chat_history(self):
        """Test session history retrieval"""
        if not self.session_token:
            return
            
        headers = {"Authorization": f"Bearer {self.session_token}"}
        with self.client.get("/api/sessions", headers=headers, catch_response=True) as response:
            if response.status_code != 200:
                response.failure(f"Failed to get sessions: {response.status_code}")
            else:
                response.success()

"""
Locust execution guide for your target:
1. Start the test with: locust -f tests/locustfile_enhanced.py -H https://your-render-backend.onrender.com
2. In the Locust web UI (http://localhost:8089):
   - Number of users: 30 (max concurrent target)
   - Spawn rate: 2 users/second (gradual ramp-up)
   
This configuration will validate:
- 20-30 concurrent users sustained
- 1000+ daily active users equivalent (30 concurrent * 30s average session = 3600/day)
- Memory usage stays within Render's 512MB limit
- Latency remains acceptable under load
"""