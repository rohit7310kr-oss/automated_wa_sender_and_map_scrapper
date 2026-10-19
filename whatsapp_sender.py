import hashlib
import os
import random
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# =====================================================================
# CONFIG: everything you normally change lives here
# =====================================================================
CSV_FILE = "collected-data/data.csv"   # contacts file (needs a "Phone Number" column)

IMAGE_USED = False        # False -> text-only messages, no image is attached
IMAGE_URL = ""           # a web link (https://...) OR a local path like "banner.jpg"

# Placeholders you can use: {name} {phone} {address} {city} {business_type}
# Use \n for a new line.
TEXT_TEMPLATE = "Hello {name} Sir, I\'m Sanjay, Founder of Soseeks India Private Limited. \n We\'ve built *Chat2Bill*, a smart billing and GST solution for retail businesses such as grocery stores, supermarkets, electronics shops, pharmacies, and jewellery stores.\n I'd like to explore whether Chat2Bill could be useful for your retail clients and discuss potential collaboration opportunities.\n Would you be available for a quick 10–15 minute discussion? I can share a brief demo if you\'re interested.\n Thank you!"

DELAY_MINUTES = 0       # wait between messages
DELAY_JITTER_SECONDS = 15  # +/- random seconds added to the delay (set 0 for exactly 4:00)
LIMIT = 25               # max messages sent per run

SESSION_DIR = "./whatsapp_session"
DEFAULT_COUNTRY_CODE = "91"
IMAGE_CACHE_DIR = Path("./.image_cache")

# Statuses that mean "do not process this row again"
DONE_STATUSES = ("sent", "invalid", "no_phone", "duplicate")

# Selectors (WhatsApp changes these often; several fallbacks are given)
MAIN_INPUT = 'footer div[contenteditable="true"]'
ATTACH_BTN = ('button[aria-label="Attach"], div[aria-label="Attach"], '
              'span[data-icon="plus"], span[data-icon="plus-rounded"], '
              'span[data-icon="attach-menu-plus"]')
PHOTO_ITEM = ('li:has-text("Photos"), div[role="button"]:has-text("Photos"), '
              'button:has-text("Photos")')
PREVIEW_SEND = ('[aria-label="Send"], [data-icon*="send"], '
                'div[role="dialog"] [role="button"]:has([data-icon*="send"])')
INVALID_POPUP = 'div[role="dialog"]:has-text("invalid"), div[role="dialog"]:has-text("not on WhatsApp")'


# =====================================================================
# HELPERS
# =====================================================================
class SafeDict(dict):
    """Unknown {placeholders} stay as-is instead of crashing."""
    def __missing__(self, key):
        return "{" + key + "}"


def clean_phone(raw):
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) == 10:
        digits = DEFAULT_COUNTRY_CODE + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = DEFAULT_COUNTRY_CODE + digits[1:]
    elif len(digits) == 12 and digits.startswith(DEFAULT_COUNTRY_CODE):
        pass
    else:
        return None
    return digits


def value_or_default(row, column, default=""):
    val = str(row.get(column, "")).strip()
    return default if val in ("", "N/A", "nan") else val


def build_message(row, phone):
    name = value_or_default(row, "Business Name", "Sir/Ma'am")
    values = SafeDict(
        name=name,
        phone=phone,
        address=value_or_default(row, "Address"),
        city=value_or_default(row, "City"),
        business_type=value_or_default(row, "Business Type"),
    )
    try:
        return TEXT_TEMPLATE.format_map(values)
    except (ValueError, IndexError):
        # stray "{" or "}" in the template: send it unchanged rather than crash
        return TEXT_TEMPLATE


