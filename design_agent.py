"""
Website Design Recreation Agent
================================
Given a reference screenshot, this agent generates a pixel-perfect,
functional website using Claude's vision + code capabilities.

Workflow (follows CLAUDE.md contract):
  1. Analyze the reference screenshot.
  2. Generate a single index.html (Tailwind CSS CDN + inline JS).
  3. Screenshot the rendered page with Playwright (headless Chromium).
  4. Compare screenshots → find mismatches (px-level).
  5. Fix mismatches → re-screenshot.
  6. Repeat until no visible differences remain (min 2 rounds).

Entry point
-----------
  asyncio.run(design_website(ref_image_path, output_dir))

or import and call from server.py as an async background task.
"""

import asyncio
import base64
import os
from pathlib import Path
from typing import Callable, Awaitable, Optional

import anthropic
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MODEL = os.getenv("DESIGN_MODEL", "claude-opus-4-6")   # best vision + code model
VIEWPORT_W = 1440
VIEWPORT_H = 900

TOOLS = [
    {
        "name": "write_html",
        "description": (
            "Write the complete website HTML to disk. Call this every time you want to "
            "create or update the page. The file must be a single self-contained index.html "
            "(Tailwind via CDN, all CSS/JS inline)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "html": {
                    "type": "string",
                    "description": "The full HTML document content.",
                }
            },
            "required": ["html"],
        },
    },
    {
        "name": "screenshot_page",
        "description": (
            "Render the current index.html in a real browser and return a screenshot. "
            "Use this to visually compare your output against the reference image. "
            "Always call this after write_html before deciding on next steps."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "finish",
        "description": (
            "Call this when the website visually matches the reference with no remaining "
            "differences. Do NOT call this before at least 2 comparison rounds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "One-sentence summary of the final website.",
                }
            },
            "required": ["summary"],
        },
    },
]

SYSTEM_PROMPT = """You are an expert front-end developer who specialises in pixel-perfect UI recreation from screenshots.

Your task: given a reference screenshot, produce a production-quality website that looks identical to it.

## Mandatory rules
- Output a **single self-contained index.html** — all CSS and JS must be inline or from a CDN.
- Use **Tailwind CSS** via CDN: `<script src="https://cdn.tailwindcss.com"></script>`
- Use placeholder images from `https://placehold.co/WxH/hex/hex?text=Label` when source images are absent.
- Preserve **exact hex colours, font sizes, weights, line-heights, spacing, border-radii, shadows**.
- Make the layout **responsive** (mobile-first).
- Wire up any obvious interactive elements (menus, tabs, accordions, modals, forms) with vanilla JS.
- Do **not** add content or sections that are not in the reference image.

## Iteration loop you must follow
1. Analyse the reference image carefully (layout, colours, typography, components).
2. Call `write_html` with your best initial implementation.
3. Call `screenshot_page` to see how it renders.
4. Compare the rendered screenshot against the reference pixel-by-pixel. List every mismatch
   (e.g. "heading font-size is 28px but reference shows ~22px", "card gap should be 24px not 16px").
5. Call `write_html` again with all fixes applied.
6. Repeat steps 3-5 — do **at least 2 full compare-and-fix rounds**.
7. Only call `finish` when no visible differences remain."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _screenshot_file(html_path: str) -> str:
    """Render a local HTML file in headless Chromium. Returns base64-encoded PNG."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": VIEWPORT_W, "height": VIEWPORT_H})
        await page.goto(f"file://{Path(html_path).resolve()}")
        await page.wait_for_load_state("networkidle", timeout=15_000)
        png = await page.screenshot(full_page=True)
        await browser.close()
    return base64.b64encode(png).decode()


ProgressCallback = Callable[[str, str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------

async def design_website(
    reference_image_path: str,
    output_dir: str,
    progress: Optional[ProgressCallback] = None,
) -> dict:
    """
    Analyze *reference_image_path* and generate a website in *output_dir*.

    Returns
    -------
    {
      "success": bool,
      "html_path": str,      # absolute path to generated index.html
      "iterations": int,     # number of write_html calls
      "summary": str,
      "error": str,          # only present when success=False
    }
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    html_path = str(out / "index.html")

    # Encode reference image
    ref_bytes = Path(reference_image_path).read_bytes()
    ext = Path(reference_image_path).suffix.lower()
    media_type = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
    ref_b64 = base64.b64encode(ref_bytes).decode()

    if progress:
        await progress("analyzing", "Analyzing the reference screenshot…")

    client = anthropic.Anthropic()

    messages: list[dict] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": ref_b64},
                },
                {
                    "type": "text",
                    "text": (
                        "This is the reference screenshot. "
                        "Recreate it as a functional website following your iteration workflow. "
                        "Do at least 2 compare-and-fix rounds before calling finish."
                    ),
                },
            ],
        }
    ]

    iterations = 0
    summary = ""

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})

        tool_results: list[dict] = []
        finished = False

        for block in response.content:
            if block.type != "tool_use":
                continue

            name = block.name
            inp  = block.input

            # ── write_html ─────────────────────────────────────────────────
            if name == "write_html":
                Path(html_path).write_text(inp["html"], encoding="utf-8")
                iterations += 1
                if progress:
                    await progress("generating", f"Writing HTML — iteration {iterations}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"index.html saved ({len(inp['html'])} chars). Call screenshot_page to see how it looks.",
                })

            # ── screenshot_page ────────────────────────────────────────────
            elif name == "screenshot_page":
                if not Path(html_path).exists():
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "No HTML file yet. Call write_html first.",
                    })
                    continue

                if progress:
                    await progress("comparing", "Screenshotting the rendered page…")
                try:
                    b64 = await _screenshot_file(html_path)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/png", "data": b64},
                            },
                            {
                                "type": "text",
                                "text": (
                                    "This is how your current HTML renders. "
                                    "Compare every detail against the reference and list all remaining differences."
                                ),
                            },
                        ],
                    })
                except Exception as exc:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Screenshot failed: {exc}",
                    })

            # ── finish ─────────────────────────────────────────────────────
            elif name == "finish":
                summary = inp.get("summary", "")
                finished = True
                if progress:
                    await progress("done", summary or "Website complete!")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Marked as complete.",
                })

        if finished or response.stop_reason == "end_turn" or not tool_results:
            break

        messages.append({"role": "user", "content": tool_results})

    if not Path(html_path).exists():
        return {"success": False, "error": "Agent produced no HTML output.", "iterations": 0, "summary": ""}

    return {
        "success": True,
        "html_path": html_path,
        "iterations": iterations,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python design_agent.py <reference_screenshot.png> [output_dir]")
        sys.exit(1)

    ref   = sys.argv[1]
    outd  = sys.argv[2] if len(sys.argv) > 2 else "designs/output"

    async def _main():
        async def _progress(stage, detail):
            print(f"[{stage}] {detail}")

        result = await design_website(ref, outd, _progress)
        if result["success"]:
            print(f"\nDone! {result['iterations']} iteration(s). HTML at: {result['html_path']}")
        else:
            print(f"\nFailed: {result['error']}")

    asyncio.run(_main())
