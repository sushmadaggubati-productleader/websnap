# WebSnap — AI-Powered Full-Page Screenshot Agent

> Capture pixel-perfect screenshots of any website — even extremely long pages — with a single click.

Built with **Claude AI**, **Playwright**, and **FastAPI**.

---

## Features

- **Full-page capture** — Automatically scrolls through the entire page to trigger lazy-loaded content before screenshotting
- **Handles extremely long pages** — Pages taller than 30,000px are captured in tiles and stitched together seamlessly using Pillow
- **Multiple output formats** — Download screenshots as PNG, JPG, GIF, or SVG
- **Image resizing** — Resize by width, height, or both (aspect ratio preserved when only one dimension is set)
- **Bulk capture** — Submit multiple URLs at once and download all results as a ZIP file
- **Real-time status** — Live progress indicators for each capture job
- **Clean web UI** — No-install frontend served directly from the backend

---

## How It Works

```
User submits URL
      ↓
Playwright launches headless Chromium
      ↓
Auto-scrolls page to trigger lazy-loaded content
      ↓
Measures full page dimensions
      ↓
  ┌─────────────────────────────────────┐
  │  ≤ 30,000px tall → native full_page │
  │  > 30,000px tall → tile + stitch   │
  └─────────────────────────────────────┘
      ↓
Pillow converts format + resizes
      ↓
File ready for download
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| AI Orchestration | [Claude API](https://anthropic.com) (`claude-opus-4-6`) with tool use |
| Browser Automation | [Playwright](https://playwright.dev/python/) (async, headless Chromium) |
| Image Processing | [Pillow](https://pillow.readthedocs.io/) |
| Backend | [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) |
| Frontend | Tailwind CSS (CDN) |

---

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/sushmadaggubati-productleader/websnap.git
cd websnap
```

### 2. Set up a Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 4. Set your Anthropic API key

```bash
export ANTHROPIC_API_KEY="your-api-key-here"
```

Get a key at [console.anthropic.com](https://console.anthropic.com).

### 5. Start the server

```bash
uvicorn server:app --reload --port 8000
```

### 6. Open in your browser

```
http://localhost:8000
```

---

## Usage

### Single URL
1. Paste any URL into the input field
2. Choose your output format (PNG / JPG / GIF / SVG)
3. Optionally set a resize width and/or height
4. Click **Capture Screenshot**
5. Download when ready

### Bulk Capture
1. Switch to the **Bulk** tab
2. Enter one URL per line
3. Choose format and optional resize dimensions
4. Click **Capture All**
5. Download individual files or **Download All as ZIP**

---

## CLI Usage

You can also run the agent directly from the command line:

```bash
python agent.py "Take a screenshot of https://en.wikipedia.org/wiki/Python_(programming_language)"
```

Or in interactive mode:

```bash
python agent.py
```

---

## Project Structure

```
websnap/
├── agent.py          # Core screenshot agent (Playwright + Claude API)
├── server.py         # FastAPI backend
├── index.html        # Web UI (Tailwind CSS)
├── requirements.txt  # Python dependencies
├── CLAUDE.md         # AI workflow rules
└── .gitignore
```

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/capture` | Start a single capture job |
| `GET` | `/status/{job_id}` | Poll job status |
| `GET` | `/download/{job_id}` | Download completed screenshot |
| `POST` | `/bulk-capture` | Start a batch of capture jobs |
| `GET` | `/status-bulk/{batch_id}` | Poll batch status |
| `GET` | `/download-bulk/{batch_id}` | Download all results as ZIP |

---

## License

MIT
