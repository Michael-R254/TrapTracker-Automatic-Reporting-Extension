"""Storage layer (Stage 2).

The thin `DetectionRepository` DAL is the ONLY module that knows the on-disk
storage shape (JSON-in-SQLite, WAL). Callers pass and receive Python-native
values — dataclasses, ``list[float]`` embeddings, dicts — never JSON or SQL
(plan §5.3, Fix b), so a later vector-store swap stays localised here.
"""
