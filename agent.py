"""
Screenshot Agent — captures full-page screenshots of even extremely long websites.

Strategy for long pages:
  1. Auto-scroll the page to trigger all lazy-loaded content.
  2. For pages ≤ 30 000 px tall: use Playwright's native full_page=True.
  3. For pages > 30 000 px tall: tile multiple viewport screenshots and
     stitch them together with Pillow, avoiding memory / codec limits.

Usage:
  python agent.py "Take a screenshot of https://en.wikipedia.org/wiki/Python_(programming_language)"
  python agent.py   (interactive prompt)
"""

import asyncio
import base64
import io
import os
import sys
from pathlib import Path

import anthropic
from playwright.async_api import async_playwright

try:
    from PIL import Image
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCREENSHOTS_DIR = Path("screenshots")
SCREENSHOTS_DIR.mkdir(exist_ok=True)

# Pages taller than this (pixels) are captured in tiles and stitched
TILE_THRESHOLD_PX = 30_000
VIEWPORT_WIDTH = 1920
VIEWPORT_HEIGHT = 1080
SCROLL_STEP = 300          # pixels per scroll step during lazy-load phase
SCROLL_DELAY_MS = 120      # ms between scroll steps
POST_SCROLL_WAIT_MS = 2000 # wait after scrolling for content to settle

client = anthropic.Anthropic()


# ---------------------------------------------------------------------------
# Core screenshot logic
# ---------------------------------------------------------------------------

async def _auto_scroll(page) -> None:
    """
    Scroll through the entire page so lazy-loaded content renders.

    Edge cases handled:
    - page.evaluate() accepts only ONE arg in Playwright Python — pass as array
    - Dynamic pages (content added while scrolling): re-sample scrollHeight each tick
    - Infinite-scroll / very tall pages: stop after MAX_SCROLL_ITERS iterations
    - Zero-height / non-scrollable pages: resolve immediately
    """
    MAX_SCROLL_ITERS = 500  # hard ceiling: 500 × 300 px = 150 000 px max scroll
    await page.evaluate(
        """
        async ([step, delay, maxIters]) => {
            await new Promise((resolve) => {
                let iter = 0;
                const timer = setInterval(() => {
                    const scrollHeight = Math.max(
                        document.body.scrollHeight,
                        document.documentElement.scrollHeight,
                        1
                    );
                    window.scrollBy(0, step);
                    iter++;
                    const atBottom  = (window.scrollY + window.innerHeight) >= scrollHeight - 2;
                    const hitCeiling = iter >= maxIters;
                    if (atBottom || hitCeiling) {
                        clearInterval(timer);
                        window.scrollTo(0, 0);
                        resolve();
                    }
                }, delay);
            });
        }
        """,
        [SCROLL_STEP, SCROLL_DELAY_MS, MAX_SCROLL_ITERS],   # single arg — array
    )
    await page.wait_for_timeout(POST_SCROLL_WAIT_MS)


async def _page_dimensions(page) -> tuple[int, int]:
    """Return (width, height) of the full page content, using the largest available value."""
    dims = await page.evaluate(
        """() => ({
            w: Math.max(document.body.scrollWidth,  document.documentElement.scrollWidth,  1),
            h: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight, 1)
        })"""
    )
    return dims["w"], dims["h"]


