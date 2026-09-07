"""The sync worker's building blocks (§5).

WHY the package exists at all: every module under it is one *decision* of §5 made in
exactly one place - how far a cursor may advance (`fetch`), how fast a portal may be
asked (`throttle`), how rows and the cursor become one durable step (`upsert`), who is
allowed to write at all (`lease`). The job layer composes them; it never re-decides
them, because the failure modes here are silent - a cursor that walks over data nobody
read, or a retry loop against an API shared by every tenant.

Kept free of import-time side effects (build rule 5): importing this package must not
open sockets, read the database or touch the network, so a test can import a single
decision function and drive it with a fake clock.
"""