def resolve_image():
    """Return a local image path, downloading IMAGE_URL first if it is a web link."""
    if not IMAGE_URL:
        raise SystemExit("IMAGE_USED is True but IMAGE_URL is empty.")

    if IMAGE_URL.lower().startswith(("http://", "https://")):
        IMAGE_CACHE_DIR.mkdir(exist_ok=True)
        ext = Path(urlparse(IMAGE_URL).path).suffix or ".jpg"
        local = IMAGE_CACHE_DIR / (hashlib.md5(IMAGE_URL.encode()).hexdigest() + ext)
        if not local.exists():
            print("Downloading image...")
            req = urllib.request.Request(IMAGE_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                local.write_bytes(resp.read())
        return str(local.resolve())

    if not os.path.exists(IMAGE_URL):
        raise SystemExit(f"Image '{IMAGE_URL}' not found.")
    return os.path.abspath(IMAGE_URL)


def save_csv(df):
    """Write to a temp file then swap, so a crash never leaves a broken CSV."""
    tmp = CSV_FILE + ".tmp"
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, CSV_FILE)


def wait_between_messages():
    seconds = DELAY_MINUTES * 60 + random.uniform(-DELAY_JITTER_SECONDS, DELAY_JITTER_SECONDS)
    seconds = max(0, seconds)
    print(f"  Waiting {seconds / 60:.1f} min before the next message...\n")
    time.sleep(seconds)


# =====================================================================
# WHATSAPP ACTIONS
# =====================================================================
def open_chat(page, phone):
    """Open chat. Returns 'ok', 'invalid' or 'timeout'."""
    page.goto(f"https://web.whatsapp.com/send?phone={phone}")
    deadline = time.time() + 30
    while time.time() < deadline:
        if page.locator(MAIN_INPUT).count() > 0:
            return "ok"
        if page.locator(INVALID_POPUP).count() > 0:
            try:
                page.locator('div[role="dialog"] button').first.click()
            except Exception:
                pass
            return "invalid"
        time.sleep(0.5)
    return "timeout"