async def _capture_tiles_and_stitch(page, output_path: str, page_height: int) -> None:
    """
    Capture the page in viewport-sized tiles and stitch into one image.
    Used when the page is taller than TILE_THRESHOLD_PX.
    """
    if not PILLOW_AVAILABLE:
        raise RuntimeError(
            "Pillow is required for stitching extremely long pages. "
            "Install it with:  pip install Pillow"
        )

    tile_paths: list[Path] = []
    y = 0
    tile_idx = 0

    while y < page_height:
        await page.evaluate(f"window.scrollTo(0, {y})")
        await page.wait_for_timeout(200)

        tile_path = SCREENSHOTS_DIR / f"_tile_{tile_idx}.png"
        await page.screenshot(path=str(tile_path), clip={
            "x": 0, "y": y,
            "width": VIEWPORT_WIDTH,
            "height": min(VIEWPORT_HEIGHT, page_height - y),
        })
        tile_paths.append(tile_path)
        y += VIEWPORT_HEIGHT
        tile_idx += 1

    # Stitch tiles vertically
    images = [Image.open(str(p)) for p in tile_paths]
    total_height = sum(img.height for img in images)
    canvas = Image.new("RGB", (VIEWPORT_WIDTH, total_height))
    offset = 0
    for img in images:
        canvas.paste(img, (0, offset))
        offset += img.height
        img.close()

    canvas.save(output_path)
    canvas.close()

    # Clean up tiles
    for p in tile_paths:
        p.unlink(missing_ok=True)


async def capture_screenshot(url: str, output_filename: str = "screenshot.png") -> dict:
    """
    Navigate to *url*, trigger lazy-loaded content, and save a full-page
    screenshot to *screenshots/<output_filename>*.

    Returns a result dict with keys: success, path, width_px, height_px,
    file_size_bytes, method, and (on error) error.
    """
    output_path = SCREENSHOTS_DIR / output_filename

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            print(f"  → Navigating to {url} …")
            try:
                # networkidle is ideal but some SPAs never fully settle
                await page.goto(url, wait_until="networkidle", timeout=60_000)
            except Exception:
                # Fall back to domcontentloaded + short wait
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(3_000)

            print("  → Auto-scrolling to trigger lazy-loaded content …")
            await _auto_scroll(page)

            page_w, page_h = await _page_dimensions(page)
            print(f"  → Page dimensions: {page_w} × {page_h} px")

            if page_h <= TILE_THRESHOLD_PX:
                print("  → Taking full-page screenshot (native) …")
                await page.screenshot(
                    path=str(output_path),
                    full_page=True,
                    timeout=120_000,
                )
                method = "native_full_page"
            else:
                print(
                    f"  → Page is {page_h} px tall — using tile-and-stitch method …"
                )
                await _capture_tiles_and_stitch(page, str(output_path), page_h)
                method = "tile_and_stitch"

            await browser.close()

        file_size = output_path.stat().st_size
        return {
            "success": True,
            "path": str(output_path),
            "width_px": page_w,
            "height_px": page_h,
            "file_size_bytes": file_size,
            "method": method,
        }

    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Post-processing: format conversion + resize
# ---------------------------------------------------------------------------

SUPPORTED_FORMATS = {"png", "jpg", "jpeg", "gif", "svg"}


