import os
from locust import HttpUser, task, between

class APIUser(HttpUser):
    wait_time = between(1, 5)

    @task
    def create_guest_session(self):
        self.client.post("/api/guest/session")