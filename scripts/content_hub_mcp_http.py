"""Stateless Streamable HTTP transport for the Content Hub MCP tools."""
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from scripts.content_hub_mcp import handle


ALLOWED_HOSTS = {"192.168.1.202", "localhost", "127.0.0.1"}

app = FastAPI(title="Newgarden Content Hub MCP", docs_url=None, redoc_url=None)


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}


@app.api_route("/mcp", methods=["GET", "DELETE"], include_in_schema=False)
def unsupported_transport_method():
    return Response(status_code=405, headers={"Allow": "POST"})


@app.post("/mcp", include_in_schema=False)
async def mcp(request: Request):
    host = request.url.hostname
    if host not in ALLOWED_HOSTS:
        return JSONResponse({"error": "Invalid Host header"}, status_code=400)

    try:
        message = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error"},
        }, status_code=400)

    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": message.get("id") if isinstance(message, dict) else None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }, status_code=400)

    result = await run_in_threadpool(handle, message)
    if "id" not in message:
        return Response(status_code=202)
    if result is None:
        return Response(status_code=202)
    if "error" in result:
        payload = {"jsonrpc": "2.0", "id": message["id"], **result}
    else:
        payload = {"jsonrpc": "2.0", "id": message["id"], "result": result}
    return JSONResponse(payload)
