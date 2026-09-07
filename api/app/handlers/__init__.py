"""Bitrix24-facing endpoints: form POST in, rendered HTML out (§2, §4.3-§4.5, §4.9).

Nothing is imported here on purpose. `app/main.py` mounts each router explicitly, so
importing this package can never drag the whole handler surface (and its database and
httpx dependencies) into a process that only needs one of them - the worker imports
`app.services` but no handler at all.
"""
