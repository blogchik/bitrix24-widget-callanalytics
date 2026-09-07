"""Bitrix24 protocol layer.

Kept free of import-time side effects (build rule 5): importing this package must not
open sockets, read the database or touch the network, so `errors` and `forms` can be
used by the handlers, the worker and the tests alike.
"""
