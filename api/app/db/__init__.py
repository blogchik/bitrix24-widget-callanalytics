"""Database layer: engine, transaction scopes and the §3 schema mirror.

Nothing is re-exported here — importing `app.db` must not build the engine as a
side effect, so callers import `app.db.engine` / `app.db.session` explicitly
(docs/architecture.md §2).
"""
