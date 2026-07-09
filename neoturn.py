"""
NeoTurn — Lightweight Cloudflare Turnstile Solver

Uses nodriver (direct CDP to Chrome) for minimal overhead.
No Playwright, no third-party API, no browser automation framework bloat.

Modes:
  1. CLI:   python neoturn.py solve --url https://example.com --sitekey 0x4AAAA...
  2. API:   python neoturn.py serve --port 5000

Note: Cloudflare detects headless Chrome, so non-headless mode + Xvfb is used
on Linux servers. Set DISPLAY env or run under xvfb-run.
"""

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from urllib.parse import unquote
from typing import Optional, Union

try:
    import nodriver
except ImportError:
    print("nodriver not installed. Run: pip install nodriver")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[35m",
        "INFO": "\033[34m",
        "SUCCESS": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "RESET": "\033[0m",
    }

    def format(self, record):
        ts = time.strftime("%H:%M:%S")
        color = self.COLORS.get(record.levelname, "")
        reset = self.COLORS["RESET"]
        return f"[{ts}] [{color}{record.levelname:<7}{reset}] {record.getMessage()}"


logging.SUCCESS = 25
logging.addLevelName(logging.SUCCESS, "SUCCESS")

logger = logging.getLogger("NeoTurn")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(ColorFormatter())
logger.addHandler(_handler)


def log_success(msg):
    logger.log(logging.SUCCESS, msg)


# ---------------------------------------------------------------------------
# Xvfb auto-start helper (Linux headless servers)
# ---------------------------------------------------------------------------

_xvfb_proc = None

