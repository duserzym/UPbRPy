"""Optional integrations with lab services (``pip install 'UPbRPy[database]'``).

Nothing in :mod:`UPbRPy` imports this package; the reduction engine never
needs a network. :mod:`UPbRPy.integration.laicpmsdb` imports ``requests``
lazily so this package itself imports without the extra.
"""
