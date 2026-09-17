"""Shared, config-swappable LLM client (§4).

A thin abstraction the report generator renders prose through. Kept separate
from the storage/agent logic so the model backend can change by config.
"""
