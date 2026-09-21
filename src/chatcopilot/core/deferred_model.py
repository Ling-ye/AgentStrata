"""A model handle materializes its transport only when a consumer requests inference."""

from __future__ import annotations

import copy
import threading


class DeferredModelClient:
    def __init__(self, config, factory):
        self._config = copy.copy(config)
        self._factory = factory
        self._client = None
        self._lock = threading.Lock()
        self._closed = False

    @property
    def model(self):
        return self._config.model

    @property
    def config(self):
        return copy.copy(self._config)

    def _materialize(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("model handle is closed")
            if self._client is None:
                self._client = self._factory(self._config)
            return self._client

    def chat(self, *args, **kwargs):
        return self._materialize().chat(*args, **kwargs)

    def complete(self, request):
        return self._materialize().complete(request)

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._client is not None:
                    self._client.close()
