"""
PX Newsletter -- Meta Ads API helper
Pulls ad-level data, preview links, and ad copy for newsletter generation.
"""

import time
import requests
from datetime import datetime, timedelta

GRAPH_URL = "https://graph.facebook.com/v21.0"


def _last_monday_sunday(ref_date: datetime = None):
    """Return (Monday, Sunday) of the most recent completed Mon-Sun week."""
    if ref_date is None:
        ref_date = datetime.now()
    days_since_sunday = (ref_date.weekday() + 1) % 7
    if days_since_sunday == 0:
        days_since_sunday = 7
    last_sunday = ref_date - timedelta(days=days_since_sunday)
    last_monday = last_sunday - timedelta(days=6)
    return last_monday.strftime("%Y-%m-%d"), last_sunday.strftime("%Y-%m-%d")


def _resolve_primary_event(action_type):
    """Map shorthand event names to Meta action_type strings."""
    shortcuts = {
        "lead": ("lead", "offsite_conversion.fb_pixel_lead"),
        "purchase": ("purchase", "offsite_conversion.fb_pixel_purchase"),
    }
    if action_type in shortcuts:
        return shortcuts[action_type]
    return (action_type,)


def pull_ad_data(account_id: str, token: str, start: str, end: str,
                 top_n: int = 3, campaign_filter: str = "",
                 client_key: dict = None) -> dict:
    """Pull ad-level data for an account in a date range.

    client_key (from client_keys.json) drives which events to count:
      - primary_event: the action_type to count as the main result
      - secondary_events: additional action_types to track
      - track_roas / track_subscription_value: flags

    Returns {
        "account": {spend, impressions, clicks, results, result_value,
                     cost_per_result, roas, ctr, secondary},
        "ads": [top N by spend]
    }
    """
    ck = client_key or {}
    primary_event = ck.get("primary_event", "lead")
    primary_match = _resolve_primary_event(primary_event)
    fallback_event = ck.get("fallback_event", "")
    fallback_match = _resolve_primary_event(fallback_event) if fallback_event else ()
    secondary_defs = ck.get("secondary_events", {})
    track_roas = ck.get("track_roas", False)
    track_sub_value = ck.get("track_subscription_value", False)
    cost_label = ck.get("cost_label", "CPR")
    result_label = ck.get("primary_event_label", "Results")

    url = f"{GRAPH_URL}/act_{account_id}/insights"
    params = {
        "access_token": token,
        "time_range": f'{{"since":"{start}","until":"{end}"}}',
        "fields": "ad_id,ad_name,campaign_name,spend,impressions,clicks,actions,action_values",
        "level": "ad",
        "limit": 50,
        "sort": "spend_descending",
    }
    resp = requests.get(url, params=params, timeout=60)
    data = resp.json()

    # Check if we need conversions field (for custom pixel events like PX_CONSULTBOOKED)
    needs_conversions = (
        "fb_pixel_custom" in primary_event
        or "schedule" in primary_event
        or "submit_application" in primary_event
        or any("fb_pixel_custom" in e or "schedule" in e or "submit_application" in e
               for e in secondary_defs)
        or ("fb_pixel_custom" in fallback_event or "schedule" in fallback_event
            or "submit_application" in fallback_event)
    )

    conv_by_ad = {}
    if needs_conversions and not data.get("error"):
        conv_params = {
            "access_token": token,
            "time_range": f'{{"since":"{start}","until":"{end}"}}',
            "fields": "ad_id,conversions",
            "level": "ad",
            "limit": 50,
            "sort": "spend_descending",
        }
        conv_resp = requests.get(url, params=conv_params, timeout=60)
        conv_data = conv_resp.json()
        for row in conv_data.get("data", []):
            aid = row.get("ad_id", "")
            conv_by_ad[aid] = row.get("conversions", [])

    if "error" in data:
        empty_account = {"spend": 0, "impressions": 0, "clicks": 0,
                         "results": 0, "result_value": 0, "cost_per_result": 0,
                         "roas": 0, "ctr": 0, "cost_label": cost_label,
                         "result_label": result_label, "secondary": {}}
        return {"account": empty_account, "ads": [],
                "error": data["error"].get("message", str(data["error"]))}

    ads = []
    for row in data.get("data", []):
        if campaign_filter:
            cname = row.get("campaign_name", "")
            filters = [f.strip() for f in campaign_filter.split(",")]
            if not all(f.upper() in cname.upper() for f in filters):
                continue

        spend = float(row.get("spend", 0))
        impressions = int(row.get("impressions", 0))
        clicks = int(row.get("clicks", 0))
        results = 0
        result_value = 0
        secondary = {}

        # Check actions first
        fallback_results = 0
        for a in row.get("actions", []):
            at = a["action_type"]
            val = int(a["value"])
            if at in primary_match:
                results = max(results, val)
            if at in fallback_match:
                fallback_results = max(fallback_results, val)
            for sec_event, sec_label in secondary_defs.items():
                if at == sec_event or at.startswith(sec_event):
                    secondary[sec_label] = secondary.get(sec_label, 0) + val

        # Check conversions field (has custom pixel events like PX_CONSULTBOOKED)
        ad_convs = conv_by_ad.get(row.get("ad_id", ""), [])
        for c in ad_convs:
            ct = c["action_type"]
            val = int(c["value"])
            if ct in primary_match:
                results = max(results, val)
            if ct in fallback_match:
                fallback_results = max(fallback_results, val)
            for sec_event, sec_label in secondary_defs.items():
                if ct == sec_event or ct.startswith(sec_event):
                    secondary[sec_label] = secondary.get(sec_label, 0) + val

        # Use fallback if primary found nothing
        if results == 0 and fallback_results > 0:
            results = fallback_results

        for av in row.get("action_values", []):
            at = av["action_type"]
            val = float(av["value"])
            if at in primary_match:
                result_value = max(result_value, val)
            if track_sub_value and "custom" in at:
                secondary["Subscription Value"] = secondary.get("Subscription Value", 0) + val

        ctr = round(clicks / impressions * 100, 2) if impressions > 0 else 0
        cpr = round(spend / results, 2) if results > 0 else None
        roas = round(result_value / spend, 2) if spend > 0 and result_value > 0 else 0

        # Get post link
        aid = row.get("ad_id", "")
        post_link = get_ad_post_link(aid, token) if aid else ""

        ads.append({
            "ad_id": aid,
            "ad_name": row.get("ad_name", "Unknown"),
            "campaign_name": row.get("campaign_name", ""),
            "spend": spend,
            "impressions": impressions,
            "clicks": clicks,
            "results": results,
            "result_value": result_value,
            "ctr": ctr,
            "cost_per_result": cpr,
            "roas": roas,
            "post_link": post_link,
            "secondary": secondary,
        })

    total_spend = sum(a["spend"] for a in ads)
    total_impressions = sum(a["impressions"] for a in ads)
    total_clicks = sum(a["clicks"] for a in ads)
    total_results = sum(a["results"] for a in ads)
    total_rv = sum(a["result_value"] for a in ads)
    total_secondary = {}
    for a in ads:
        for k, v in a.get("secondary", {}).items():
            total_secondary[k] = total_secondary.get(k, 0) + v

    account = {
        "spend": total_spend,
        "impressions": total_impressions,
        "clicks": total_clicks,
        "results": total_results,
        "result_value": total_rv,
        "cost_per_result": round(total_spend / total_results, 2) if total_results > 0 else 0,
        "roas": round(total_rv / total_spend, 2) if total_spend > 0 and total_rv > 0 else 0,
        "ctr": round(total_clicks / total_impressions * 100, 2) if total_impressions > 0 else 0,
        "cost_label": cost_label,
        "result_label": result_label,
        "secondary": total_secondary,
    }

    ads_sorted = sorted(ads, key=lambda x: x["spend"], reverse=True)[:top_n]
    return {"account": account, "ads": ads_sorted}


