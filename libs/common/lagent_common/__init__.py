"""Shared *infrastructure* for listing-agent services: event bus, correlation IDs,
internal auth, DB helpers, migrations, service factory.

Rule: no business logic here. Domain models and rules live inside each service.
"""
