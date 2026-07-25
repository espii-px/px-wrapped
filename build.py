"""
PX Wrapped Builder
==================
Builds a client-facing "Wrapped" report from either:
  - A filled-out 90 Day Game Plan Google Doc (--gdoc URL)
  - A local gameplay text file (positional arg)
  - An edited data.json from a previous build (--from-data)

Usage:
    python3 build.py --gdoc "https://docs.google.com/document/d/DOC_ID/edit"
    python3 build.py plans/client-q2.txt
    python3 build.py --from-data builds/client-q2/data.json --no-pull

The script will:
  1. Parse the input with Claude to extract structured data
  2. Pull top 6 ads from Meta (thumbnails, post links, impressions, clicks)
  3. Generate the HTML from the template
  4. Output to builds/<client-slug>/
"""

import os
import sys
import json
import re
import shutil
import requests
from datetime import datetime
from dotenv import load_dotenv
from meta_api import pull_ad_data, get_ad_creative, get_video_url, get_ad_post_link, GRAPH_URL

load_dotenv()

TOKEN = os.getenv("META_ACCESS_TOKEN")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

QUARTER_DATES = {
    "Q1": ("01-01", "03-31", "January - March"),
    "Q2": ("04-01", "06-30", "April - June"),
    "Q3": ("07-01", "09-30", "July - September"),
    "Q4": ("10-01", "12-31", "October - December"),
}

NEXT_QUARTER = {"Q1": "Q2", "Q2": "Q3", "Q3": "Q4", "Q4": "Q1"}

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def resolve_dates(month=None, quarter=None, year=None, range_start=None, range_end=None):
    """Resolve date arguments into (start, end, label, period, next_period).

    Returns:
        start: "YYYY-MM-DD"
        end: "YYYY-MM-DD"
        label: "May 2026" or "April - June 2026"
        period: "May" or "Q2"
        next_period: "June" or "Q3"
    """
    import calendar

    if range_start and range_end:
        # Custom range
        s = datetime.strptime(range_start, "%Y-%m-%d")
        e = datetime.strptime(range_end, "%Y-%m-%d")
        label = f"{s.strftime('%b %d')} - {e.strftime('%b %d, %Y')}"
        period = f"{s.strftime('%B')}"
        next_period = f"{MONTH_NAMES[(s.month % 12) + 1]}"
        return range_start, range_end, label, period, next_period

    if month:
        # Monthly: "2026-05" or "2026-5"
        parts = month.split("-")
        y = int(parts[0])
        m = int(parts[1])
        last_day = calendar.monthrange(y, m)[1]
        start = f"{y}-{m:02d}-01"
        end = f"{y}-{m:02d}-{last_day}"
        label = f"{MONTH_NAMES[m]} {y}"
        period = MONTH_NAMES[m]
        next_m = (m % 12) + 1
        next_y = y + 1 if next_m == 1 else y
        next_period = MONTH_NAMES[next_m]
        return start, end, label, period, next_period

    if quarter:
        # Quarterly
        y = year or datetime.now().year
        q_start_md, q_end_md, q_label = QUARTER_DATES[quarter]
        start = f"{y}-{q_start_md}"
        end = f"{y}-{q_end_md}"
        label = f"{q_label} {y}"
        period = quarter
        next_period = NEXT_QUARTER[quarter]
        return start, end, label, period, next_period

    # Default: last month
    today = datetime.now()
    m = today.month - 1 if today.month > 1 else 12
    y = today.year if today.month > 1 else today.year - 1
    return resolve_dates(month=f"{y}-{m:02d}")


def fetch_gdoc(url):
    """Fetch a Google Doc's content as plain text via export URL.

    Works with docs shared as 'anyone with the link can view'.
    Falls back to the Google Drive API if the export URL fails.
    """
    # Extract doc ID from URL
    match = re.search(r'/document/d/([a-zA-Z0-9_-]+)', url)
    if not match:
        raise ValueError(f"Could not extract Google Doc ID from: {url}")
    doc_id = match.group(1)

    # Try public export first (works for publicly shared docs)
    export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    resp = requests.get(export_url, timeout=30)
    if resp.status_code == 200 and len(resp.text.strip()) > 50:
        return resp.text

    # Fall back to Google Drive API (requires oauth credentials)
    try:
        sys.path.insert(0, str(os.path.join(os.path.dirname(__file__), "..", "creative-briefs")))
        from google_auth import get_drive_service
        drive = get_drive_service()
        result = drive.files().export(fileId=doc_id, mimeType="text/plain").execute()
        return result.decode("utf-8") if isinstance(result, bytes) else result
    except Exception as e:
        raise RuntimeError(
            f"Could not fetch Google Doc. Make sure it's shared or OAuth is set up.\n"
            f"Export URL returned {resp.status_code}. Drive API error: {e}"
        )


