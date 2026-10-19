import os
import random
import time

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

CSV_FILE = "google_maps_contacts.csv"
IMAGE_PATH = "banner.jpg"
SESSION_DIR = "./whatsapp_session"
LIMIT = 25                   # max messages per run
DELAY_RANGE = (0, 0)       # random seconds between contacts (avoids ban)
DEFAULT_COUNTRY_CODE = "91"

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


def outgoing_count(page):
    """Number of outgoing (sent by me) messages currently in the open chat."""
    return page.locator('div.message-out, div[class*="message-out"]').count()


def send_image_with_caption(page, image_path, caption):
    attach_image(page, image_path)

    # Wait for the preview: a send button AND the image preview must appear
    send_btn = page.locator(PREVIEW_SEND).last
    send_btn.wait_for(state="visible", timeout=25000)
    time.sleep(2.0)  # let the preview finish rendering

    # Caption box is auto-focused in the preview
    page.keyboard.insert_text(caption)
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

    # Confirm the send. Icon names change between WhatsApp versions, so use
    # version-proof signals instead: the preview closes AND the number of
    # outgoing messages in the chat goes up.
    deadline = time.time() + 40
    confirmed = False
    while time.time() < deadline:
        preview_open = page.locator(PREVIEW_SEND).count() > 0 and \
            page.locator(PREVIEW_SEND).last.is_visible()
        if not preview_open:
            # Sent = preview closed and the main chat input box is empty
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


def main():
    if not os.path.exists(IMAGE_PATH):
        print(f"Image '{IMAGE_PATH}' not found.")
        return

    df = pd.read_csv(CSV_FILE, dtype=str)
    df = df.dropna(subset=["Phone Number"]).drop_duplicates(subset=["Phone Number"])
    if "Status" not in df.columns:
        df["Status"] = ""
    df["Status"] = df["Status"].fillna("")

    image_path = os.path.abspath(IMAGE_PATH)

    # Show what is in the CSV so a "0 sent" run is never a mystery
    counts = df["Status"].replace("", "pending").value_counts().to_dict()
    print(f"Contacts in CSV: {len(df)} -> {counts}")
    pending = (~df["Status"].isin(["sent", "invalid"])).sum()
    if pending == 0:
        print("Nothing to send: every contact is already marked 'sent' or "
              "'invalid' in the Status column.\n"
              "To re-send, clear the Status column (or delete it) in the CSV, "
              "or add new contacts.")
        return

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
        for idx, row in df.iterrows():
            if sent >= LIMIT:
                print(f"Reached limit of {LIMIT}.")
                break
            if row["Status"] in ("sent", "invalid"):
                continue  # resume support

            name = row.get("Business Name") or "Sir/Ma'am"
            phone = clean_phone(row["Phone Number"])
            if not phone:
                print(f"Skipping bad number for {name}: {row['Phone Number']}")
                df.at[idx, "Status"] = "invalid"
                df.to_csv(CSV_FILE, index=False)
                continue

            caption = (f"Hello {name}, hope you're doing well! "
                       f"I found your store on Google Maps and wanted to share "
                       f"this quick overview of our services.")

            print(f"[{sent + 1}/{LIMIT}] {name} ({phone})")
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
                    send_image_with_caption(page, image_path, caption)
                    df.at[idx, "Status"] = "sent"
                    sent += 1
                    print("  Sent.\n")
            except Exception as e:
                print(f"  Failed: {e}\n")
                df.at[idx, "Status"] = "failed"
                page.screenshot(path=f"error_{idx}.png")

            df.to_csv(CSV_FILE, index=False)  # save progress after every contact
            time.sleep(random.uniform(*DELAY_RANGE))

        print(f"Done. Sent {sent} messages.")
        ctx.close()


if __name__ == "__main__":
    main()