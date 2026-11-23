"""Local HTTP and JSON Streamable HTTP requests shared by listener tests."""
import http.client
import json
import socket


PROTOCOL_VERSION = "2025-06-18"
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
    "clientInfo": {"name": "http-fixture", "version": "1"},
}}


def ports():
    sockets = [socket.socket() for _ in range(3)]
    try:
        for server in sockets:
            server.bind(("127.0.0.1", 0))
        return [server.getsockname()[1] for server in sockets]
    finally:
        for server in sockets:
            server.close()


def request(port, method, path, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        connection.request(method, path, body=data, headers=headers or {})
        response = connection.getresponse()
        content = response.read()
        if response.getheader("Content-Type", "").startswith("application/json"):
            content = json.loads(content)
        return response.status, response.headers, content
    finally:
        connection.close()


def mcp_request(port, headers, message=None, session=None):
    headers = {"Accept": "application/json, text/event-stream", **headers}
    if session:
        headers.update({"Mcp-Session-Id": session, "MCP-Protocol-Version": PROTOCOL_VERSION})
    if message is None:
        return request(port, "DELETE", "/mcp", headers)
    return request(port, "POST", "/mcp", headers | {"Content-Type": "application/json"}, message)


def mcp_probe(port, headers):
    """Exercise the public authentication policy without leaving an allocated session."""
    response = mcp_request(port, headers, INITIALIZE)
    if response[0] == 200:
        session = response[1]["Mcp-Session-Id"]
        assert session and response[2]["result"]["protocolVersion"] == PROTOCOL_VERSION
        cleanup = mcp_request(port, headers, session=session)
        assert cleanup[0] == 204
    return response


def start_session(port, headers):
    status, response_headers, body = mcp_request(port, headers, INITIALIZE)
    session = response_headers["Mcp-Session-Id"]
    assert status == 200 and session and body["result"]["protocolVersion"] == PROTOCOL_VERSION
    status, _, _ = mcp_request(port, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    assert status == 202
    return session


def call_tool(port, headers, session, request_id, name, arguments):
    status, _, body = mcp_request(port, headers, {
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, session)
    assert status == 200 and body["id"] == request_id
    return body["result"]
