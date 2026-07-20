# -*- coding: utf-8 -*-
import logging
import os

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv(override=True)

logger       = logging.getLogger("alerts.late_cash_void")
WEBHOOK_URL  = os.getenv("TEAMS_WEBHOOK_URL")


def _enrich(data: dict) -> dict:
    """
    Single query to get all display fields needed for the Teams card.
    Joins through order_checks → orders_head → locations for location,
    and three separate employee lookups for server, void user, and approver.
    """
    conn = psycopg2.connect(
        host=os.getenv("PGHOST"),
        port=os.getenv("PGPORT"),
        dbname=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
        sslmode="require",
    )
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                l.abbreviation,
                l.location_name,
                o.order_number,
                oc.display_number           AS check_number,
                es.first_name || ' ' || es.last_name AS server_name,
                eu.first_name || ' ' || eu.last_name AS void_user_name,
                ea.first_name || ' ' || ea.last_name AS approver_name
            FROM public.check_payments cp
            JOIN  public.order_checks oc    ON cp.check_guid             = oc.check_guid
            JOIN  public.orders_head o      ON oc.order_guid             = o.order_guid
            JOIN  public.locations l        ON o.location_id::text       = l.store_guid::text
            LEFT JOIN public.employees es   ON cp.server_guid::text      = es.guid::text
            LEFT JOIN public.employees eu   ON cp.void_user_guid::text   = eu.guid::text
            LEFT JOIN public.employees ea   ON cp.void_approver_guid::text = ea.guid::text
            WHERE cp.payment_guid = %s
            """,
            (str(data.get("payment_guid")),)
        )
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()

    if not row:
        logger.warning(f"No enrichment data found for payment_guid: {data.get('payment_guid')}")
        return {
            **data,
            "location_abbr":  "Unknown",
            "location_name":  "Unknown",
            "order_number":   "Unknown",
            "check_number":   "Unknown",
            "server_name":    "Unknown",
            "void_user_name": "Unknown",
            "approver_name":  "Unknown",
        }

    return {
        **data,
        "location_abbr":  row[0] or "Unknown",
        "location_name":  row[1] or "Unknown",
        "order_number":   row[2] or "Unknown",
        "check_number":   row[3] or "Unknown",
        "server_name":    row[4] or "Unknown",
        "void_user_name": row[5] or "Unknown",
        "approver_name":  row[6] or "Unknown",
    }


def _format_time(ts) -> str:
    """Format a timestamp to readable local time string."""
    if not ts:
        return "N/A"
    try:
        from datetime import datetime, timezone
        import pytz
        eastern = pytz.timezone("America/New_York")
        if isinstance(ts, str):
            from dateutil.parser import parse
            ts = parse(ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(eastern)
        return local.strftime("%Y-%m-%d %I:%M %p")
    except Exception:
        return str(ts)


def _calc_gap(paid, void) -> str:
    """Calculate HH:MM:SS gap between paid_date and void_date."""
    try:
        from dateutil.parser import parse
        from datetime import timezone
        p = parse(paid) if isinstance(paid, str) else paid
        v = parse(void) if isinstance(void, str) else void
        if p.tzinfo is None:
            p = p.replace(tzinfo=timezone.utc)
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        total_sec = int((v - p).total_seconds())
        h = total_sec // 3600
        m = (total_sec % 3600) // 60
        s = total_sec % 60
        return f"{h:02d}:{m:02d}:{s:02d}"
    except Exception:
        return "N/A"


def _build_card(data: dict) -> dict:
    """Build the Teams Adaptive Card payload."""
    gap          = _calc_gap(data.get("paid_date"), data.get("void_date"))
    payment_time = _format_time(data.get("paid_date"))
    void_time    = _format_time(data.get("void_date"))
    amount       = f"${float(data.get('amount', 0)):,.2f}"

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": "🚨 Late Cash Void Detected",
                            "weight": "Bolder",
                            "size": "Large",
                            "color": "Attention",
                            "wrap": True
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Location",       "value": f"{data['location_abbr']} — {data['location_name']}"},
                                {"title": "Order #",        "value": str(data["order_number"])},
                                {"title": "Check #",        "value": str(data["check_number"])},
                                {"title": "Date",           "value": payment_time.split(" ")[0]},
                                {"title": "Server",         "value": data["server_name"]},
                                {"title": "Void User",      "value": data["void_user_name"]},
                                {"title": "Void Approver",  "value": data["approver_name"]},
                                {"title": "",               "value": ""},
                                {"title": "Payment Time",   "value": payment_time},
                                {"title": "Void Time",      "value": void_time},
                                {"title": "Time Gap",       "value": gap},
                                {"title": "",               "value": ""},
                                {"title": "Payment Type",   "value": data.get("payment_type", "N/A")},
                                {"title": "Amount",         "value": amount},
                                {"title": "",               "value": ""},
                                {"title": "Order GUID",     "value": str(data.get("order_guid", "N/A"))},
                            ]
                        }
                    ]
                }
            }
        ]
    }


def handle_late_cash_void(data: dict):
    """
    Enriches the payload, builds the Teams card, and sends it.
    Called by the listener when alert_type == 'late_cash_void'.
    """
    enriched = _enrich(data)
    card     = _build_card(enriched)

    response = requests.post(
        WEBHOOK_URL,
        json=card,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )

    if response.status_code in (200, 202):
        logger.info(f"Teams alert sent — check: {enriched['check_number']} | gap: {_calc_gap(data.get('paid_date'), data.get('void_date'))}")
    else:
        raise RuntimeError(f"Teams webhook failed: {response.status_code} — {response.text}")