def ensure_display():
    """Ensure a DISPLAY is available. Auto-start Xvfb if needed (Linux)."""
    global _xvfb_proc
    if os.environ.get("DISPLAY"):
        return  # Already have a display

    if sys.platform.startswith("linux"):
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            logger.warning("No DISPLAY and Xvfb not found. Turnstile may not render.")
            logger.warning("Install: apt install xvfb  |  Or run: xvfb-run -a python neoturn.py ...")
            return

        display_num = 99
        _xvfb_proc = subprocess.Popen(
            [xvfb, f":{display_num}", "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = f":{display_num}"
        time.sleep(0.5)
        logger.info(f"Auto-started Xvfb on :{display_num}")


def cleanup_xvfb():
    global _xvfb_proc
    if _xvfb_proc:
        _xvfb_proc.terminate()
        _xvfb_proc = None


# ---------------------------------------------------------------------------
# Core solver
# ---------------------------------------------------------------------------

class NeoTurnSolver:
    """Solve Cloudflare Turnstile challenges using nodriver + real Chrome.

    Cloudflare detects headless Chrome and refuses to render the Turnstile
    widget. Therefore, non-headless mode is used (with Xvfb on Linux servers).
    """

    def __init__(
        self,
        headless: bool = False,
        browser_path: Optional[str] = None,
        timeout: int = 30,
        max_retries: int = 3,
        proxy: Optional[str] = None,
        debug: bool = False,
    ):
        self.headless = headless
        self.browser_path = browser_path or self._find_chrome()
        self.timeout = timeout
        self.max_retries = max_retries
        self.proxy = proxy
        self.debug = debug
        if debug:
            logger.setLevel(logging.DEBUG)

    @staticmethod
    def _find_chrome() -> str:
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            for path in (
                f"/usr/bin/{name}",
                f"/usr/local/bin/{name}",
                f"/snap/bin/{name}",
                os.path.expanduser(f"~/.local/bin/{name}"),
            ):
                if os.path.isfile(path):
                    return path
        win_paths = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        ]
        for p in win_paths:
            if os.path.isfile(p):
                return p
        mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.isfile(mac):
            return mac
        return "chrome"

    def _build_browser_args(self) -> list:
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-popup-blocking",
            "--disable-background-networking",
            "--disable-sync",
            "--metrics-recording-only",
            "--disable-component-update",
            "--disable-gpu",
            "--window-size=800,600",
        ]
        if self.proxy:
            args.append(f"--proxy-server={self.proxy}")
        return args

    @staticmethod
    def _extract_value(result) -> Optional[str]:
        """Extract a string value from nodriver evaluate() result.

        nodriver may return a RemoteObject, a plain string, or None depending
        on the evaluation result type.
        """
        if result is None:
            return None
        if isinstance(result, str):
            return result
        # RemoteObject — extract .value or .description
        if hasattr(result, "value") and result.value is not None:
            return str(result.value)
        if hasattr(result, "description") and result.description:
            return str(result.description)
        if hasattr(result, "deep_serialized_value") and result.deep_serialized_value:
            dv = result.deep_serialized_value
            if hasattr(dv, "value") and dv.value is not None:
                return str(dv.value)
        return None

    async def solve(
        self,
        url: str,
        sitekey: str,
        action: Optional[str] = None,
        cdata: Optional[str] = None,
    ) -> dict:
        """Solve a Turnstile challenge and return the token.

        Returns:
            {"token": str, "elapsed": float, "success": bool, "error": Optional[str]}
        """
        start = time.time()
        last_error = None

        for attempt in range(1, self.max_retries + 1):
            logger.debug(f"Attempt {attempt}/{self.max_retries} for {url} sitekey={sitekey[:12]}...")
            try:
                token = await self._solve_once(url, sitekey, action, cdata)
                if token:
                    elapsed = round(time.time() - start, 3)
                    log_success(f"Solved in {elapsed}s — token={token[:20]}...")
                    return {"token": token, "elapsed": elapsed, "success": True, "error": None}
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Attempt {attempt} failed: {e}")
                await asyncio.sleep(0.5)

        elapsed = round(time.time() - start, 3)
        logger.error(f"All {self.max_retries} attempts failed in {elapsed}s")
        return {"token": None, "elapsed": elapsed, "success": False, "error": last_error}

    async def _solve_once(
        self,
        url: str,
        sitekey: str,
        action: Optional[str],
        cdata: Optional[str],
    ) -> Optional[str]:
        """Single solve attempt — launches browser, injects widget, extracts token."""
        browser = None
        try:
            browser_args = self._build_browser_args()
            logger.debug(f"Launching Chrome (headless={self.headless})")

            browser = await nodriver.start(
                headless=self.headless,
                browser_executable_path=self.browser_path,
                browser_args=browser_args,
                sandbox=False,
                lang="en-US",
            )

            # Navigate to the target URL so the origin is correct for Turnstile
            url_normalized = url if url.endswith("/") else url + "/"
            logger.debug(f"Navigating to {url_normalized}")

            try:
                tab = await browser.get(url_normalized)
                await tab.sleep(2)
            except Exception as e:
                logger.debug(f"Navigation to target URL failed ({e}), using about:blank")
                tab = await browser.get("about:blank")
                await tab.sleep(1)

            # Inject Turnstile script + widget div via JavaScript.
            # We clear the page and inject our own widget. The Turnstile script
            # loads dynamically and renders the challenge iframe.
            action_js = f"widget.setAttribute('data-action', '{action}');" if action else ""
            cdata_js = f"widget.setAttribute('data-cdata', '{cdata}');" if cdata else ""
            inject_js = f"""
            (function() {{
                document.body.innerHTML = '';
                document.head.innerHTML = '';
                var script = document.createElement('script');
                script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
                script.async = true;
                document.head.appendChild(script);
                var container = document.createElement('div');
                container.style.cssText = 'background:white;padding:20px;display:flex;justify-content:center;align-items:center;min-height:100vh';
                document.body.appendChild(container);
                var widget = document.createElement('div');
                widget.className = 'cf-turnstile';
                widget.setAttribute('data-sitekey', '{sitekey}');
                {action_js}
                {cdata_js}
                container.appendChild(widget);
                script.onload = function() {{
                    if (window.turnstile) {{
                        window.turnstile.render(widget);
                    }}
                }};
            }})();
            """
            await tab.evaluate(inject_js)
            logger.debug("Injected Turnstile widget via JS")

            # Wait for the Turnstile script to load and initialize
            await asyncio.sleep(3)

            # Poll for the token
            token = await self._poll_for_token(tab)
            return token

        finally:
            if browser:
                try:
                    browser.stop()
                except Exception:
                    pass

    async def _poll_for_token(self, tab, timeout: Optional[int] = None) -> Optional[str]:
        """Poll the page for the cf-turnstile-response token."""
        timeout = timeout or self.timeout
        deadline = time.time() + timeout
        check_interval = 0.5
        attempts = 0

        check_js = """
        (function() {
            var input = document.querySelector('[name="cf-turnstile-response"]');
            if (input && input.value && input.value.length > 10) return input.value;
            try {
                if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                    var resp = window.turnstile.getResponse();
                    if (resp && resp.length > 10) return resp;
                }
            } catch(e) {}
            var widget = document.querySelector('.cf-turnstile');
            if (widget && widget.getAttribute('data-response')) {
                var dr = widget.getAttribute('data-response');
                if (dr.length > 10) return dr;
            }
            return null;
        })();
        """

        while time.time() < deadline:
            attempts += 1
            try:
                result = await tab.evaluate(check_js, return_by_value=True)
                token = self._extract_value(result)

                if token and len(token) > 10:
                    logger.debug(f"Token found after {attempts} polls")
                    return token

                # Try clicking the Turnstile checkbox (managed mode)
                if attempts % 4 == 0:
                    try:
                        await tab.evaluate("""
                        (function() {
                            var iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                            if (iframe) {
                                var rect = iframe.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) {
                                    var ev = new MouseEvent('click', {
                                        bubbles: true, cancelable: true,
                                        clientX: rect.left + 15,
                                        clientY: rect.top + rect.height / 2
                                    });
                                    iframe.dispatchEvent(ev);
                                }
                            }
                        })();
                        """)
                    except Exception:
                        pass

                if attempts % 10 == 0:
                    elapsed_s = int(time.time() - (deadline - timeout))
                    logger.debug(f"Still polling... ({attempts} attempts, {elapsed_s}s elapsed)")

            except Exception as e:
                logger.debug(f"Poll error: {e}")

            await asyncio.sleep(check_interval)

        return None