def load_client_profiles():
    """Load client profiles for account ID / event config lookup."""
    profiles_path = os.path.join(os.path.dirname(__file__), "..", "creative-briefs", "client_profiles.json")
    if os.path.exists(profiles_path):
        with open(profiles_path) as f:
            return json.load(f)
    return {}


def lookup_client_config(client_name, profiles):
    """Find a client's Meta account ID and event config from profiles."""
    if not profiles or not client_name:
        return {}
    name_lower = client_name.lower().strip()
    for slug, profile in profiles.items():
        pname = profile.get("client_name", "").lower().strip()
        if name_lower == pname or name_lower in pname or pname in name_lower or slug == name_lower:
            return {
                "meta_account_id": profile.get("meta_account_id", ""),
                "campaign_filter": profile.get("campaign_filter", "PX"),
                "primary_event": profile.get("event_1", "lead"),
                "primary_event_label": profile.get("event_1_label", "Leads"),
            }
    return {}


def parse_scaling_plan(text):
    """Use Claude to extract structured data from a scaling plan doc."""
    # Load client profiles so Claude can reference known account IDs
    profiles = load_client_profiles()
    profile_hint = ""
    if profiles:
        client_list = {p.get("client_name", k): p.get("meta_account_id", "")
                       for k, p in profiles.items() if p.get("meta_account_id")}
        profile_hint = f"\n\nKNOWN CLIENTS (use these account IDs if the client name matches):\n{json.dumps(client_list, indent=2)}\n"

    prompt = f"""Extract the following from this scaling plan document. Return ONLY valid JSON, no markdown.
{profile_hint}
{{
  "client_name": "the client/brand name",
  "meta_account_id": "the Meta/Facebook ad account ID (just digits). Look up from KNOWN CLIENTS if not in the doc.",
  "campaign_filter": "campaign name filter if mentioned (e.g. PX), or empty string",
  "quarter": "which quarter is being recapped (Q1, Q2, Q3, or Q4)",
  "year": "the year (e.g. 2026). If not explicitly stated, default to {datetime.now().year}",
  "primary_event": "the primary conversion event type (lead, purchase, or a custom event string)",
  "primary_event_label": "what to call the primary event (e.g. Leads, Consults Booked)",
  "stats": {{
    "ads_ran": "number of ads ran",
    "campaigns": "number of campaigns",
    "impressions": "total impressions as a formatted string (e.g. 2.65M)",
    "reach": "people reached as formatted string (e.g. 467K)",
    "leads": "total leads/results as formatted string (e.g. 1,471)",
    "avg_cpl": "average cost per lead as formatted string (e.g. $89)",
    "video_views": "video views as formatted string (e.g. 584K)",
    "ads_color_text": "fun one-liner about the ads count",
    "impressions_color_text": "fun comparison for impressions (e.g. more than the population of X)",
    "reach_color_text": "fun one-liner about reach",
    "video_color_text": "one-liner about the video content themes that resonated"
  }},
  "what_learned": {{
    "old_approach": "what didn't work (short phrase)",
    "new_approach": "what replaced it (short phrase)",
    "detail": "1-2 sentence explanation"
  }},
  "next_quarter_playbook": [
    {{"icon": "emoji", "title": "short title", "description": "1 sentence"}},
    {{"icon": "emoji", "title": "short title", "description": "1 sentence"}},
    {{"icon": "emoji", "title": "short title", "description": "1 sentence"}},
    {{"icon": "emoji", "title": "short title", "description": "1 sentence"}}
  ],
  "verticals": [
    {{"icon": "emoji", "name": "vertical name"}},
    {{"icon": "emoji", "name": "vertical name"}},
    {{"icon": "emoji", "name": "vertical name"}}
  ],
  "verticals_sub": "subtitle for verticals slide",
  "verticals_footer": "footer text for verticals slide",
  "target": {{
    "number": "the big target number (e.g. $1M)",
    "subtitle": "what the number means (e.g. per month in funded deals)",
    "detail_html": "1-2 lines of detail with <strong> tags for emphasis",
    "moon_goal": "the stretch/moon goal if mentioned, or empty"
  }},
  "campaign_engine": {{
    "core_pct": 75,
    "core_label": "70-80%",
    "test_pct": 17,
    "test_label": "15-20%",
    "retarget_pct": 8,
    "retarget_label": "5-10%"
  }},
  "outro": {{
    "headline": "short punchy headline with <br> for line break",
    "sub": "1-2 sentences about what's next",
    "cta": "short call to action phrase"
  }}
}}

DOCUMENT:
{text}"""

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    data = resp.json()
    text_out = data["content"][0]["text"]

    # Extract JSON from response
    json_match = re.search(r'\{[\s\S]*\}', text_out)
    if json_match:
        return json.loads(json_match.group())
    raise ValueError("Could not parse Claude response as JSON")


