"""Agents (Stage 4).

`RetrievalAgent` is DB-read only; `ReportGeneratorAgent` consumes its output
only. Neither imports from ``sources/`` or ``enrichment/``. The report depends on the
retrieval agent's return type, not on the DB.
"""
