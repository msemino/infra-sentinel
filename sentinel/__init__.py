"""Sentinel — an LLM-in-the-loop infrastructure watchdog.

Sentinel polls Zabbix and Prometheus for infrastructure problems and, only when
there is genuinely new information, asks a self-hosted LLM to reason about impact
and draft a concise operator alert. It then notifies via a pluggable channel
(Telegram, or a file-based inbox for demos).

The interesting parts are the guardrails: baseline seeding, deduplication with a
cooldown window, a mass-outage circuit breaker, and no-fallback retry semantics.
See :mod:`sentinel.watchdog` for the core diff/guardrail logic.
"""

__version__ = "1.0.0"