def pull_account_totals(account_id, campaign_filter, start, end, client_key):
    """Pull account-level totals including purchases and revenue."""
    result = pull_ad_data(
        account_id=account_id,
        token=TOKEN,
        start=start,
        end=end,
        top_n=100,
        campaign_filter=campaign_filter,
        client_key=client_key,
    )

    if result.get("error"):
        print(f"  API Error: {result['error']}")
        return {}

    acct = result["account"]

    # Pull purchase data from the raw API (pull_ad_data already aggregates actions)
    # We need to re-query for purchase specifically
    url = f"{GRAPH_URL}/act_{account_id}/insights"
    params = {
        "access_token": TOKEN,
        "time_range": f'{{"since":"{start}","until":"{end}"}}',
        "fields": "spend,impressions,actions,action_values",
        "level": "account",
    }
    if campaign_filter:
        filters = [f.strip() for f in campaign_filter.split(",")]
        filter_rules = [{"field": "campaign.name", "operator": "CONTAIN", "value": f} for f in filters]
        import urllib.parse
        params["filtering"] = json.dumps(filter_rules)

    resp = requests.get(url, params=params, timeout=60)
    data = resp.json()

    purchases = 0
    revenue = 0.0

    for row in data.get("data", []):
        for a in row.get("actions", []):
            if a["action_type"] in ("purchase", "offsite_conversion.fb_pixel_purchase"):
                purchases += int(a["value"])
        for av in row.get("action_values", []):
            if av["action_type"] in ("purchase", "offsite_conversion.fb_pixel_purchase"):
                revenue += float(av["value"])

    total_spend = acct.get("spend", 0)
    roas = round(revenue / total_spend, 2) if total_spend > 0 and revenue > 0 else 0

    return {
        **acct,
        "purchases": purchases,
        "revenue": revenue,
        "roas": roas,
    }


