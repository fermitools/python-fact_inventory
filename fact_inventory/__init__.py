"""Compatibility re-exports of the ASGI application.

Warning
-------
Litestar is an ASGI framework. Both objects below are ASGI applications, not
WSGI applications. They must be served by an ASGI server (uvicorn, gunicorn
with a uvicorn worker, hypercorn, etc.) or wrapped with an ASGI-to-WSGI bridge
before use with a WSGI server.

Two entry points are exposed:

``app_factory``
    An alias for ``fact_inventory.server.app.create_app``. This is the
    recommended entry point for production: it defers configuration loading
    until the ASGI server actually calls it, so importing this module does
    not require ``DEPLOYMENT``/``DATABASE_URI`` to already be set. Use it
    with an ASGI server's factory mode, e.g.::

        uvicorn fact_inventory:app_factory --factory
        gunicorn fact_inventory:app_factory --factory -k uvicorn_worker.UvicornWorker

``app``
    An eagerly-constructed ``Litestar`` instance, built by calling
    ``create_app()`` at import time. Importing this module therefore
    requires configuration (``DEPLOYMENT`` and ``DATABASE_URI`` environment
    variables) to already be set, and will raise if it is not. Prefer
    ``app_factory`` unless you specifically need an already-built instance
    (e.g. some embedding scenarios or the ``python -m fact_inventory`` dev
    server).
"""

from fact_inventory.server.app import create_app

app_factory = create_app
app = create_app()

__all__ = ["app", "app_factory"]