def get_ad_post_link(ad_id: str, token: str) -> str:
    """Get the Facebook post link for an ad via effective_object_story_id."""
    url = f"{GRAPH_URL}/{ad_id}/adcreatives"
    params = {
        "access_token": token,
        "fields": "effective_object_story_id",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        for c in data.get("data", []):
            osi = c.get("effective_object_story_id", "")
            if "_" in osi:
                page_id, post_id = osi.split("_", 1)
                return f"https://www.facebook.com/{page_id}/posts/{post_id}/"
    except Exception:
        pass
    return ""


def get_ad_preview_link(ad_id: str, token: str) -> str:
    """Get the fb.me preview link for an ad."""
    url = f"{GRAPH_URL}/{ad_id}/previews"
    params = {
        "access_token": token,
        "ad_format": "DESKTOP_FEED_STANDARD",
    }
    resp = requests.get(url, params=params, timeout=30)
    data = resp.json()
    if data.get("data"):
        # Extract href from the iframe HTML
        html = data["data"][0].get("body", "")
        if "href=" in html:
            start = html.index("href=") + 6
            end = html.index('"', start)
            link = html[start:end].replace("&amp;", "&")
            return link
    # Fallback: construct a direct ad link
    return f"https://www.facebook.com/ads/library/?id={ad_id}"


def get_ad_creative(ad_id: str, token: str) -> dict:
    """Get ad creative details (primary text, headline, description).

    Returns {"primary_text": str, "headline": str, "description": str}
    """
    result = {"primary_text": "", "headline": "", "description": ""}

    # Use adcreatives endpoint — more reliable than nested creative{} query
    url = f"{GRAPH_URL}/{ad_id}/adcreatives"
    params = {
        "access_token": token,
        "fields": "body,title,object_story_spec",
    }
    resp = requests.get(url, params=params, timeout=30)
    data = resp.json()

    for c in data.get("data", []):
        result["primary_text"] = c.get("body", "")
        result["headline"] = c.get("title", "")

        # Fallback to object_story_spec
        if not result["primary_text"]:
            oss = c.get("object_story_spec", {})
            link_data = oss.get("link_data", {})
            result["primary_text"] = link_data.get("message", "")
            if not result["headline"]:
                result["headline"] = link_data.get("name", "")

            video_data = oss.get("video_data", {})
            if not result["primary_text"]:
                result["primary_text"] = video_data.get("message", "")
            if not result["headline"]:
                result["headline"] = video_data.get("title", "")
        break

    return result


def get_video_url(ad_id: str, token: str) -> str:
    """Get the video source URL from an ad's creative.

    Key: use object_story_spec.video_data.video_id (inner ID), NOT the
    top-level video_id. The inner ID returns the source URL; the outer
    one is blocked by permissions.
    """
    url = f"{GRAPH_URL}/{ad_id}/adcreatives"
    params = {
        "access_token": token,
        "fields": "video_id,object_story_spec",
    }
    resp = requests.get(url, params=params, timeout=30)
    data = resp.json()

    for c in data.get("data", []):
        # Try inner video_id first (from object_story_spec.video_data)
        oss = c.get("object_story_spec", {})
        inner_vid = oss.get("video_data", {}).get("video_id")
        if inner_vid:
            r = requests.get(
                f"{GRAPH_URL}/{inner_vid}",
                params={"access_token": token, "fields": "source"},
                timeout=30,
            )
            src = r.json().get("source", "")
            if src:
                return src

        # Fallback: try top-level video_id
        outer_vid = c.get("video_id")
        if outer_vid:
            r = requests.get(
                f"{GRAPH_URL}/{outer_vid}",
                params={"access_token": token, "fields": "source"},
                timeout=30,
            )
            src = r.json().get("source", "")
            if src:
                return src

    return ""


def transcribe_video(video_url: str, fal_key: str) -> str:
    """Transcribe a video using fal.ai Whisper API.

    Args:
        video_url: Direct URL to video file (from Meta API source field)
        fal_key: fal.ai API key

    Returns:
        Transcribed text string

    Cost: ~$0.005 per minute of audio (very cheap)
    """
    if not video_url or not fal_key:
        return ""

    try:
        resp = requests.post(
            "https://fal.run/fal-ai/whisper",
            headers={
                "Authorization": f"Key {fal_key}",
                "Content-Type": "application/json",
            },
            json={"audio_url": video_url},
            timeout=120,
        )
        if resp.status_code == 200:
            text = resp.json().get("text", "").strip()
            # Break into sentences so it reads like a script
            import re
            text = re.sub(r'([.!?])\s+', r'\1\n', text)
            return text
        return ""
    except Exception:
        return ""


def pull_ad_data_from_json(account_id: str, week_data: dict,
                           top_n: int = 3, campaign_filter: str = "") -> dict:
    """Pull ad data from pre-fetched week_data.json instead of the API.

    week_data.json now stores the new shape from pull_ad_data (results, cost_per_result, etc).
    Returns the account dict and top N ads directly.
    """
    acct_data = week_data.get(account_id)
    if not acct_data:
        return {
            "account": {"spend": 0, "impressions": 0, "clicks": 0,
                        "results": 0, "result_value": 0, "cost_per_result": 0,
                        "roas": 0, "ctr": 0, "secondary": {}},
            "ads": [],
            "error": f"Account {account_id} not found in week_data.json",
        }

    all_ads = acct_data.get("ads", [])
    if campaign_filter:
        filters = [f.strip() for f in campaign_filter.split(",")]
        all_ads = [a for a in all_ads
                   if all(f.upper() in a.get("campaign_name", "").upper() for f in filters)]

    ads = all_ads[:top_n]

    # Account-level data is already computed and stored
    account = {k: v for k, v in acct_data.items() if k != "ads"}

    return {"account": account, "ads": ads}
