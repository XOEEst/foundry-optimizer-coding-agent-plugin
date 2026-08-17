from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx


class AzureTransportRecorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Any] = {}
        self.history: list[httpx.Request] = []

    def add(self, method: str, url: str, handler: Any) -> None:
        self.routes[(method.upper(), url)] = handler

    def add_sequence(self, method: str, url: str, handlers: list[Any]) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if not handlers:
                return httpx.Response(500, json={"error": {"code": "exhausted"}}, request=request)
            handler = handlers.pop(0)
            if callable(handler):
                return handler(request)
            status_code, payload = handler
            return httpx.Response(status_code, json=payload, request=request)
        self.routes[(method.upper(), url)] = handle

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            self.history.append(request)
            key = (request.method.upper(), str(request.url))
            if key not in self.routes:
                return httpx.Response(404, json={"error": {"code": "not_found"}}, request=request)
            handler = self.routes[key]
            if callable(handler):
                return handler(request)
            status_code, payload = handler
            return httpx.Response(status_code, json=payload, request=request)
        return httpx.MockTransport(handle)


def json_body(request: httpx.Request) -> Mapping[str, object]:
    raw = request.read()
    return json.loads(raw.decode("utf-8")) if raw else {}