# ---------------------------------------------------------------------------
# API Server (asyncio + raw HTTP, no extra deps)
# ---------------------------------------------------------------------------

class NeoTurnAPI:
    """Minimal async HTTP API server — no Flask/Quart/aiohttp needed."""

    def __init__(self, solver: NeoTurnSolver, host: str = "127.0.0.1", port: int = 5000):
        self.solver = solver
        self.host = host
        self.port = port
        self.tasks: dict = {}
        self.results: dict = {}

    async def start(self):
        server = await asyncio.start_server(self._handle_connection, self.host, self.port)
        addr = server.sockets[0].getsockname()
        logger.info(f"NeoTurn API listening on http://{addr[0]}:{addr[1]}")
        logger.info(f"  GET /turnstile?url=...&sitekey=...   — submit async solve task")
        logger.info(f"  GET /result?id=<task_id>             — get result")
        logger.info(f"  GET /solve?url=...&sitekey=...       — synchronous solve")
        logger.info(f"  GET /health                          — health check")
        async with server:
            await server.serve_forever()

    async def _handle_connection(self, reader, writer):
        try:
            request_line = await reader.readline()
            await reader.readuntil(b"\r\n\r\n")
            request_str = request_line.decode("utf-8").strip()
            method, path, _ = request_str.split(" ", 2)

            if method != "GET":
                await self._send_json(writer, 405, {"error": "Method not allowed"})
                return

            if "?" in path:
                route, query_str = path.split("?", 1)
            else:
                route, query_str = path, ""
            params = {}
            for pair in query_str.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    params[k] = unquote(v)

            if route in ("/", ""):
                await self._send_html(writer, self._docs_page())
            elif route == "/turnstile":
                await self._handle_turnstile_async(writer, params)
            elif route == "/result":
                await self._handle_get_result(writer, params)
            elif route == "/solve":
                await self._handle_solve_sync(writer, params)
            elif route == "/health":
                await self._send_json(writer, 200, {"status": "ok"})
            else:
                await self._send_json(writer, 404, {"error": "Not found"})

        except Exception as e:
            logger.error(f"Connection error: {e}")
            try:
                await self._send_json(writer, 500, {"error": str(e)})
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_turnstile_async(self, writer, params):
        url = params.get("url")
        sitekey = params.get("sitekey")
        if not url or not sitekey:
            await self._send_json(writer, 400, {"error": "Both 'url' and 'sitekey' are required"})
            return

        task_id = str(uuid.uuid4())
        self.results[task_id] = {"status": "pending"}
        action = params.get("action")
        cdata = params.get("cdata")

        async def run_task():
            result = await self.solver.solve(url, sitekey, action, cdata)
            self.results[task_id] = {"status": "done", **result}

        self.tasks[task_id] = asyncio.create_task(run_task())
        logger.info(f"Task {task_id[:8]} submitted — url={url} sitekey={sitekey[:12]}...")
        await self._send_json(writer, 202, {"task_id": task_id})

    async def _handle_get_result(self, writer, params):
        task_id = params.get("id")
        if not task_id or task_id not in self.results:
            await self._send_json(writer, 400, {"error": "Invalid task ID"})
            return
        result = self.results[task_id]
        if result.get("status") == "pending":
            await self._send_json(writer, 200, {"status": "pending"})
        elif result.get("success"):
            await self._send_json(writer, 200, {
                "status": "ok",
                "token": result["token"],
                "elapsed": result["elapsed"],
            })
        else:
            await self._send_json(writer, 422, {
                "status": "failed",
                "error": result.get("error", "unknown"),
                "elapsed": result.get("elapsed", 0),
            })

    async def _handle_solve_sync(self, writer, params):
        url = params.get("url")
        sitekey = params.get("sitekey")
        if not url or not sitekey:
            await self._send_json(writer, 400, {"error": "Both 'url' and 'sitekey' are required"})
            return
        action = params.get("action")
        cdata = params.get("cdata")
        logger.info(f"Sync solve — url={url} sitekey={sitekey[:12]}...")
        result = await self.solver.solve(url, sitekey, action, cdata)
        if result["success"]:
            await self._send_json(writer, 200, {
                "status": "ok",
                "token": result["token"],
                "elapsed": result["elapsed"],
            })
        else:
            await self._send_json(writer, 422, {
                "status": "failed",
                "error": result.get("error", "unknown"),
                "elapsed": result.get("elapsed", 0),
            })

    async def _send_json(self, writer, status: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        header = (
            f"HTTP/1.1 {status} OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8")
        writer.write(header + body)
        await writer.drain()

    async def _send_html(self, writer, html: str):
        body = html.encode("utf-8")
        header = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8")
        writer.write(header + body)
        await writer.drain()

    @staticmethod
    def _docs_page() -> str:
        return """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>NeoTurn API</title>
<style>body{background:#0a0a0a;color:#c0c0c0;font-family:monospace;padding:40px;max-width:700px;margin:0 auto}
h1{color:#00ff88}code{background:#1a1a1a;padding:2px 6px;border-radius:3px;color:#00ff88}
.endpoint{margin:15px 0;padding:10px;background:#111;border-left:3px solid #00ff88}</style>
</head><body>
<h1>NeoTurn API</h1>
<p>Lightweight Cloudflare Turnstile Solver — nodriver + real Chrome</p>
<div class="endpoint"><code>GET /turnstile?url=...&sitekey=...</code><br>Submit async solve task, returns task_id</div>
<div class="endpoint"><code>GET /result?id=task_id</code><br>Poll for result</div>
<div class="endpoint"><code>GET /solve?url=...&sitekey=...</code><br>Synchronous solve (blocks until done)</div>
<div class="endpoint"><code>GET /health</code><br>Health check</div>
<p style="margin-top:30px;color:#666">No browser automation framework. No third-party API. Just Chrome + CDP.</p>
</body></html>"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_solve(args):
    """Solve a single Turnstile challenge from CLI."""
    ensure_display()
    solver = NeoTurnSolver(
        headless=args.headless,
        timeout=args.timeout,
        max_retries=args.retries,
        proxy=args.proxy,
        debug=args.debug,
    )
    try:
        result = asyncio.run(solver.solve(args.url, args.sitekey, args.action, args.cdata))
    finally:
        cleanup_xvfb()
    if result["success"]:
        print(f"\n{result['token']}")
        if args.verbose:
            print(f"\nElapsed: {result['elapsed']}s", file=sys.stderr)
        sys.exit(0)
    else:
        print(f"FAILED: {result['error']}", file=sys.stderr)
        sys.exit(1)


def cmd_serve(args):
    """Start the API server."""
    ensure_display()
    solver = NeoTurnSolver(
        headless=args.headless,
        timeout=args.timeout,
        max_retries=args.retries,
        proxy=args.proxy,
        debug=args.debug,
    )
    api = NeoTurnAPI(solver, host=args.host, port=args.port)
    try:
        asyncio.run(api.start())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        cleanup_xvfb()


def main():
    parser = argparse.ArgumentParser(
        prog="neoturn",
        description="NeoTurn — Lightweight Cloudflare Turnstile Solver (nodriver + Chrome)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # solve subcommand
    p_solve = sub.add_parser("solve", help="Solve a single Turnstile challenge")
    p_solve.add_argument("--url", required=True, help="Target URL")
    p_solve.add_argument("--sitekey", required=True, help="Turnstile sitekey")
    p_solve.add_argument("--action", default=None, help="Turnstile action (optional)")
    p_solve.add_argument("--cdata", default=None, help="Custom data (optional)")
    p_solve.add_argument("--proxy", default=None, help="Proxy server (e.g. http://host:port)")
    p_solve.add_argument("--timeout", type=int, default=30, help="Timeout per attempt (seconds)")
    p_solve.add_argument("--retries", type=int, default=3, help="Max retry attempts")
    p_solve.add_argument("--headless", action="store_true", help="Run Chrome headless (Cloudflare may detect this)")
    p_solve.add_argument("--debug", action="store_true", help="Debug logging")
    p_solve.add_argument("--verbose", action="store_true", help="Print elapsed time")
    p_solve.set_defaults(func=cmd_solve)

    # serve subcommand
    p_serve = sub.add_parser("serve", help="Start API server")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=5000, help="Bind port (default: 5000)")
    p_serve.add_argument("--proxy", default=None, help="Proxy server")
    p_serve.add_argument("--timeout", type=int, default=30, help="Timeout per attempt (seconds)")
    p_serve.add_argument("--retries", type=int, default=3, help="Max retry attempts")
    p_serve.add_argument("--headless", action="store_true", help="Run Chrome headless (Cloudflare may detect this)")
    p_serve.add_argument("--debug", action="store_true", help="Debug logging")
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
