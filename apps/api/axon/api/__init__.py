"""HTTP/WS routers. Routers stay thin: parse/validate, call a service, shape
the response. Domain logic lives in ``axon.services`` so the worker can reuse
it without HTTP."""
