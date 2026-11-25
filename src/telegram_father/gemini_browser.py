import asyncio
import os
import re
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

ALLOWED_EXTENSIONS = (".ppt", ".pptx", ".pdf")
PPTX_EXTENSIONS = (".ppt", ".pptx")
out_dir = Path.cwd().resolve() / "downloads"
out_dir.mkdir(parents=True, exist_ok=True)
print("Saving files to:", out_dir)

CODE_MARKERS = (
    "import ",
    "from ",
    "def ",
    "async def ",
    "class ",
    "console.log",
    "#!/usr/bin",
    "<html",
    "<div",
    "playwright",
    "asyncio",
)

retry_prompt = "create the power point file already asked for only"
max_attempts = 10
wait_seconds = 120


def is_allowed_doc(path: Path, require_pptx: bool = False) -> bool:
    allowed = PPTX_EXTENSIONS if require_pptx else ALLOWED_EXTENSIONS
    return path.suffix.lower() in allowed


def build_dated_dest(ext: str) -> Path:
    """Build a unique timestamped destination in ./downloads."""
    clean_ext = ext.lower() if ext.startswith(".") else f".{ext.lower()}"
    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    candidate = out_dir / f"{timestamp}{clean_ext}"
    counter = 2
    while candidate.exists():
        candidate = out_dir / f"{timestamp}-{counter}{clean_ext}"
        counter += 1
    return candidate


