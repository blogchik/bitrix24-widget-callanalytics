"""Application services: the transactional building blocks the handlers and the worker share.

Kept free of import-time side effects (build rule 5): importing this package must not open
sockets, read the database or touch the network, so `rest_log` can be imported from
`bitrix/client.py` (which every other layer imports in turn) without creating a cycle.
"""
