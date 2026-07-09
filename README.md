<div align="center">

# NeoTurn

**Lightweight Cloudflare Turnstile Solver**

Powered by `nodriver` — direct CDP to Chrome. No Playwright. No third-party API.

[![Python](https://img.shields.io/badge/Python-3.8%2B-blue?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![nodriver](https://img.shields.io/badge/Backend-nodriver-orange?style=flat-square)](https://github.com/ultrafunkamsterdam/nodriver)
[![Chrome](https://img.shields.io/badge/Browser-Chrome-red?style=flat-square&logo=googlechrome&logoColor=white)](https://www.google.com/chrome/)

[Features](#-features) · [Install](#-install) · [Usage](#-usage) · [API](#-api-server) · [Performance](#-performance) · [How it Works](#-how-it-works)

</div>

---

## ✨ Features

- **Single file, single dependency** — `neoturn.py` + `nodriver`. No Playwright, no Quart, no aiohttp, no Flask.
- **Built-in HTTP API server** — raw asyncio, zero extra deps. Sync (`/solve`) and async (`/turnstile` + `/result`) modes.
- **Auto Xvfb** — auto-starts a virtual display on Linux servers. No `xvfb-run` wrapper needed.
- **Real Chrome fingerprint** — uses real Chrome via CDP, not a patched automation browser. Higher success rate against Cloudflare's ML scoring.
- **CLI + library + API** — three ways to use it. Pipe token to stdout, import as Python module, or call via HTTP.
- **Cross-platform** — auto-detects Chrome on Linux, macOS, and Windows.
- **Retry & timeout** — configurable retry attempts and per-solve timeout.
- **Proxy support** — pass `--proxy http://host:port` for IP rotation.

## 📦 Install

```bash
pip install nodriver
```

**Chrome** must be installed:

| OS | Command |
|---|---|
| Linux (Debian/Ubuntu) | `apt install google-chrome-stable` or `apt install chromium-browser` |
| macOS | Download from [google.com/chrome](https://www.google.com/chrome/) |
| Windows | Download from [google.com/chrome](https://www.google.com/chrome/) |

**Xvfb** (Linux servers without display only — NeoTurn auto-starts it):

```bash
apt install xvfb
```

## 🚀 Usage

### CLI — Single solve

```bash
python neoturn.py solve --url https://example.com --sitekey 0x4AAAAAAA...
```

Token is printed to **stdout** (pipe-friendly), logs go to **stderr**:

```
1.io9sk7GD5revm72TPT3gFzS4zmVxWBTcS5wszH3hXU3r4DCGst2Sq9DIHlSDP31oIA...
```

Capture it cleanly:

```bash
TOKEN=$(python neoturn.py solve --url https://example.com --sitekey 0x4AAAAAAA... 2>/dev/null)
echo "$TOKEN"
```

**Options:**

| Flag | Default | Description |
|---|---|---|
| `--url` | *(required)* | Target URL where Turnstile is embedded |
| `--sitekey` | *(required)* | Turnstile sitekey (`0x4AAAA...`) |
| `--action` | `None` | Turnstile action parameter (optional) |
| `--cdata` | `None` | Custom data parameter (optional) |
| `--proxy` | `None` | Proxy server, e.g. `http://host:port` |
| `--timeout` | `30` | Timeout per attempt (seconds) |
| `--retries` | `3` | Max retry attempts |
| `--headless` | `False` | Run Chrome headless (Cloudflare may detect & block this) |
| `--debug` | `False` | Debug logging |
| `--verbose` | `False` | Print elapsed time to stderr |

### Python library

```python
import asyncio
from neoturn import NeoTurnSolver, ensure_display, cleanup_xvfb

async def main():
    ensure_display()  # auto-start Xvfb on Linux if needed
    try:
        solver = NeoTurnSolver(timeout=30, max_retries=3)
        result = await solver.solve(
            url="https://example.com",
            sitekey="0x4AAAAAAA...",
        )
        if result["success"]:
            print(f"Token: {result['token']}")
            print(f"Elapsed: {result['elapsed']}s")
        else:
            print(f"Failed: {result['error']}")
    finally:
        cleanup_xvfb()

asyncio.run(main())
```

## 🌐 API Server

```bash
python neoturn.py serve --port 5000
```

| Endpoint | Method | Description |
|---|---|---|
| `/solve?url=...&sitekey=...` | GET | Synchronous solve — blocks until done, returns token |
| `/turnstile?url=...&sitekey=...` | GET | Submit async task, returns `task_id` |
| `/result?id=<task_id>` | GET | Poll for async task result |
| `/health` | GET | Health check |
| `/` | GET | API docs (HTML) |

**Synchronous solve:**

```bash
curl "http://127.0.0.1:5000/solve?url=https://example.com&sitekey=0x4AAAAAAA..."
# {"status":"ok","token":"1.io9sk7GD5...","elapsed":5.769}
```

**Asynchronous solve (for batch processing):**

```bash
# Submit
curl "http://127.0.0.1:5000/turnstile?url=https://example.com&sitekey=0x4AAAAAAA..."
# {"task_id":"d2cbb257-9c37-4f9c-9bc7-1eaee72d96a8"}

# Poll until done
curl "http://127.0.0.1:5000/result?id=d2cbb257-9c37-4f9c-9bc7-1eaee72d96a8"
# {"status":"ok","token":"1.io9sk7GD5...","elapsed":5.769}
```

**Server options:**

```bash
python neoturn.py serve --host 0.0.0.0 --port 5000 --proxy http://host:port --timeout 30 --retries 3 --debug
```

## 📊 Performance

Tested with real Cloudflare Turnstile sitekeys (July 2026):

| Scenario | Time | Success |
|---|---|---|
| First solve (cold start) | 5-10s | ✅ |
| Subsequent solves | 3-7s | ✅ |
| With HTTP proxy | 7-15s | ✅ |
| Headless mode | — | ❌ Cloudflare detects & blocks |

**Real-world test** — registered accounts on New-API relay providers requiring Turnstile:

| Provider | Register | Login | Token extract | Total Turnstile solves |
|---|---|---|---|---|
| `www.apiddt.com` | ✅ | ✅ | ✅ | 4 (OTP + register + login + token) |
| `api.euzhi.com` | ✅ | ✅ | ✅ | 4 |

Full register flow (OTP → register → login → token creation) takes ~25-30s with 4 Turnstile solves.

## 🔧 How it Works

```
┌─────────────┐     ┌──────────────────────────┐     ┌─────────────┐
│  NeoTurn    │────▶│  nodriver (CDP)          │────▶│  Real Chrome │
│  neoturn.py │     │  - launch browser        │     │  - real fingerprint
│             │     │  - navigate to target URL│     │  - WebGL, canvas, fonts
│             │◀────│  - inject Turnstile widget│◀────│  - behavioral signals
│             │     │  - poll for token        │     │  - PoW challenge
│  token out  │     │  - return token          │     │  - ML scoring pass
└─────────────┘     └──────────────────────────┘     └─────────────┘
```

1. **Launch Chrome** via nodriver (direct CDP, no Playwright injection points)
2. **Navigate to target URL** — sets the correct browser origin so Turnstile accepts the sitekey
3. **Inject Turnstile widget** — dynamically loads `challenges.cloudflare.com/turnstile/v0/api.js` and renders the widget
4. **Poll for token** — checks `cf-turnstile-response` hidden input, `turnstile.getResponse()`, and widget data attributes
5. **Return token** — valid `cf-turnstile-response` string ready for form submission

**Why not headless?** Cloudflare Turnstile collects 55+ browser signals (WebGL, canvas, audio, fonts, screen, navigator, React state) and runs them through ML scoring. Headless Chrome is missing key signals and gets blocked. NeoTurn uses non-headless Chrome with auto-started Xvfb on Linux.

## ⚖️ Comparison

| Project | Backend | Deps | Headless | API Server | Auto-Xvfb |
|---|---|---|---|---|---|
| **NeoTurn** | nodriver (CDP) | **1** | Xvfb auto | Built-in (zero deps) | ✅ |
| Theyka/Turnstile-Solver | patchright (Playwright) | 4+ | Manual `xvfb-run` | Quart | ❌ |
| taozhiyu/Turnstile-Solver | Camoufox | 3+ | Manual | Quart | ❌ |
| EzSolver | nodriver | 1 | Xvfb manual | aiohttp | ❌ |
| odell0111/turnstile_solver | patchright | 3+ | Manual | aiohttp | ❌ |

## ⚠️ Limitations

- **Not truly browserless** — Cloudflare Turnstile requires a real browser environment. No working browserless Turnstile solver exists as of 2026. Anyone claiming otherwise is either using a third-party API (Capsolver, 2Captcha) or a dead/broken reverse-engineering attempt.
- **Non-headless required** — Cloudflare detects headless Chrome. Xvfb provides a virtual display on headless Linux servers.
- **Token TTL: ~300 seconds** — use the token immediately after solving.
- **Single-use** — each token can only be validated once via Cloudflare's siteverify endpoint.
- **Sitekey + URL bound** — tokens are bound to the specific sitekey and page URL they were generated for.

## 📁 Project Structure

```
NeoTurn/
├── neoturn.py        # Single-file solver + API server (643 lines)
├── requirements.txt  # 1 dependency: nodriver
├── LICENSE           # MIT
├── README.md         # This file
└── .gitignore
```

## 🤝 Credits

- [nodriver](https://github.com/ultrafunkamsterdam/nodriver) — the CDP library that makes this lightweight
- [Theyka/Turnstile-Solver](https://github.com/Theyka/Turnstile-Solver) — original patchright-based solver that inspired this project
- [EzSolver](https://github.com/ismoiloffS/EzSolver) — proved nodriver works for Turnstile

## 📜 License

MIT — see [LICENSE](LICENSE).

---

<div align="center">

**NeoTurn** — Lightweight. Fast. No bloat.

</div>
