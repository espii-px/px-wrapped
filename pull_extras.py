"""Pull post links, impressions, and clicks for each ad in creatives.json."""
import os, json, requests
from dotenv import load_dotenv
from meta_api import get_ad_post_link, GRAPH_URL

load_dotenv()
TOKEN = os.getenv("META_ACCESS_TOKEN")

with open("creatives.json") as f:
    ads = json.load(f)

for ad in ads:
    aid = ad["ad_id"]
    # Get post link
    link = get_ad_post_link(aid, TOKEN)
    print(f"[{ad['rank']}] {ad['ad_name']}")
    print(f"    Post link: {link}")

    # Get impressions/clicks from insights
    url = f"{GRAPH_URL}/{aid}/insights"
    params = {
        "access_token": TOKEN,
        "time_range": '{"since":"2026-01-01","until":"2026-03-31"}',
        "fields": "impressions,clicks",
    }
    resp = requests.get(url, params=params, timeout=30)
    data = resp.json()
    imps = 0
    clicks = 0
    for row in data.get("data", []):
        imps = int(row.get("impressions", 0))
        clicks = int(row.get("clicks", 0))

    print(f"    Impressions: {imps:,} | Clicks: {clicks:,}")
    ad["post_link"] = link
    ad["impressions"] = imps
    ad["clicks"] = clicks

with open("creatives.json", "w") as f:
    json.dump(ads, f, indent=2)

print("\nUpdated creatives.json with post links, impressions, clicks.")