def pull_creatives(account_id, campaign_filter, start, end, client_key, output_dir):
    """Pull top 6 ads from Meta, download thumbnails, get post links + stats."""
    print(f"  Pulling top ads from Meta account {account_id}...")

    result = pull_ad_data(
        account_id=account_id,
        token=TOKEN,
        start=start,
        end=end,
        top_n=6,
        campaign_filter=campaign_filter,
        client_key=client_key,
    )

    if result.get("error"):
        print(f"  API Error: {result['error']}")
        return []

    ads = result["ads"]
    print(f"  Got {len(ads)} top ads")
    creatives = []

    for i, ad in enumerate(ads):
        ad_id = ad["ad_id"]
        spend = ad["spend"]

        # Get creative details
        creative = get_ad_creative(ad_id, TOKEN)

        # Get ad type
        url = f"{GRAPH_URL}/{ad_id}/adcreatives"
        params = {"access_token": TOKEN, "fields": "video_id,object_story_spec"}
        r = requests.get(url, params=params, timeout=30)
        is_video = False
        for c in r.json().get("data", []):
            if c.get("video_id") or c.get("object_story_spec", {}).get("video_data", {}).get("video_id"):
                is_video = True
                break

        # Get thumbnail
        thumb_url = ""
        t_url = f"{GRAPH_URL}/{ad_id}/adcreatives"
        t_params = {"access_token": TOKEN, "fields": "thumbnail_url,image_url,object_story_spec"}
        t_resp = requests.get(t_url, params=t_params, timeout=30)
        for c in t_resp.json().get("data", []):
            thumb_url = c.get("thumbnail_url") or c.get("image_url") or ""
            if not thumb_url:
                oss = c.get("object_story_spec", {})
                thumb_url = oss.get("link_data", {}).get("picture", "")
            if thumb_url:
                break

        # Download thumbnail
        thumb_path = ""
        if thumb_url:
            thumb_path = f"assets/ad_{i+1}_thumb.jpg"
            full_path = os.path.join(output_dir, thumb_path)
            try:
                resp = requests.get(thumb_url, timeout=60, stream=True)
                if resp.status_code == 200:
                    with open(full_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            f.write(chunk)
            except Exception:
                thumb_path = ""

        # Get post link
        post_link = get_ad_post_link(ad_id, TOKEN)

        # Get impressions/clicks
        ins_url = f"{GRAPH_URL}/{ad_id}/insights"
        ins_params = {
            "access_token": TOKEN,
            "time_range": f'{{"since":"{start}","until":"{end}"}}',
            "fields": "impressions,clicks",
        }
        ins_resp = requests.get(ins_url, params=ins_params, timeout=30)
        imps = 0
        clicks = 0
        for row in ins_resp.json().get("data", []):
            imps = int(row.get("impressions", 0))
            clicks = int(row.get("clicks", 0))

        print(f"  [{i+1}] {ad['ad_name'][:50]} | ${spend:,.0f} | {imps:,} imps | {clicks:,} clicks")

        creatives.append({
            "rank": i + 1,
            "headline": creative["headline"] or ad["ad_name"],
            "spend": spend,
            "impressions": imps,
            "clicks": clicks,
            "is_video": is_video,
            "thumbnail": thumb_path,
            "post_link": post_link,
        })

    return creatives


def format_number_short(n):
    """Format a number like 132178 to 132K."""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n/1_000:.0f}K"
    return f"{n:,}"


def build_creative_cards(creatives):
    """Generate HTML for creative carousel cards."""
    cards = []
    for c in creatives:
        play_icon = '<div class="play-icon"></div>' if c["is_video"] else ""
        spend_short = f"${c['spend']/1000:.1f}K" if c["spend"] >= 1000 else f"${c['spend']:,.0f}"
        imps_short = format_number_short(c["impressions"])
        clicks_fmt = f"{c['clicks']:,}"

        card = f"""      <a class="creative-card" href="{c['post_link']}" target="_blank" rel="noopener">
        <div class="rank-badge">#{c['rank']}</div>
        {play_icon}
        <img class="thumb" src="{c['thumbnail']}" alt="Ad {c['rank']}">
        <div class="card-info">
          <div class="card-headline">{c['headline']}</div>
          <div class="card-stats">
            <div class="card-stat"><div class="csv">{spend_short}</div><div class="csl">Spend</div></div>
            <div class="card-stat"><div class="csv">{imps_short}</div><div class="csl">Impressions</div></div>
            <div class="card-stat"><div class="csv">{clicks_fmt}</div><div class="csl">Clicks</div></div>
          </div>
        </div>
      </a>"""
        cards.append(card)

    return "\n".join(cards)


def build_playbook_cards(playbook):
    """Generate HTML for playbook goal cards."""
    cards = []
    for i, item in enumerate(playbook):
        delay = f"delay-{i+1}"
        card = f"""      <div class="goal-card animate-in {delay}">
        <div class="goal-icon">{item['icon']}</div>
        <h3>{item['title']}</h3>
        <p>{item['description']}</p>
      </div>"""
        cards.append(card)
    return "\n".join(cards)


