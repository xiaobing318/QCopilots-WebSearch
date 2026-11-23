#!/usr/bin/env python3
"""Exercise HTTP and stdio through the official, pinned MCP Python SDK."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import AsyncExitStack, suppress
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import secrets
import select
import socket
import tempfile
import threading
import time
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx2
import psutil
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from testkit import (
    ROOT, Fault, admin_smoke, digest, environment, fingerprint, host_info, start_process, stop_process,
)
from process_cleanup import wait_owned

EXPECTED = {"web_search", "web_open", "web_find", "web_search_status"}
# Handshake-era revisions the server negotiates; it echoes either one.
SUPPORTED_VERSIONS = {"2025-11-25", "2025-06-18"}
CATALOG = json.loads((ROOT / "contracts/v1/tools.json").read_text("utf-8"))


def require(condition, code):
    if not condition:
        raise Fault(code)


class Fixture(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.entered = threading.Event()
        self.disconnected = threading.Event()
        self.release = threading.Event()

    def arm(self):
        self.entered.clear()
        self.disconnected.clear()
        self.release.clear()

    def handle_error(self, request, client_address):
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            self.connection.settimeout(2)
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/search" or not 0 < length <= 65536:
                self.send_error(400)
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                return
            query = parse_qs(raw.decode("utf-8")).get("q", [""])[0]
            if query.startswith("mcp-cancel-"):
                self.server.entered.set()
                deadline = time.monotonic() + 10
                while not self.server.release.wait(0.025):
                    if select.select([self.connection], [], [], 0)[0]:
                        try:
                            disconnected = self.connection.recv(1, socket.MSG_PEEK) == b""
                        except ConnectionResetError:
                            disconnected = True
                        if disconnected:
                            self.server.disconnected.set()
                            return
                    if time.monotonic() >= deadline:
                        return
            else:
                time.sleep(0.1)
            body = json.dumps({"unresponsive_engines": [], "results": [
                {"url": f"https://example.org/fixture/{i}", "title": f"Fixture {i}",
                 "content": "Synthetic MCP interoperability content.", "engines": ["fixture"]}
                for i in range(20)]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (OSError, ValueError):
            pass


def payload(result, error=None):
    require(bool(result.is_error) == (error is not None), "tool_error_flag")
    data = result.structured_content
    require(isinstance(data, dict), "structured_content_missing")
    require(len(result.content) == 1 and result.content[0].type == "text", "text_content_missing")
    require(json.loads(result.content[0].text) == data, "content_disagreement")
    if error is None:
        require(type(data.get("contract_version")) is int and data["contract_version"] == 1,
                "contract_version")
    else:
        require(data.get("error", {}).get("code") == error, "business_error_code")
    return data


async def wait_until(predicate, timeout, code):
    deadline = time.monotonic() + timeout
    while not predicate():
        require(time.monotonic() < deadline, code)
        await asyncio.sleep(0.025)


async def rpc_error(client, name, args):
    try:
        await client.call_tool(name, args)
    except MCPError as exc:
        require(exc.code == -32602, "jsonrpc_error_code")
    else:
        raise Fault("jsonrpc_error_missing")


async def cancel_in_flight(client, fixture, label):
    fixture.arm()
    task = asyncio.create_task(client.call_tool("web_search",
        {"query": f"mcp-cancel-{label}-{secrets.token_hex(8)}"}, read_timeout_seconds=20))
    try:
        await wait_until(fixture.entered.is_set, 5, "cancel_request_not_in_flight")
        require(not task.done(), "cancel_request_already_finished")
        status = payload(await client.call_tool("web_search_status", {}))
        require(status["active_calls"] >= 1 and status["active_upstream"] >= 1, "cancel_request_not_active")
        started = time.monotonic()
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2)
        except asyncio.CancelledError:
            pass
        else:
            raise Fault("sdk_did_not_cancel")
        deadline = time.monotonic() + 5
        while True:
            status = payload(await client.call_tool("web_search_status", {}))
            if all(status[key] == 0 for key in ("active_calls", "active_upstream", "search_waiters")):
                break
            require(time.monotonic() < deadline, "server_capacity_not_released")
            await asyncio.sleep(0.025)
        await wait_until(fixture.disconnected.is_set, 5, "upstream_not_disconnected")
        await client.session.send_ping()
        recovered = payload(await client.call_tool("web_search",
            {"query": f"mcp-recovered-{label}", "limit": 10}))
        require(len(recovered["results"]) == 10, "search_after_cancel_failed")
        return round(time.monotonic() - started, 3)
    finally:
        fixture.release.set()
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def exercise(client, fixture, label, public_url):
    require(client.protocol_version in SUPPORTED_VERSIONS, "protocol_negotiation")
    await client.session.send_ping()
    listing = await client.list_tools(cache_mode="bypass")
    require({tool.name for tool in listing.tools} == EXPECTED, "tool_inventory")
    require([tool.name for tool in listing.tools] == [entry["name"] for entry in CATALOG["tools"]],
            "tool_order")
    require(listing.next_cursor is None, "unexpected_tool_pagination")
    for tool, entry in zip(listing.tools, CATALOG["tools"]):
        expected = json.loads((ROOT / "contracts/v1" / f"{tool.name}.schema.json").read_text("utf-8"))
        require(tool.input_schema == expected, "tool_schema_disagreement")
        require(tool.title == entry["title"] and tool.description == entry["description"],
                "tool_metadata_disagreement")
        annotations = tool.annotations.model_dump(by_alias=True, exclude_none=True) if tool.annotations else {}
        require(annotations == entry["annotations"], "tool_annotation_disagreement")
    status = payload(await client.call_tool("web_search_status", {}))
    require(status["service"] == "ready", "service_not_ready")
    query = f"mcp-search-{label}-{secrets.token_hex(8)}"
    args = {"query": query, "limit": 10, "language": "zh-CN"}
    result = payload(await client.call_tool("web_search", args))
    require(result["status"] == "ok" and len(result["results"]) == 10, "search_results")
    require(result["provider"] == "searxng", "unexpected_search_provider")
    require(result["cache"]["hit"] is False, "cold_cache")
    require(payload(await client.call_tool("web_search", args))["cache"]["hit"] is True, "warm_cache")
    payload(await client.call_tool("web_open",
        {"url": f"http://127.0.0.1:{fixture.server_port}/private"}), error="destination_blocked")
    payload(await client.call_tool("web_find",
        {"page_id": secrets.token_hex(16), "pattern": "fixture"}), error="page_unavailable")
    # Unknown tools stay protocol errors. Argument errors are tool results
    # naming the failing field, so a model can correct its call.
    await rpc_error(client, "unknown_tool", {})
    invalid = payload(await client.call_tool("web_search", {}), error="invalid_argument")
    require("'query'" in invalid["error"]["message"], "validation_detail_missing")
    payload(await client.call_tool("web_open", {"url": "https://example.org", "page_id": "x"}),
            error="invalid_argument")
    cancellation = await cancel_in_flight(client, fixture, label)
    public = {"attempted": public_url is not None, "passed": False}
    if public_url is not None:
        try:
            fetched = await client.call_tool("web_open", {"url": public_url}, read_timeout_seconds=30)
            if fetched.is_error:
                error = (fetched.structured_content or {}).get("error", {}).get("code")
                public_codes = {"invalid_argument", "destination_blocked", "unsupported_content",
                                "upstream_http_error", "timeout", "response_too_large"}
                raise Fault(error if error in public_codes else "public_open_failed")
            opened = payload(fetched)
            first = next(line for line in opened["lines"] if line["text"].strip())
            found = payload(await client.call_tool("web_find",
                {"page_id": opened["page_id"], "pattern": first["text"].strip()[:64]}))
            require(any(hit["line"] == first["line"] for hit in found["matches"]), "public_find_mismatch")
            next_line = min(first["line"] + 1, opened["total_lines"])
            reopened = payload(await client.call_tool("web_open",
                {"page_id": opened["page_id"], "start_line": next_line, "max_lines": 1}))
            require(reopened["page_id"] == opened["page_id"] and reopened["cache"]["hit"] is True
                    and len(reopened["lines"]) == 1 and reopened["lines"][0]["line"] == next_line,
                    "public_page_continuation")
            public["passed"] = True
        except Exception as exc:
            public["error"] = fault_code(exc)
    await client.session.send_ping()
    return {"transport": label, "protocol": client.protocol_version, "tools": sorted(EXPECTED),
            "passed": public_url is None or public["passed"], "cancellation_seconds": cancellation, "public": public}


async def exercise_auto(client, label):
    # The SDK default mode probes for a 2026-era server first. This server is
    # handshake-based, so the probe must fall back to initialize.
    require(client.protocol_version in SUPPORTED_VERSIONS, f"{label}_auto_fallback")
    await client.session.send_ping()
    listing = await client.list_tools(cache_mode="bypass")
    require({tool.name for tool in listing.tools} == EXPECTED, f"{label}_auto_tool_inventory")
    status = payload(await client.call_tool("web_search_status", {}))
    require(status["service"] == "ready", f"{label}_auto_status")
    return client.protocol_version


def stdio_children(config):
    found = []
    for child in psutil.Process().children(recursive=True):
        try:
            command = child.cmdline()
            if "--Stdio" in command and str(config) in command:
                found.append(child)
        except psutil.Error:
            pass
    return found


async def exercise_http(port, token, fixture, public_url):
    methods, deletes = [], []

    async def observe_request(request):
        if request.method == "POST":
            methods.append(json.loads(await request.aread()).get("method"))

    async def observe_response(response):
        if response.request.method == "DELETE":
            deletes.append(response.status_code)

    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False,
        follow_redirects=False, timeout=30,
        event_hooks={"request": [observe_request], "response": [observe_response]}) as http:
        transport = streamable_http_client(f"http://127.0.0.1:{port}/mcp",
                                           http_client=http, terminate_on_close=True)
        async with Client(transport, mode="legacy", read_timeout_seconds=10) as client:
            result = await exercise(client, fixture, "http", public_url)
        require(methods.count("initialize") == 1, "initialize_count")
        require(methods.count("notifications/initialized") == 1, "initialized_missing")
        require("notifications/cancelled" in methods, "cancel_notification_missing")
        require(deletes == [204], "session_delete_failed")
    result["http_session_deleted"] = True
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False,
        follow_redirects=False, timeout=30) as http:
        transport = streamable_http_client(f"http://127.0.0.1:{port}/mcp",
                                           http_client=http, terminate_on_close=True)
        async with Client(transport, read_timeout_seconds=10) as client:
            result["auto_protocol"] = await exercise_auto(client, "http")
    return result


async def exercise_stdio(executable, config, tokens, fixture, public_url):
    owned = []
    params = StdioServerParameters(command=str(executable),
        args=["--ConfigPath", str(config), "--Stdio"], env=environment(tokens))
    try:
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            transport = stdio_client(params, errlog=errlog)
            async with Client(transport, mode="legacy", read_timeout_seconds=10) as client:
                owned.extend(stdio_children(config))
                require(len(owned) == 1, "stdio_process_not_identified")
                result = await exercise(client, fixture, "stdio", public_url)
            async with Client(stdio_client(params, errlog=errlog), read_timeout_seconds=10) as client:
                fresh = [child for child in stdio_children(config) if child not in owned]
                require(len(fresh) == 1, "stdio_auto_process_not_identified")
                owned.extend(fresh)
                result["auto_protocol"] = await exercise_auto(client, "stdio")
        require(not any(child.is_running() for child in owned), "stdio_sdk_left_process")
        result["stdio_cleanup"] = "sdk_managed_no_remaining_process"
        return result
    finally:
        for child in owned:
            with suppress(psutil.NoSuchProcess):
                if child.is_running():
                    child.kill()
        require(not wait_owned(owned, timeout=5), "stdio_child_cleanup_failed")


async def exercise_live_provider(client, label):
    status = payload(await client.call_tool("web_search_status", {}))
    require(status["settings"]["provider"] == "auto", f"live_{label}_auto_settings")
    result = await client.call_tool(
        "web_search", {"query": "QGIS official documentation", "limit": 5, "language": "en"},
        read_timeout_seconds=30,
    )
    if result.is_error:
        data = result.structured_content
        error = data.get("error", {}).get("code") if isinstance(data, dict) else None
        known_errors = {"provider_unavailable", "rate_limited", "timeout", "upstream_http_error"}
        raise Fault(f"live_{label}_{error}" if error in known_errors else f"live_{label}_search_failed")
    data = payload(result)
    require(data["results"], f"live_{label}_empty_results")
    source = data.get("provider")
    require(isinstance(source, str) and (source == "bing" or source.startswith("duckduckgo")),
            f"live_{label}_unexpected_provider")
    return {"transport": label, "provider": source, "result_count": len(data["results"])}


async def exercise_fixed_page(reader, other, fixture, label):
    search = payload(await reader.call_tool("web_search", {"query": "fixture page", "limit": 1}))
    require(len(search["results"]) == 1, "fixture_search_result")
    url = search["results"][0]["url"]
    opened = payload(await reader.call_tool("web_open", {"url": url, "max_lines": 1}))
    page_id = opened["page_id"]
    require(opened["lines"] == [{"line": 1, "text": "First fixture line"}]
            and opened["next_start_line"] == 2 and opened["cache"]["hit"] is False,
            "fixture_first_page")
    found = payload(await reader.call_tool("web_find", {"page_id": page_id, "pattern": "needLE"}))
    require(found["matches"] == [{"line": 2, "text": "Needle on the second line"}], "fixture_find")
    rest = payload(await reader.call_tool("web_open", {"page_id": page_id, "start_line": opened["next_start_line"]}))
    require(rest["lines"] == [{"line": 2, "text": "Needle on the second line"},
                              {"line": 3, "text": "Last fixture line"}]
            and rest["next_start_line"] is None and rest["cache"]["hit"] is True, "fixture_continuation")
    payload(await other.call_tool("web_open", {"page_id": page_id}), error="page_unavailable")
    payload(await other.call_tool("web_find", {"page_id": page_id, "pattern": "Needle"}), error="page_unavailable")
    elapsed = await cancel_in_flight(reader, fixture, "page-" + label)
    return {"transport": label, "source": "injected_page", "passed": True, "cancellation_seconds": elapsed}


async def run_page_fixture(executable, template, folder, fixture, tokens):
    folder.mkdir()
    proc, ports, _ = start_process(executable, template, folder, fixture, tokens, 300)
    results = []
    try:
        for label in ("http", "stdio"):
            async with AsyncExitStack() as stack:
                clients = []
                for token in tokens[:2]:
                    if label == "http":
                        http = await stack.enter_async_context(httpx2.AsyncClient(
                            headers={"Authorization": "Bearer " + token}, trust_env=False, timeout=30))
                        transport = streamable_http_client(f"http://127.0.0.1:{ports[0]}/mcp",
                                                           http_client=http, terminate_on_close=True)
                    else:
                        params = StdioServerParameters(command=str(executable),
                            args=["--ConfigPath", str(folder / "runtime.json"), "--Stdio"], env=environment(tokens))
                        errlog = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
                        transport = stdio_client(params, errlog=errlog)
                    clients.append(await stack.enter_async_context(
                        Client(transport, mode="legacy", read_timeout_seconds=10)))
                results.append(await exercise_fixed_page(*clients, fixture, label))
    finally:
        fixture.release.set()
        stopped = stop_process(proc)
        remaining = stdio_children(folder / "runtime.json")
        for child in remaining:
            with suppress(psutil.NoSuchProcess):
                child.kill()
        require(not wait_owned(remaining, timeout=5), "fixture_stdio_stop_failed")
    require(not remaining, "fixture_stdio_cleanup")
    require(stopped["passed"], "fixture_stop_failed")
    return results


async def run_live(args, template, folder, tokens, report):
    proc, ports, _ = start_process(args.Executable, template, folder, None, tokens, 300)
    config = folder / "runtime.json"
    results, owned = [], []
    report["live_provider"] = results
    try:
        try:
            async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + tokens[0]},
                                          trust_env=False, follow_redirects=False, timeout=35) as http:
                transport = streamable_http_client(f"http://127.0.0.1:{ports[0]}/mcp",
                                                   http_client=http, terminate_on_close=True)
                async with Client(transport, mode="legacy", read_timeout_seconds=30) as client:
                    results.append(await exercise_live_provider(client, "http"))
        except Exception as exc:
            results.append({"transport": "http", "error": fault_code(exc)})

        try:
            params = StdioServerParameters(command=str(args.Executable),
                args=["--ConfigPath", str(config), "--Stdio"], env=environment(tokens))
            with open(os.devnull, "w", encoding="utf-8") as errlog:
                async with Client(stdio_client(params, errlog=errlog),
                                  mode="legacy", read_timeout_seconds=30) as client:
                    owned.extend(stdio_children(config))
                    require(len(owned) == 1, "live_stdio_process_not_identified")
                    results.append(await exercise_live_provider(client, "stdio"))
            require(not any(child.is_running() for child in owned), "live_stdio_sdk_left_process")
        except Exception as exc:
            results.append({"transport": "stdio", "error": fault_code(exc)})
    finally:
        report["live_process_stop"] = stop_process(proc)
        for child in owned:
            with suppress(psutil.NoSuchProcess):
                if child.is_running():
                    child.kill()
        require(not wait_owned(owned, timeout=5), "live_stdio_cleanup_failed")
    require(report["live_process_stop"]["passed"], "live_process_stop_failed")
    failed = next((result["error"] for result in results if "error" in result), None)
    require(failed is None, failed)


async def run(args, report):
    template = json.loads((ROOT / "config/runtime.json").read_text("utf-8"))
    work = ROOT / "work"
    work.mkdir(exist_ok=True)
    fixture = Fixture()
    thread = threading.Thread(target=fixture.serve_forever)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="qcws-sdk-", dir=work) as temporary:
            folder = Path(temporary).resolve()
            require(folder.is_relative_to(work.resolve()), "temporary_directory_scope")
            tokens = [secrets.token_hex(32) for _ in range(6)]
            proc, ports, _ = start_process(args.Executable, template, folder, fixture, tokens, 300)
            try:
                report["admin"] = admin_smoke(args.Executable, folder / "runtime.json", environment(tokens), "searxng")
                report["transports"].append(await exercise_http(ports[0], tokens[0], fixture, args.PublicUrl))
                report["transports"].append(await exercise_stdio(
                    args.Executable, folder / "runtime.json", tokens, fixture, args.PublicUrl))
            finally:
                fixture.release.set()
                report["http_process_stop"] = stop_process(proc)
            if args.PageFixture:
                report["fixed_pages"] = await run_page_fixture(
                    args.PageFixture, template, folder / "injected", fixture, tokens)
            if args.LiveProvider == "auto":
                live_folder = folder / "live"
                live_folder.mkdir()
                await run_live(args, template, live_folder, tokens, report)
    finally:
        fixture.release.set()
        fixture.shutdown()
        fixture.server_close()
        thread.join(timeout=3)
        require(not thread.is_alive(), "fixture_not_stopped")


def fault_code(exc):
    # AnyIO wraps user assertions in ExceptionGroup during transport teardown.
    if isinstance(exc, Fault):
        return exc.args[0]
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            code = fault_code(child)
            if code != "interop_failed":
                return code
    return "interop_failed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--Executable", type=Path, required=True)
    parser.add_argument("--Output", type=Path, required=True)
    parser.add_argument("--PublicUrl", default=None)
    parser.add_argument("--PageFixture", type=Path, help="Test-only executable with deterministic page responses")
    parser.add_argument("--LiveProvider", choices=("auto",), default=None)
    args = parser.parse_args()
    args.Executable = args.Executable.resolve(strict=True)
    if args.PageFixture:
        args.PageFixture = args.PageFixture.resolve(strict=True)
    output = args.Output.resolve()
    require(output not in (args.Executable, args.PageFixture, Path(__file__).resolve()), "invalid_output")
    logging.disable(logging.CRITICAL)
    report = {"report_version": 1, "executable_sha256": digest(args.Executable),
              "runtime": fingerprint(args.Executable), "host": host_info(),
              "sdk_version": importlib.metadata.version("mcp"),
              "httpx2_version": importlib.metadata.version("httpx2"), "transports": [], "passed": False}
    if args.PageFixture:
        report["page_fixture_sha256"] = digest(args.PageFixture)
    code = 1
    try:
        require(report["sdk_version"] == "2.3.0", "unexpected_sdk_version")
        require(report["httpx2_version"] == "2.16.0", "unexpected_httpx2_version")
        asyncio.run(run(args, report))
        require(digest(args.Executable) == report["executable_sha256"], "binary_changed")
        require(fingerprint(args.Executable) == report["runtime"], "runtime_changed")
        if args.PageFixture:
            require(digest(args.PageFixture) == report["page_fixture_sha256"], "fixture_binary_changed")
        require(len(report["transports"]) == 2, "missing_transport")
        require(report["http_process_stop"]["passed"], "http_process_stop_failed")
        if args.PublicUrl is not None:
            for transport in report["transports"]:
                require(transport["public"]["passed"], f"{transport['transport']}_public_check_failed")
        report["passed"], code = True, 0
    except KeyboardInterrupt:
        report["error"], code = "interrupted", 130
    except Exception as exc:
        report["error"] = fault_code(exc)
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix=output.name + ".", delete=False) as target:
            json.dump(report, target, ensure_ascii=False, indent=2)
            pending = Path(target.name)
        try:
            os.replace(pending, output)
        finally:
            pending.unlink(missing_ok=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
