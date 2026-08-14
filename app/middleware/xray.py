"""X-Ray tracing middleware (CLAUDE.md Section 14 Phase 14: "X-Ray tracing
on all FastAPI routes and DynamoDB calls").

aws-xray-sdk ships middleware for Flask/Django/Bottle but not Starlette/
FastAPI (checked: aws_xray_sdk.ext has no starlette/fastapi submodule as of
2.15.0) — this is the equivalent hand-written as a Starlette
BaseHTTPMiddleware: one segment per request, closed in a `finally` so a
route that raises still gets a (marked-faulted) segment instead of leaking
context into the next request.

DynamoDB/S3/etc. tracing needs no per-call code at all: `patch(["boto3"])`
(app/main.py's lifespan) instruments every boto3 client call as a
subsegment of whatever segment is open when the call happens — which, for
any call made during request handling, is the segment this middleware
opened.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aws_xray_sdk.core import xray_recorder
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp


class XRayMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, service_name: str) -> None:
        super().__init__(app)
        self._service_name = service_name

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        segment = xray_recorder.begin_segment(name=self._service_name)
        segment.put_http_meta("url", str(request.url))
        segment.put_http_meta("method", request.method)
        try:
            response = await call_next(request)
        except Exception as exc:
            segment.add_exception(exc, [])
            raise
        else:
            segment.put_http_meta("status", response.status_code)
            return response
        finally:
            xray_recorder.end_segment()
