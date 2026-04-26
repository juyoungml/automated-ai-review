"""
Submit PDFs from review_pdf_files/ to paperreview.ai and save access tokens.

Usage:
  python submit_stanford.py <start> <end> <email>

Flow per paper:
  1. POST /api/get-upload-url  -> presigned S3 URL + s3_key
  2. POST <presigned_url>      -> upload PDF bytes directly to S3
  3. POST /api/confirm-upload  -> confirm upload, returns token
"""

import io
import json
import os
import re
import sys
from pathlib import Path

import requests
import time
from pypdf import PdfReader, PdfWriter

BASE_URL = "https://paperreview.ai"
PDF_DIR = "review_pdf_files"
TOKENS_DIR = Path("access_tokens/stanford")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


def truncate_pdf(pdf_bytes: bytes, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """Return pdf_bytes truncated to at most max_bytes by dropping trailing pages."""
    if len(pdf_bytes) <= max_bytes:
        return pdf_bytes

    print(f"PDF is {len(pdf_bytes)} bytes, truncating to 10MBs by dropping pages...")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)

    lo, hi, best = 1, total, None
    while lo <= hi:
        mid = (lo + hi) // 2
        writer = PdfWriter()
        for i in range(mid):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        if buf.tell() <= max_bytes:
            best = buf.getvalue()
            lo = mid + 1
        else:
            hi = mid - 1

    return best if best is not None else pdf_bytes


def get_pdf_files() -> dict[int, str]:
    pattern = re.compile(r"^paper(\d+)\.pdf$")
    result = {}
    for f in os.listdir(PDF_DIR):
        m = pattern.match(f)
        if m:
            result[int(m.group(1))] = os.path.join(PDF_DIR, f)
    return result


def submit_paper(pdf_path: str, email: str, venue: str = "") -> str:
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    pdf_bytes = truncate_pdf(pdf_bytes)
    filename = os.path.basename(pdf_path)

    payload = {"filename": filename}
    if venue:
        payload["venue"] = venue
    resp = requests.post(f"{BASE_URL}/api/get-upload-url", json=payload)
    resp.raise_for_status()
    data = resp.json()
    presigned_url = data["presigned_url"]
    presigned_fields = data["presigned_fields"]
    s3_key = data["s3_key"]

    form_data = {k: (None, v) for k, v in presigned_fields.items()}
    form_data["file"] = (filename, pdf_bytes, "application/pdf")
    put_resp = requests.post(presigned_url, files=form_data)
    put_resp.raise_for_status()

    confirm_resp = requests.post(
        f"{BASE_URL}/api/confirm-upload",
        data={"s3_key": s3_key, "email": email, "venue": venue or None},
    )
    confirm_resp.raise_for_status()
    return confirm_resp.json()["token"]


def load_tokens(tokens_file: str) -> dict:
    if os.path.exists(tokens_file):
        with open(tokens_file) as f:
            return json.load(f)
    return {}


def save_tokens(tokens: dict, tokens_file: str):
    with open(tokens_file, "w") as f:
        json.dump(tokens, f, indent=2)


if __name__ == "__main__":
    args = sys.argv[1:]

    if len(args) != 3:
        print("Usage: python submit_stanford.py <start> <end> <email>")
        sys.exit(1)

    start_idx = int(args[0])
    end_idx = int(args[1])
    user_email = args[2]

    all_pdfs = get_pdf_files()

    selected = [(idx, all_pdfs[idx]) for idx in range(start_idx, end_idx + 1) if idx in all_pdfs]
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    tokens_file = str(TOKENS_DIR / f"{start_idx}_{end_idx}.json")

    print(f"Submitting {len(selected)} paper(s)...")

    last_successful_idx = None

    tokens = load_tokens(tokens_file)
    for paper_idx, pdf_path in selected:
        filename = os.path.basename(pdf_path)
        try:
            token = submit_paper(pdf_path, user_email)
            tokens[filename] = token
            save_tokens(tokens, tokens_file)
            last_successful_idx = paper_idx
            print(f"[OK] {filename}: {token}")
            print(f"     {BASE_URL}/review?token={token}")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                print(f"[429] Rate limited on {filename}. Stopping.")
                if last_successful_idx is not None:
                    new_tokens_file = str(TOKENS_DIR / f"{start_idx}_{last_successful_idx}.json")
                    os.rename(tokens_file, new_tokens_file)
                    print(f"Tokens saved to {new_tokens_file}")
                else:
                    print("No successful submissions; no token file saved.")
                sys.exit(1)
            print(f"[FAIL] {filename}: {e}")
        # sleep 5 seconds between submissions to avoid overwhelming the server
        time.sleep(5)
    print(f"\nTokens saved to {tokens_file}")
