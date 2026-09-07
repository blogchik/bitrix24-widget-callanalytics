"""Security primitives: token encryption at rest, log redaction, session tokens.

Kept import-side-effect free on purpose (beyond building `settings`) so tests can import
a single primitive without pulling in the database or HTTP stack.
"""
