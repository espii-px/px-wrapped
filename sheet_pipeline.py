"""
Sheet Pipeline - Pull qualified deals + pipeline value from a client Google Sheet
=================================================================================
Reads a Google Sheet, finds columns containing "qualified" in the header,
and sums the deals count + deal value.

Usage:
    from sheet_pipeline import pull_pipeline_data
    data = pull_pipeline_data("https://docs.google.com/spreadsheets/d/SHEET_ID/edit")
"""

import re
import sys
import os
from pathlib import Path

# Import auth from creative-briefs
CREATIVE_BRIEFS_DIR = Path(__file__).parent / ".." / "creative-briefs"
sys.path.insert(0, str(CREATIVE_BRIEFS_DIR))


def _extract_sheet_id(url):
    """Extract the spreadsheet ID from a Google Sheets URL."""
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)
    # Maybe it's just the ID
    if re.match(r'^[a-zA-Z0-9_-]{20,}$', url):
        return url
    raise ValueError(f"Could not extract sheet ID from: {url}")


def pull_pipeline_data(sheet_url):
    """Pull qualified deals and pipeline value from a Google Sheet.

    Looks for columns with "qualified" in the header (case-insensitive).
    Looks for adjacent "value" / "amount" / "deal value" columns for deal amounts.

    Returns:
        {"qualified_deals": int, "pipeline_value": float}
    """
    from google_auth import get_sheets_service

    sheets = get_sheets_service()
    sheet_id = _extract_sheet_id(sheet_url)

    # Read all data from the first sheet
    result = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="A1:Z500",
    ).execute()

    rows = result.get("values", [])
    if not rows:
        return {"qualified_deals": 0, "pipeline_value": 0}

    headers = [h.lower().strip() for h in rows[0]]

    # Find qualified column(s)
    qualified_cols = []
    value_cols = []
    for i, h in enumerate(headers):
        if "qualified" in h:
            qualified_cols.append(i)
        if any(term in h for term in ["value", "amount", "revenue", "deal size", "deal value"]):
            value_cols.append(i)

    if not qualified_cols:
        print(f"  Warning: No 'qualified' column found. Headers: {headers}")
        return {"qualified_deals": 0, "pipeline_value": 0}

    # Count qualified deals and sum values
    qualified_deals = 0
    pipeline_value = 0.0

    for row in rows[1:]:
        for col in qualified_cols:
            if col < len(row):
                cell = row[col].strip()
                if not cell:
                    continue
                # Check if it's a number (count)
                clean = re.sub(r'[$,]', '', cell)
                try:
                    num = float(clean)
                    qualified_deals += int(num) if num == int(num) else 1
                except ValueError:
                    # Non-numeric qualified field - check if it's a yes/true/qualified status
                    if cell.lower() in ("yes", "true", "qualified", "1", "x"):
                        qualified_deals += 1

        for col in value_cols:
            if col < len(row):
                cell = row[col].strip()
                if not cell:
                    continue
                clean = re.sub(r'[$,]', '', cell)
                try:
                    pipeline_value += float(clean)
                except ValueError:
                    pass

    return {
        "qualified_deals": qualified_deals,
        "pipeline_value": pipeline_value,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python sheet_pipeline.py <google_sheet_url>")
        sys.exit(1)

    data = pull_pipeline_data(sys.argv[1])
    print(f"Qualified Deals: {data['qualified_deals']}")
    print(f"Pipeline Value: ${data['pipeline_value']:,.2f}")
