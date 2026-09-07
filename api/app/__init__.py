"""Call Analytics API package (`texnobus.callanalytics`).

Kept import-side-effect free on purpose: §5.9 dispatches jobs into the same
process as the API image, so importing `app` must never open sockets or
schedule work (docs/architecture.md §2, §5.9).
"""
