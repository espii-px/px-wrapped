"""
Pull top Q1 ad creatives from Meta for the Wrapped page.
Downloads video thumbnails and video files into assets/.
Outputs a creatives.json for the HTML to consume.
"""

import os
import json
import requests
from dotenv import load_dotenv
from meta_api import pull_ad_data, get_ad_creative, get_video_url, GRAPH_URL

load_dotenv()

TOKEN = os.getenv("META_ACCESS_TOKEN")
ACCOUNT_ID = "2120443238413329"  # Zippi
CAMPAIGN_FILTER = "PX"

# Q1 date range
START = "2026-01-01"
END = "2026-03-31"


def get_ad_thumbnail(ad_id, token):
    """Get the thumbnail URL for an ad creative (image or video thumbnail)."""
    url = f"{GRAPH_URL}/{ad_id}/adcreatives"
    params = {
        "access_token": token,
        "fields": "thumbnail_url,image_url,object_story_spec,effective_object_story_id",
    }
    resp = requests.get(url, params=params, timeout=30)
    data = resp.json()

    for c in data.get("data", []):
        # Try thumbnail_url first (video ads)
        if c.get("thumbnail_url"):
            return c["thumbnail_url"]
        # Try image_url (static ads)
        if c.get("image_url"):
            return c["image_url"]
        # Try object_story_spec for image
        oss = c.get("object_story_spec", {})
        link_data = oss.get("link_data", {})
        if link_data.get("image_hash"):
            # Can't resolve hash to URL easily, skip
            pass
        if link_data.get("picture"):
            return link_data["picture"]
    return ""


def get_ad_type(ad_id, token):
    """Determine if an ad is video or static."""
    url = f"{GRAPH_URL}/{ad_id}/adcreatives"
    params = {
        "access_token": token,
        "fields": "video_id,object_story_spec",
    }
    resp = requests.get(url, params=params, timeout=30)
    data = resp.json()

    for c in data.get("data", []):
        if c.get("video_id"):
            return "video"
        oss = c.get("object_story_spec", {})
        if oss.get("video_data", {}).get("video_id"):
            return "video"
    return "static"


def download_file(url, filepath):
    """Download a file from URL."""
    try:
        resp = requests.get(url, timeout=60, stream=True)
        if resp.status_code == 200:
            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
    except Exception as e:
        print(f"  Download failed: {e}")
    return False


def main():
    print(f"Pulling top ads for Doneverse Q1 ({START} to {END})...")

    # Pull top 6 ads by spend for the full quarter
    client_key = {
        "primary_event": "lead",
        "primary_event_label": "Leads",
        "cost_label": "Cost per Lead",
        "secondary_events": {
            "offsite_conversion.fb_pixel_custom": "Trial Started + Subscriptions",
        },
        "track_subscription_value": True,
    }

    result = pull_ad_data(
        account_id=ACCOUNT_ID,
        token=TOKEN,
        start=START,
        end=END,
        top_n=6,
        campaign_filter=CAMPAIGN_FILTER,
        client_key=client_key,
    )

    if result.get("error"):
        print(f"API Error: {result['error']}")
        return

    ads = result["ads"]
    print(f"Got {len(ads)} top ads\n")

    creatives = []

    for i, ad in enumerate(ads):
        ad_id = ad["ad_id"]
        ad_name = ad["ad_name"]
        spend = ad["spend"]
        results = ad["results"]
        cpr = ad["cost_per_result"]

        print(f"[{i+1}] {ad_name}")
        print(f"    Spend: ${spend:,.2f} | Results: {results} | CPR: ${cpr or 0:,.2f}")

        # Get creative copy
        creative = get_ad_creative(ad_id, TOKEN)
        print(f"    Headline: {creative['headline'][:60] if creative['headline'] else 'N/A'}")

        # Determine type
        ad_type = get_ad_type(ad_id, TOKEN)
        print(f"    Type: {ad_type}")

        # Get thumbnail
        thumb_url = get_ad_thumbnail(ad_id, TOKEN)
        thumb_path = ""
        if thumb_url:
            ext = ".jpg"
            thumb_path = f"assets/ad_{i+1}_thumb{ext}"
            if download_file(thumb_url, thumb_path):
                print(f"    Thumbnail saved: {thumb_path}")
            else:
                thumb_path = ""

        # Get video URL if video ad
        video_url = ""
        video_path = ""
        if ad_type == "video":
            video_url = get_video_url(ad_id, TOKEN)
            if video_url:
                video_path = f"assets/ad_{i+1}_video.mp4"
                print(f"    Downloading video...")
                if download_file(video_url, video_path):
                    print(f"    Video saved: {video_path}")
                else:
                    video_path = ""

        creatives.append({
            "rank": i + 1,
            "ad_id": ad_id,
            "ad_name": ad_name,
            "spend": spend,
            "results": results,
            "cost_per_result": cpr,
            "headline": creative["headline"],
            "primary_text": creative["primary_text"][:200] if creative["primary_text"] else "",
            "ad_type": ad_type,
            "thumbnail": thumb_path,
            "video": video_path,
        })

        print()

    # Save creatives data
    with open("creatives.json", "w") as f:
        json.dump(creatives, f, indent=2)

    print(f"Done! Saved {len(creatives)} creatives to creatives.json")
    print("Thumbnails and videos in assets/")


if __name__ == "__main__":
    main()
