"""Axelr Vultr Integration — v1.2.0

Compute provisioning with blueprints (7), cost guards, lifecycle,
orphan detection, snapshots, SSH keys, firewall, metrics, quotas,
circuit breaker, idempotency, and request coalescing.
"""
__version__ = "1.2.0"
from .client import VultrClient, get_client


def health():
    return {"ok": True, "details": {"version": "1.2.0", "note": "Vultr Cloud provisioning operational"}}

def init(**kwargs):
    pass

def shutdown():
    pass
