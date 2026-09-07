"""The JSON API the SPA talks to: everything under `/api/v1`, everything bearer-authed (§2).

Nothing is imported here on purpose - `app/main.py` mounts `app.api.router` explicitly,
so importing this package can never drag the whole API surface into a process that does
not serve it (the worker imports `app.services` and no endpoint at all).

Two house rules hold for every module in this package:

* the request identity comes only from `security/principal.py::get_principal` - never
  from the query string, the body or a header (§4.1);
* responses carry machine codes, never translated sentences (§8: the SPA translates).
"""