def build_vertical_pills(verticals):
    """Generate HTML for vertical pills."""
    pills = []
    for i, v in enumerate(verticals):
        delay = f"delay-{i+2}"
        pill = f"""      <div class="vertical-pill animate-in {delay}">
        <span class="v-icon">{v['icon']}</span> {v['name']}
      </div>"""
        pills.append(pill)
    return "\n".join(pills)


def build_wrapped(plan_path=None, month=None, quarter=None, year=None,
                   range_start=None, range_end=None, sheet_url=None,
                   from_data=None, no_pull=False, gdoc_url=None):
    """Main build function.

    Args:
        plan_path: Path to gameplay text file
        month: "YYYY-MM" for monthly wraps
        quarter: "Q1"-"Q4" for quarterly wraps
        year: Year (int or str)
        range_start/range_end: Custom date range "YYYY-MM-DD"
        sheet_url: Google Sheet URL for pipeline data
        from_data: Path to a data.json to rebuild from (skip Claude parsing)
        no_pull: If True, skip Meta API pulls and reuse existing creatives/thumbnails
        gdoc_url: Google Doc URL for the 90 Day Game Plan
    """
    # -----------------------------------------------------------
    # Step 1: Get structured data
    # -----------------------------------------------------------
    if from_data:
        print(f"Rebuilding from data file: {from_data}")
        with open(from_data) as f:
            data = json.load(f)
    elif gdoc_url:
        print(f"Fetching 90 Day Game Plan from Google Doc...")
        plan_text = fetch_gdoc(gdoc_url)
        print(f"  Got {len(plan_text)} chars. Parsing with Claude...")
        data = parse_scaling_plan(plan_text)
    elif plan_path:
        print(f"Reading gameplay: {plan_path}")
        with open(plan_path) as f:
            plan_text = f.read()
        print("Parsing with Claude...")
        data = parse_scaling_plan(plan_text)
    else:
        raise ValueError("Provide a plan file, --gdoc URL, or --from-data path")

    client_name = data["client_name"]
    client_slug = re.sub(r'[^a-z0-9]+', '-', client_name.lower()).strip('-')
    account_id = data.get("meta_account_id", "")
    campaign_filter = data.get("campaign_filter", "PX")

    # If account ID is missing or placeholder, look it up from client profiles
    if not account_id or account_id.startswith("X") or len(account_id) < 5:
        profiles = load_client_profiles()
        config = lookup_client_config(client_name, profiles)
        if config.get("meta_account_id"):
            account_id = config["meta_account_id"]
            data["meta_account_id"] = account_id
            # Also pull event config if not already set
            if not data.get("primary_event") or data["primary_event"] == "lead":
                data["primary_event"] = config.get("primary_event", "lead")
                data["primary_event_label"] = config.get("primary_event_label", "Leads")
            if not campaign_filter or campaign_filter == "PX":
                campaign_filter = config.get("campaign_filter", "PX")
                data["campaign_filter"] = campaign_filter
            print(f"  Matched client '{client_name}' to account {account_id} from profiles")
        else:
            print(f"  WARNING: No Meta account ID found for '{client_name}'.")
            print(f"  Add it to client_profiles.json or include it in the game plan doc.")

    # Resolve dates - CLI args override data
    if month or range_start or (quarter and quarter != data.get("quarter")):
        start_date, end_date, date_range, period, next_period = resolve_dates(
            month=month, quarter=quarter,
            year=int(year) if year else None,
            range_start=range_start, range_end=range_end,
        )
    elif from_data and data.get("_meta", {}).get("start_date"):
        # Reuse dates from the saved data
        start_date = data["_meta"]["start_date"]
        end_date = data["_meta"]["end_date"]
        period = data["_meta"]["period"]
        next_period = NEXT_QUARTER.get(period, MONTH_NAMES[(MONTH_NAMES.index(period) % 12) + 1] if period in MONTH_NAMES else "")
        y = int(data.get("year", datetime.now().year))
        date_range = f"{period} {y}" if len(period) <= 2 else period
        # Try to rebuild a proper date_range label
        if period in QUARTER_DATES:
            _, _, date_range = QUARTER_DATES[period]
            date_range = f"{date_range} {y}"
    elif data.get("quarter"):
        q = data["quarter"]
        y = int(data.get("year", datetime.now().year))
        start_date, end_date, date_range, period, next_period = resolve_dates(quarter=q, year=y)
    else:
        start_date, end_date, date_range, period, next_period = resolve_dates(month=month)

    print(f"  Client: {client_name}")
    print(f"  Period: {period} ({start_date} to {end_date})")
    print(f"  Meta Account: {account_id}")

    # Step 2: Set up output directory
    period_slug = period.lower().replace(" ", "-")
    output_dir = os.path.join("builds", f"{client_slug}-{period_slug}")
    assets_dir = os.path.join(output_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    # -----------------------------------------------------------
    # Step 3: Pull data from Meta (or reuse existing)
    # -----------------------------------------------------------
    primary_event = data.get("primary_event", "lead")
    client_key = {
        "primary_event": primary_event,
        "primary_event_label": data.get("primary_event_label", "Leads"),
        "cost_label": f"Cost per {data.get('primary_event_label', 'Lead')}",
    }

    if no_pull:
        # Reuse saved meta data from data.json
        meta_saved = data.get("_meta", {})
        purchases = meta_saved.get("purchases", 0)
        revenue = meta_saved.get("revenue", 0)
        roas = meta_saved.get("roas", 0)
        # Reuse existing creatives from data.json
        creatives = data.get("_creatives", [])
        print(f"  Skipping Meta pull. Using {len(creatives)} saved creatives.")
    else:
        # Pull account totals (including purchases/revenue)
        print("  Pulling account totals...")
        account_totals = pull_account_totals(account_id, campaign_filter, start_date, end_date, client_key)
        purchases = account_totals.get("purchases", 0)
        revenue = account_totals.get("revenue", 0)
        roas = account_totals.get("roas", 0)

        if purchases > 0:
            print(f"  Purchases: {purchases} | Revenue: ${revenue:,.2f} | ROAS: {roas}x")

        # Pull top creatives
        creatives = pull_creatives(account_id, campaign_filter, start_date, end_date, client_key, output_dir)

    # Step 4: Pull pipeline data from Google Sheet
    pipeline_deals = 0
    pipeline_value = 0
    if sheet_url:
        print(f"  Pulling pipeline data from sheet...")
        try:
            from sheet_pipeline import pull_pipeline_data
            pipeline = pull_pipeline_data(sheet_url)
            pipeline_deals = pipeline.get("qualified_deals", 0)
            pipeline_value = pipeline.get("pipeline_value", 0)
            print(f"  Pipeline: {pipeline_deals} qualified deals | ${pipeline_value:,.0f} value")
        except Exception as e:
            print(f"  Pipeline ERROR: {e}")
    elif no_pull:
        pipeline_deals = data.get("_meta", {}).get("pipeline_deals", 0)
        pipeline_value = data.get("_meta", {}).get("pipeline_value", 0)

    # Step 5: Build HTML from template
    print("Building HTML...")
    with open("template.html") as f:
        template = f.read()

    stats = data["stats"]
    learned = data["what_learned"]
    target = data["target"]
    engine = data["campaign_engine"]
    outro = data["outro"]

    # Determine slide count
    total_slides = 13
    has_purchases = purchases > 0
    has_pipeline = pipeline_deals > 0
    if has_purchases:
        total_slides += 1
    if has_pipeline:
        total_slides += 1

    replacements = {
        "{{CLIENT_NAME}}": client_name,
        "{{CLIENT_NAME_UPPER}}": client_name.upper(),
        "{{QUARTER}}": period,
        "{{NEXT_QUARTER}}": next_period,
        "{{DATE_RANGE}}": date_range,
        "{{TOTAL_SLIDES}}": str(total_slides),
        "{{ADS_RAN}}": str(stats["ads_ran"]),
        "{{CAMPAIGNS}}": str(stats["campaigns"]),
        "{{ADS_COLOR_TEXT}}": stats["ads_color_text"],
        "{{IMPRESSIONS}}": stats["impressions"],
        "{{IMPRESSIONS_COLOR_TEXT}}": stats["impressions_color_text"],
        "{{REACH}}": stats["reach"],
        "{{REACH_COLOR_TEXT}}": stats["reach_color_text"],
        "{{LEADS}}": stats["leads"],
        "{{LEADS_LABEL}}": data.get("primary_event_label", "leads").lower(),
        "{{AVG_CPL}}": stats["avg_cpl"],
        "{{VIDEO_VIEWS}}": stats["video_views"],
        "{{VIDEO_COLOR_TEXT}}": stats["video_color_text"],
        "{{LEARNED_OLD}}": learned["old_approach"],
        "{{LEARNED_NEW}}": learned["new_approach"],
        "{{LEARNED_DETAIL}}": learned["detail"],
        "{{TARGET_NUMBER}}": target["number"],
        "{{TARGET_SUBTITLE}}": target["subtitle"],
        "{{TARGET_DETAIL}}": target["detail_html"],
        "{{TARGET_MOON}}": f'Moon goal: <strong style="color: #f9ca24;">{target["moon_goal"]}</strong>' if target.get("moon_goal") else "",
        "{{ENGINE_CORE_PCT}}": str(engine["core_pct"]),
        "{{ENGINE_CORE_LABEL}}": engine["core_label"],
        "{{ENGINE_TEST_PCT}}": str(engine["test_pct"]),
        "{{ENGINE_TEST_LABEL}}": engine["test_label"],
        "{{ENGINE_RETARGET_PCT}}": str(engine["retarget_pct"]),
        "{{ENGINE_RETARGET_LABEL}}": engine["retarget_label"],
        "{{OUTRO_HEADLINE}}": outro["headline"],
        "{{OUTRO_SUB}}": outro["sub"],
        "{{OUTRO_CTA}}": outro["cta"],
        "{{VERTICALS_SUB}}": data.get("verticals_sub", "Testing, validating, scaling."),
        "{{VERTICALS_FOOTER}}": data.get("verticals_footer", "Validate or deprioritize. No wasted spend."),
        "{{CREATIVE_CARDS}}": build_creative_cards(creatives),
        "{{PLAYBOOK_CARDS}}": build_playbook_cards(data["next_quarter_playbook"]),
        "{{VERTICAL_PILLS}}": build_vertical_pills(data["verticals"]),
        # Purchase data
        "{{PURCHASES}}": f"{purchases:,}" if has_purchases else "0",
        "{{REVENUE}}": f"${revenue:,.0f}" if revenue >= 1000 else f"${revenue:,.2f}",
        "{{ROAS}}": f"{roas}x",
        "{{PURCHASES_SLIDE}}": _build_purchases_slide(purchases, revenue, roas) if has_purchases else "",
        # Pipeline data
        "{{PIPELINE_SLIDE}}": _build_pipeline_slide(pipeline_deals, pipeline_value) if has_pipeline else "",
    }

    html = template
    for key, value in replacements.items():
        html = html.replace(key, value)

    output_path = os.path.join(output_dir, "index.html")
    with open(output_path, "w") as f:
        f.write(html)

    # Save the parsed data for reference (and for --from-data rebuilds)
    data["_meta"] = {
        "start_date": start_date,
        "end_date": end_date,
        "period": period,
        "purchases": purchases,
        "revenue": revenue,
        "roas": roas,
        "pipeline_deals": pipeline_deals,
        "pipeline_value": pipeline_value,
    }
    data["_creatives"] = creatives
    with open(os.path.join(output_dir, "data.json"), "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nDone! Output: {output_dir}/")
    print(f"  index.html  - the wrapped page")
    print(f"  assets/     - ad thumbnails")
    print(f"  data.json   - editable data (edit this, then rebuild with --from-data)")
    print(f"\nOpen with: open {output_path}")
    print(f"\nTo edit and rebuild:")
    print(f"  1. Edit {output_dir}/data.json")
    print(f"  2. python3 build.py --from-data {output_dir}/data.json --no-pull")


def _build_purchases_slide(purchases, revenue, roas):
    """Generate the purchases/revenue slide HTML."""
    rev_fmt = f"${revenue/1_000_000:.1f}M" if revenue >= 1_000_000 else (
        f"${revenue/1_000:.0f}K" if revenue >= 1_000 else f"${revenue:,.0f}"
    )
    return f"""
  <div class="slide" id="slide-purchases">
    <p class="big-sublabel animate-in" style="color: var(--gray); margin-bottom: 8px;">Purchases tracked from ads</p>
    <h1 class="big-number animate-in" style="background: linear-gradient(135deg, #2ecc71, #27ae60); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-size: clamp(60px, 15vw, 120px);">{purchases:,}</h1>
    <p class="big-sublabel animate-in delay-1" style="color: var(--gray); margin-top: 12px;">purchases</p>
    <div class="stat-row animate-in delay-2" style="margin-top: 32px;">
      <div class="stat-item"><div class="stat-value" style="color: #2ecc71;">{rev_fmt}</div><div class="stat-label">Revenue</div></div>
      <div class="stat-item"><div class="stat-value" style="color: #2ecc71;">{roas}x</div><div class="stat-label">ROAS</div></div>
    </div>
  </div>"""


def _build_pipeline_slide(deals, value):
    """Generate the pipeline/opportunity slide HTML."""
    val_fmt = f"${value/1_000_000:.1f}M" if value >= 1_000_000 else (
        f"${value/1_000:.0f}K" if value >= 1_000 else f"${value:,.0f}"
    )
    return f"""
  <div class="slide" id="slide-pipeline">
    <p class="big-sublabel animate-in" style="color: var(--gray); margin-bottom: 8px;">Pipeline generated</p>
    <h1 class="big-number animate-in" style="background: linear-gradient(135deg, #f39c12, #e67e22); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-size: clamp(60px, 15vw, 120px);">{val_fmt}</h1>
    <p class="big-sublabel animate-in delay-1" style="color: var(--gray); margin-top: 12px;">in qualified pipeline</p>
    <div class="stat-row animate-in delay-2" style="margin-top: 32px;">
      <div class="stat-item"><div class="stat-value" style="color: #f39c12;">{deals}</div><div class="stat-label">Qualified Deals</div></div>
      <div class="stat-item"><div class="stat-value" style="color: #f39c12;">{val_fmt}</div><div class="stat-label">Deal Value</div></div>
    </div>
  </div>"""


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="PX Wrapped Builder",
        epilog="""
Examples:
  # Build from the CSM's 90 Day Game Plan Google Doc:
  python3 build.py --gdoc "https://docs.google.com/document/d/DOC_ID/edit"

  # Build from a local gameplay file:
  python3 build.py plans/client-q2.txt

  # Build for a specific month:
  python3 build.py --gdoc "..." --month 2026-06

  # Edit the output, then rebuild without re-pulling:
  python3 build.py --from-data builds/client-q2/data.json --no-pull

  # Re-pull Meta data but keep your text edits:
  python3 build.py --from-data builds/client-q2/data.json
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("plan", nargs="?", default=None,
                        help="Path to gameplay text file (not needed with --gdoc or --from-data)")
    parser.add_argument("--gdoc", dest="gdoc_url", metavar="URL",
                        help="Google Doc URL for the 90 Day Game Plan")
    parser.add_argument("--from-data", dest="from_data", metavar="DATA_JSON",
                        help="Rebuild from an edited data.json (skip Claude parsing)")
    parser.add_argument("--no-pull", dest="no_pull", action="store_true",
                        help="Skip Meta API pulls, reuse saved creatives/thumbnails")
    parser.add_argument("--month", help="Monthly wrap: YYYY-MM (e.g. 2026-05)")
    parser.add_argument("--quarter", help="Quarterly wrap: Q1, Q2, Q3, Q4")
    parser.add_argument("--year", help="Year (default: current year)")
    parser.add_argument("--range", nargs=2, metavar=("START", "END"),
                        help="Custom date range: YYYY-MM-DD YYYY-MM-DD")
    parser.add_argument("--sheet", help="Google Sheet URL for pipeline data")
    args = parser.parse_args()

    if not args.plan and not args.from_data and not args.gdoc_url:
        parser.error("Provide a gameplay file, --gdoc URL, or --from-data path")

    build_wrapped(
        plan_path=args.plan,
        month=args.month,
        quarter=args.quarter,
        year=args.year,
        range_start=args.range[0] if args.range else None,
        range_end=args.range[1] if args.range else None,
        sheet_url=args.sheet,
        from_data=args.from_data,
        no_pull=args.no_pull,
        gdoc_url=args.gdoc_url,
    )