def has_valid_file_header(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 200:
        return False
    head = path.read_bytes()[:8]
    ext = path.suffix.lower()
    if ext in (".ppt", ".pptx"):
        return head.startswith(b"PK\x03\x04") or head.startswith(b"\xd0\xcf\x11\xe0")
    if ext == ".pdf":
        return head.startswith(b"%PDF-")
    return False


def extract_slides_id(url: str) -> str | None:
    m = re.search(r"docs\.google\.com/presentation/d/([a-zA-Z0-9_-]+)", url or "")
    return m.group(1) if m else None


async def ensure_cdp_chrome(port: int = 9222, timeout: int = 15) -> str:
    url = f"http://127.0.0.1:{port}"
    version_url = f"{url}/json/version"

    def is_up() -> bool:
        try:
            with urllib.request.urlopen(version_url, timeout=1.5) as resp:
                return resp.status == 200
        except Exception:
            return False

    if not is_up():
        user_data_dir = os.path.expanduser("~/.chrome-playwright")
        subprocess.Popen(
            [
                "google-chrome",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={user_data_dir}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(timeout * 4):
            if is_up():
                break
            await asyncio.sleep(0.25)

    return url


async def response_looks_like_code(latest_response) -> bool:
    return await latest_response.evaluate(
        """(el, markers) => {
            if (el.querySelector("pre, code, code-block, .code-block")) {
                return true;
            }
            const txt = (el.innerText || "").toLowerCase();
            const hits = markers.filter(m => txt.includes(m.toLowerCase()));
            return hits.length >= 2;
        }""",
        list(CODE_MARKERS),
    )


async def save_if_allowed_download(
    download,
    latest_response,
    fallback_name: str = "presentation.pptx",
    require_pptx: bool = False,
) -> Path | None:
    suggested_name = download.suggested_filename or fallback_name
    suggested_path = Path(suggested_name)

    if suggested_path.suffix and not is_allowed_doc(suggested_path, require_pptx=require_pptx):
        print(f"Ignoring unsupported download ({suggested_name}); require_pptx={require_pptx}")
        return None

    ext = suggested_path.suffix.lower() or Path(fallback_name).suffix.lower() or ".pptx"
    temp_dest = out_dir / f".tmp_{datetime.now().strftime('%H%M%S_%f')}{ext}"
    await download.save_as(str(temp_dest))

    if latest_response is not None and ext == ".pdf" and await response_looks_like_code(latest_response):
        temp_dest.unlink(missing_ok=True)
        print(f"Rejected code-related PDF: {suggested_name}")
        return None

    if not is_allowed_doc(temp_dest, require_pptx=require_pptx) or not has_valid_file_header(temp_dest):
        temp_dest.unlink(missing_ok=True)
        print(f"Removed invalid or non-PowerPoint file: {suggested_name}")
        return None

    final_dest = build_dated_dest(ext)
    temp_dest.replace(final_dest)
    return final_dest


async def send_prompt(page, text: str) -> None:
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.25)

    editor = page.locator('rich-textarea div.ql-editor[contenteditable="true"]').last
    await editor.wait_for(state="visible", timeout=20000)
    await editor.scroll_into_view_if_needed()
    await editor.click()

    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await editor.fill(text)

    async def text_is_present() -> bool:
        current = await editor.evaluate(
            """el => ((el.innerText || el.textContent || "")
                .replace(/\\u00a0/g, " ")
                .replace(/\\s+/g, " ")
                .trim())"""
        )
        normalized_expected = " ".join(text.split())
        return normalized_expected in current

    for _ in range(10):
        if await text_is_present():
            break
        await asyncio.sleep(0.2)

    if not await text_is_present():
        await editor.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await editor.press_sequentially(text, delay=15)

    await page.wait_for_function(
        """([el, expected]) => {
            const actual = (el.innerText || el.textContent || "")
                .replace(/\\u00a0/g, " ")
                .replace(/\\s+/g, " ")
                .trim();
            return actual.includes(expected);
        }""",
        arg=[await editor.element_handle(), " ".join(text.split())],
        timeout=15000,
    )

    send_locators = [
        page.get_by_role("button", name=re.compile(r"send|submit|إرسال", re.I)).last,
        page.locator('button[aria-label*="send" i], button[aria-label*="submit" i], button.send-button').last,
    ]

    clicked = False
    for btn in send_locators:
        try:
            if await btn.count() > 0 and await btn.is_visible():
                await page.wait_for_function(
                    """b => b.getAttribute("aria-disabled") !== "true" && !b.disabled""",
                    arg=await btn.element_handle(),
                    timeout=5000,
                )
                await btn.click(timeout=5000)
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        await editor.focus()
        await page.keyboard.press("Enter")


async def wait_for_response_done(page, response_candidates, timeout_seconds: int = 120) -> str:
    prev_count = await response_candidates.count()
    stop_btn = page.get_by_role("button", name=re.compile(r"stop|إيقاف", re.I))

    for _ in range(30):
        if await response_candidates.count() > prev_count:
            break
        if await stop_btn.count() > 0 and await stop_btn.first.is_visible():
            break
        await asyncio.sleep(0.5)

    await response_candidates.last.wait_for(state="visible", timeout=60000)
    latest = response_candidates.last

    prev_text = ""
    stable_ticks = 0
    for _ in range(timeout_seconds * 2):
        curr_text = (await latest.inner_text()).strip()
        is_stopping = await stop_btn.count() > 0 and await stop_btn.first.is_visible()
        if curr_text and not is_stopping and curr_text == prev_text:
            stable_ticks += 1
            if stable_ticks >= 8:
                break
        else:
            stable_ticks = 0
            prev_text = curr_text
        await asyncio.sleep(0.5)

    return (await latest.inner_text()).strip()


async def try_direct_download(page, latest_response) -> list[Path]:
    """Check for a visible Download control and accept only valid .ppt, .pptx, or non-code .pdf files."""
    if await response_looks_like_code(latest_response):
        print("Response contains code; skipping direct download so code is not saved as PDF.")
        return []

    candidates = [
        latest_response.get_by_role("button", name=re.compile(r"download", re.I)),
        latest_response.get_by_role("link", name=re.compile(r"download", re.I)),
        latest_response.locator('button[aria-label*="download" i], a[aria-label*="download" i], [download]'),
    ]

    for loc in candidates:
        count = await loc.count()
        for i in range(count):
            el = loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                async with page.expect_download(timeout=15000) as dl_info:
                    await el.click()
                download = await dl_info.value
                saved_path = await save_if_allowed_download(
                    download, latest_response, "gemini_presentation.pptx", require_pptx=True
                )
                if saved_path is not None:
                    print(f"Downloaded valid file to {saved_path}")
                    return [saved_path]
            except Exception:
                continue

    return []


async def download_pptx_from_slides_page(context, slides_page, slides_url: str, dest: Path) -> bool:
    """Visit the Google Slides page and download the presentation as .pptx."""
    pres_id = extract_slides_id(slides_url)
    if not pres_id or not is_allowed_doc(dest, require_pptx=True):
        return False

    export_pptx_url = f"https://docs.google.com/presentation/d/{pres_id}/export/pptx"

    if slides_page is not None and not slides_page.is_closed():
        await slides_page.bring_to_front()
        try:
            await slides_page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(1.5)

        try:
            async with slides_page.expect_download(timeout=25000) as dl_info:
                await slides_page.evaluate(
                    """url => {
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = '';
                        document.body.appendChild(a);
                        a.click();
                        a.remove();
                    }""",
                    export_pptx_url,
                )
            download = await dl_info.value
            temp_path = await save_if_allowed_download(
                download, None, dest.name, require_pptx=True
            )
            if temp_path is not None and has_valid_file_header(temp_path):
                temp_path.replace(dest)
                print(f"Downloaded PPTX from Slides page to {dest}")
                return True
        except Exception as e:
            print(f"Direct export trigger on Slides page failed ({e}), trying context request...")

    try:
        resp = await context.request.get(export_pptx_url, timeout=30000)
        if resp.ok:
            body = await resp.body()
            temp_path = out_dir / f".tmp_{datetime.now().strftime('%H%M%S_%f')}.pptx"
            temp_path.write_bytes(body)
            if is_allowed_doc(temp_path, require_pptx=True) and has_valid_file_header(temp_path):
                temp_path.replace(dest)
                print(f"Downloaded PPTX via export URL to {dest}")
                return True
            temp_path.unlink(missing_ok=True)
    except Exception as e:
        print(f"Export URL request failed: {e}")

    return False


async def obtain_presentation(context, page, latest_response) -> list[Path]:
    direct_files = await try_direct_download(page, latest_response)
    if direct_files:
        return direct_files

    before_pages = set(context.pages)
    slides_page = None
    slides_url = None

    existing_hrefs = await latest_response.locator("a[href]").evaluate_all(
        "els => els.map(a => a.href)"
    )
    existing_slide_url = next((h for h in existing_hrefs if extract_slides_id(h)), None)

    if existing_slide_url:
        slides_url = existing_slide_url
        slides_page = await context.new_page()
        await slides_page.goto(slides_url, wait_until="domcontentloaded", timeout=25000)
    else:
        export_btn = latest_response.get_by_role("button", name=re.compile(r"export", re.I)).first
        if await export_btn.count() == 0:
            export_btn = page.get_by_role("button", name=re.compile(r"export", re.I)).last

        if await export_btn.count() > 0 and await export_btn.is_visible():
            await export_btn.click()
            await asyncio.sleep(0.6)

            menu_items = page.locator(
                '[role="menuitem"], [role="option"], .cdk-overlay-pane button, .cdk-overlay-pane a'
            )
            slides_option = menu_items.filter(has_text=re.compile(r"slides", re.I)).first
            download_option = menu_items.filter(
                has_text=re.compile(r"(download|powerpoint|\.pptx?)", re.I)
            ).first

            if await slides_option.count() > 0 and await slides_option.is_visible():
                await slides_option.click()

                open_slides_btn = page.locator(
                    '.snackbar-message button:has-text("Open Slides"), '
                    'button:has(.button-label:text-is("Open Slides")), '
                    'button:has-text("Open Slides")'
                ).first

                try:
                    await open_slides_btn.wait_for(state="visible", timeout=20000)
                    new_tab_task = asyncio.create_task(context.wait_for_event("page", timeout=20000))
                    await open_slides_btn.click()
                    print('Clicked "Open Slides" on snackbar.')

                    try:
                        slides_page = await new_tab_task
                    except Exception:
                        slides_page = page

                    for _ in range(40):
                        candidates = [slides_page] if slides_page and not slides_page.is_closed() else []
                        for p in context.pages:
                            if p not in before_pages and p not in candidates and not p.is_closed():
                                candidates.append(p)
                        if page not in candidates and not page.is_closed():
                            candidates.append(page)

                        for tab in candidates:
                            try:
                                u = tab.url or ""
                            except Exception:
                                u = ""
                            if extract_slides_id(u):
                                slides_page = tab
                                slides_url = u
                                break
                        if slides_url:
                            break
                        await asyncio.sleep(0.5)

                    if slides_page and not slides_url and not slides_page.is_closed():
                        try:
                            await slides_page.wait_for_url(
                                re.compile(r"docs\.google\.com/presentation/d/"),
                                timeout=15000,
                            )
                            slides_url = slides_page.url
                        except Exception:
                            pass
                except Exception as e:
                    print(f'Snackbar "Open Slides" button did not appear or click failed: {e}')

            elif await download_option.count() > 0 and await download_option.is_visible():
                print('No "Slides" option found in Export menu; clicking "Download" option...')
                try:
                    async with page.expect_download(timeout=30000) as dl_info:
                        await download_option.click()
                    download = await dl_info.value
                    saved_path = await save_if_allowed_download(
                        download,
                        latest_response,
                        "gemini_presentation.pptx",
                        require_pptx=True,
                    )
                    if saved_path is not None:
                        print(f"Downloaded valid PowerPoint file via Export -> Download: {saved_path}")
                        return [saved_path]
                    else:
                        print("Export -> Download did not produce a valid PowerPoint (.ppt/.pptx) file.")
                except Exception as e:
                    print(f"Export -> Download failed: {e}")
            else:
                await page.keyboard.press("Escape")

    if slides_url:
        print("Visited / captured Google Slides URL:", slides_url)
        dest = build_dated_dest(".pptx")
        ok = await download_pptx_from_slides_page(context, slides_page, slides_url, dest)
        if ok:
            if slides_page is not None and slides_page != page and not slides_page.is_closed():
                try:
                    await slides_page.close()
                except Exception:
                    pass
            return [dest]

    return []


async def generate_presentation(initial_prompt: str) -> Path | None:
    """Generate a PowerPoint for initial_prompt and return its saved path."""
    cdp_url = await ensure_cdp_chrome(9222)
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0]
        page = await context.new_page()
        await page.goto("https://gemini.google.com", wait_until="domcontentloaded")
        print("Opened:", page.url)

        response_candidates = page.locator(
            "model-response, message-content, .model-response-text, .response-container"
        )

        downloaded_files: list[Path] = []
        attempt = 0

        while not downloaded_files and attempt < max_attempts:
            attempt += 1
            current_prompt = initial_prompt if attempt == 1 else retry_prompt
            print(f"\n--- Attempt {attempt}/{max_attempts}: {current_prompt} ---")

            await send_prompt(page, current_prompt)
            response_text = await wait_for_response_done(
                page, response_candidates, timeout_seconds=wait_seconds
            )
            print("\n--- GEMINI RESPONSE ---\n")
            print(response_text)

            latest_response = response_candidates.last
            downloaded_files = await obtain_presentation(context, page, latest_response)

            if not downloaded_files:
                print("\nNo valid presentation/PDF obtained yet; sending retry prompt...")

        if downloaded_files:
            print("\nDone! Saved files in", out_dir, ":", [str(p) for p in downloaded_files])
            return downloaded_files[-1]
        else:
            print(f"\nReached max_attempts ({max_attempts}) without saving a valid PowerPoint.")
            return None
    finally:
        await playwright.stop()


if __name__ == "__main__":
    asyncio.run(generate_presentation(initial_prompt))
