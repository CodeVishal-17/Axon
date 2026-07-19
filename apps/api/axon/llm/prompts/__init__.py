"""Versioned prompt definitions — one module per LLM job.

Prompts are core IP (architecture §1): they live in git, get code review,
and are the single source of truth their services import. No prompt text
may be defined inline in a service.
"""