def post_process_image(
    input_path: str,
    output_path: str,
    fmt: str = "png",
    resize_width: int | None = None,
    resize_height: int | None = None,
) -> dict:
    """
    Convert and/or resize a captured screenshot PNG.

    Formats
    -------
    png  — lossless, full colour, transparency preserved
    jpg  — lossy (quality=92), RGB; transparency flattened onto white
    gif  — 256-colour palette (expect colour loss on photo-like pages)
    svg  — raster image embedded as base64 inside an <svg> wrapper

    Resize rules
    ------------
    • width only  → height scaled to preserve aspect ratio
    • height only → width scaled to preserve aspect ratio
    • both        → exact resize (may distort aspect ratio)
    • neither     → original dimensions kept
    """
    if not PILLOW_AVAILABLE:
        raise RuntimeError("Pillow is required for format conversion / resizing.")

    fmt = fmt.lower().lstrip(".")
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"Unsupported format '{fmt}'. Choose from: {SUPPORTED_FORMATS}")

    img = Image.open(input_path)
    orig_w, orig_h = img.size

    # ── Resize ────────────────────────────────────────────────────────────────
    if resize_width or resize_height:
        rw = int(resize_width)  if resize_width  else None
        rh = int(resize_height) if resize_height else None
        if rw and rh:
            new_size = (rw, rh)
        elif rw:
            new_size = (rw, max(1, round(orig_h * rw / orig_w)))
        else:
            new_size = (max(1, round(orig_w * rh / orig_h)), rh)
        img = img.resize(new_size, Image.LANCZOS)

    final_w, final_h = img.size

    # ── Format conversion ─────────────────────────────────────────────────────
    if fmt == "jpg":
        # Flatten any transparency onto a white background
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            mask = img.split()[3] if img.mode == "RGBA" else None
            bg.paste(img.convert("RGBA") if img.mode == "P" else img, mask=mask)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.save(output_path, "JPEG", quality=92, optimize=True)

    elif fmt == "gif":
        img = img.convert("P", palette=Image.ADAPTIVE, colors=256)
        img.save(output_path, "GIF")

    elif fmt == "svg":
        # Embed the (resized) image as a base64 PNG inside an SVG wrapper
        buf = io.BytesIO()
        embed = img if img.mode in ("RGBA", "RGB") else img.convert("RGBA")
        embed.save(buf, "PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        svg_content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{final_w}" height="{final_h}" '
            f'viewBox="0 0 {final_w} {final_h}">\n'
            f'  <image href="data:image/png;base64,{b64}" '
            f'width="{final_w}" height="{final_h}"/>\n'
            "</svg>\n"
        )
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(svg_content)

    else:  # png
        img.save(output_path, "PNG", optimize=True)

    return {
        "success": True,
        "path": output_path,
        "final_width": final_w,
        "final_height": final_h,
        "file_size_bytes": Path(output_path).stat().st_size,
    }


# ---------------------------------------------------------------------------
# Tool definitions for Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "capture_screenshot",
        "description": (
            "Capture a full-page screenshot of any website URL, including "
            "extremely long pages. Automatically scrolls through the page first "
            "to trigger lazy-loaded content, then saves a PNG file. "
            "Returns the file path, page dimensions, and file size."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full URL of the website to screenshot (must start with http:// or https://).",
                },
                "output_filename": {
                    "type": "string",
                    "description": (
                        "Optional. Filename for the PNG screenshot, e.g. 'homepage.png'. "
                        "Defaults to 'screenshot.png'. Saved in the screenshots/ directory."
                    ),
                },
            },
            "required": ["url"],
        },
    }
]


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

async def run_agent(user_request: str) -> None:
    """Send the user request to Claude and handle tool calls."""
    messages = [{"role": "user", "content": user_request}]

    print(f"\nAgent starting — request: {user_request}\n")

    while True:
        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=4096,
            thinking={"type": "adaptive"},
            tools=TOOLS,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()

        # Print any text Claude produced in this turn
        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"\nClaude: {block.text}\n")

        if response.stop_reason == "end_turn":
            break

        # Execute tool calls
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tool_use in tool_use_blocks:
            if tool_use.name == "capture_screenshot":
                url = tool_use.input["url"]
                filename = tool_use.input.get("output_filename", "screenshot.png")
                print(f"[Tool] capture_screenshot → {url}")
                result = await capture_screenshot(url, filename)

                if result["success"]:
                    content = (
                        f"Screenshot saved successfully.\n"
                        f"  Path: {result['path']}\n"
                        f"  Dimensions: {result['width_px']} × {result['height_px']} px\n"
                        f"  File size: {result['file_size_bytes']:,} bytes\n"
                        f"  Method: {result['method']}"
                    )
                    print(f"  ✓ {content}")
                else:
                    content = f"Screenshot failed: {result['error']}"
                    print(f"  ✗ {content}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": content,
                })

        messages.append({"role": "user", "content": tool_results})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:
        request = " ".join(sys.argv[1:])
    else:
        print("Screenshot Agent — captures full-page screenshots of any website.")
        print("Examples:")
        print('  "Take a screenshot of https://example.com"')
        print('  "Screenshot https://news.ycombinator.com and save as hn.png"')
        print()
        request = input("Your request: ").strip()
        if not request:
            request = "Take a screenshot of https://en.wikipedia.org/wiki/Python_(programming_language)"

    asyncio.run(run_agent(request))