def type_message(page, text):
    """Type text into the focused box. New lines use Shift+Enter (plain Enter would send)."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line:
            page.keyboard.insert_text(line)
        if i < len(lines) - 1:
            page.keyboard.press("Shift+Enter")


def attach_image(page, image_path):
    page.locator(ATTACH_BTN).first.click()
    time.sleep(0.8)
    # Preferred: click "Photos & videos" and catch the file chooser
    try:
        with page.expect_file_chooser(timeout=5000) as fc:
            page.locator(PHOTO_ITEM).first.click()
        fc.value.set_files(image_path)
        return
    except Exception:
        pass
    # Fallback: image-specific hidden input
    page.locator('input[type="file"][accept*="image"]').first.set_input_files(image_path)


def send_text_only(page, text):
    box = page.locator(MAIN_INPUT).first
    box.click()
    type_message(page, text)
    time.sleep(0.8)
    page.keyboard.press("Enter")

    # Sent = the input box is empty again
    deadline = time.time() + 20
    while time.time() < deadline:
        box = page.locator(MAIN_INPUT).first
        if box.count() > 0 and box.is_visible() and box.inner_text().strip() == "":
            time.sleep(4)  # let the message leave (clock icon -> tick) before navigating away
            return
        time.sleep(0.5)
    raise Exception("Message not confirmed sent (input still not empty)")


def send_image_with_caption(page, image_path, caption):
    attach_image(page, image_path)

    # Wait for the preview: a send button AND the image preview must appear
    send_btn = page.locator(PREVIEW_SEND).last
    send_btn.wait_for(state="visible", timeout=25000)
    time.sleep(2.0)  # let the preview finish rendering

    # Caption box is auto-focused in the preview
    type_message(page, caption)
    time.sleep(0.8)
    page.keyboard.press("Enter")
    time.sleep(1.5)

    # If Enter did not send (preview still open), click the send button
    try:
        if page.locator('div[role="dialog"]').count() > 0 or \
                page.locator('[aria-label="Send"]').count() > 0:
            page.locator(PREVIEW_SEND).last.click(timeout=3000)
    except Exception:
        pass

    # Confirm the send: preview closed AND main chat input box is empty
    deadline = time.time() + 40
    confirmed = False
    while time.time() < deadline:
        preview_open = page.locator(PREVIEW_SEND).count() > 0 and \
            page.locator(PREVIEW_SEND).last.is_visible()
        if not preview_open:
            box = page.locator(MAIN_INPUT).first
            if box.count() > 0 and box.is_visible():
                if box.inner_text().strip() == "":
                    confirmed = True
                    break
        time.sleep(0.5)
    if not confirmed:
        raise Exception("Message not confirmed sent (preview open or input not empty)")

    # Let the upload finish (clock icon -> tick) before navigating away
    time.sleep(6)


# =====================================================================
# MAIN
# =====================================================================
def main():
    image_path = resolve_image() if IMAGE_USED else None

    # keep_default_na=False keeps "N/A" as text and empty cells as "" (no NaN surprises)
    df = pd.read_csv(CSV_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    if "Phone Number" not in df.columns:
        raise SystemExit(f"'{CSV_FILE}' has no 'Phone Number' column.")
    if "Status" not in df.columns:
        df["Status"] = ""

    # Mark rows that can never be sent, WITHOUT deleting them from the file
    seen_phones = set()
    for idx, row in df.iterrows():
        if row["Status"] in DONE_STATUSES:
            continue
        phone = clean_phone(row["Phone Number"])
        if not phone:
            df.at[idx, "Status"] = "no_phone"
        elif phone in seen_phones:
            df.at[idx, "Status"] = "duplicate"
        else:
            seen_phones.add(phone)
    save_csv(df)

    counts = df["Status"].replace("", "pending").value_counts().to_dict()
    print(f"Contacts in CSV: {len(df)} -> {counts}")

    pending = [i for i in df.index if df.at[i, "Status"] not in DONE_STATUSES]
    if not pending:
        print("Nothing to send: every contact is already done (sent / invalid / no phone / duplicate).\n"
              "To re-send, clear the Status column in the CSV.")
        return

    to_send = min(len(pending), LIMIT)
    print(f"Mode: {'image + text' if IMAGE_USED else 'text only'} | "
          f"will try up to {to_send} message(s) | delay {DELAY_MINUTES} min "
          f"(about {to_send * DELAY_MINUTES} min total)\n")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=SESSION_DIR,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 850},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("dialog", lambda d: d.accept())  # "Leave site?" prompts

        page.goto("https://web.whatsapp.com/")
        print("Waiting for WhatsApp Web (scan QR if asked, up to 3 min)...")
        try:
            page.wait_for_selector("#pane-side", timeout=180000)
        except PWTimeout:
            print("WhatsApp Web did not load.")
            ctx.close()
            return
        print("Logged in.\n")

        sent = 0
        for pos, idx in enumerate(pending):
            if sent >= LIMIT:
                print(f"Reached limit of {LIMIT}.")
                break

            row = df.loc[idx]
            name = value_or_default(row, "Business Name", "Sir/Ma'am")
            phone = clean_phone(row["Phone Number"])
            message = build_message(row, phone)

            print(f"[{sent + 1}/{to_send}] {name} ({phone})")
            attempted_send = False   # True once we actually tried to message them
            try:
                result = open_chat(page, phone)
                if result == "invalid":
                    print("  Not on WhatsApp, moving on.\n")
                    df.at[idx, "Status"] = "invalid"
                elif result == "timeout":
                    print("  Chat did not load, moving on.\n")
                    df.at[idx, "Status"] = "failed"
                else:
                    time.sleep(random.uniform(1.5, 3))
                    attempted_send = True
                    if IMAGE_USED:
                        send_image_with_caption(page, image_path, message)
                    else:
                        send_text_only(page, message)
                    df.at[idx, "Status"] = "sent"
                    sent += 1
                    print("  Sent.")
            except Exception as e:
                print(f"  Failed: {e}")
                df.at[idx, "Status"] = "failed"
                page.screenshot(path=f"error_{idx}.png")

            save_csv(df)  # save progress after every contact

            # Delay only after a real send attempt, and not after the last one
            is_last = pos == len(pending) - 1
            if attempted_send and not is_last and sent < LIMIT:
                wait_between_messages()
            else:
                print()

        print(f"Done. Sent {sent} messages.")
        ctx.close()


if __name__ == "__main__":
    main()