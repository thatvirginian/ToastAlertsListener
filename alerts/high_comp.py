# -*- coding: utf-8 -*-
import logging
import os

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv(override=True)

logger      = logging.getLogger("alerts.high_comp")
WEBHOOK_URL = os.getenv("TEAMS_COMP_WEBHOOK_URL")


def _enrich(data: dict) -> dict:
    """
    Joins to orders_head, locations, and employees to get
    display fields for the Teams card.
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
                es.first_name || ' ' || es.last_name AS server_name,
                ea.first_name || ' ' || ea.last_name AS approver_name
            FROM public.orders_head o
            JOIN  public.locations l      ON o.location_id::text     = l.store_guid::text
            LEFT JOIN public.employees es ON o.server_guid::text     = es.guid::text
            LEFT JOIN public.employees ea ON %s::text                = ea.guid::text
            WHERE o.order_guid = %s
            """,
            (str(data.get("approver_guid")), str(data.get("order_guid")))
        )
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()

    if not row:
        logger.warning(f"No enrichment data found for order_guid: {data.get('order_guid')}")
        return {
            **data,
            "location_abbr": "Unknown",
            "location_name": "Unknown",
            "order_number":  "Unknown",
            "server_name":   "Unknown",
            "approver_name": "Unknown",
        }

    return {
        **data,
        "location_abbr": row[0] or "Unknown",
        "location_name": row[1] or "Unknown",
        "order_number":  row[2] or "Unknown",
        "server_name":   row[3] or "Unknown",
        "approver_name": row[4] or "Unknown",
    }


def _format_time(ts) -> str:
    """Format a timestamp to readable local time string."""
    if not ts:
        return "N/A"
    try:
        from datetime import timezone
        import pytz
        from dateutil.parser import parse
        eastern = pytz.timezone("America/New_York")
        if isinstance(ts, str):
            ts = parse(ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(eastern).strftime("%Y-%m-%d %I:%M %p")
    except Exception:
        return str(ts)


def _build_card(data: dict) -> dict:
    """Build the Teams Adaptive Card payload."""
    opened_time     = _format_time(data.get("opened_date"))
    closed_time     = _format_time(data.get("closed_date"))
    net_amount      = f"${float(data.get('net_amount',  0)):,.2f}"
    comp_amount     = f"${float(data.get('comp_amount', 0)):,.2f}"
    gross_amount    = f"${float(data.get('gross_amount', 0)):,.2f}"
    comp_percentage = f"{float(data.get('comp_percentage', 0)):.2f}%"

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
                            "text": "🚨 High Comp Alert",
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
                                {"title": "Server",         "value": data["server_name"]},
                                {"title": "Comp Approver",  "value": data["approver_name"]},
                                {"title": "",               "value": ""},
                                {"title": "Opened",         "value": opened_time},
                                {"title": "Closed",         "value": closed_time},
                                {"title": "",               "value": ""},
                                {"title": "Comp Type(s)",   "value": data.get("comp_types",    "N/A")},
                                {"title": "Discount Name",  "value": data.get("discount_name", "N/A")},
                                {"title": "Reason(s)",      "value": data.get("reasons",       "N/A")},
                                {"title": "",               "value": ""},
                                {"title": "Gross Amount",   "value": gross_amount},
                                {"title": "Comp Amount",    "value": comp_amount},
                                {"title": "Net Amount",     "value": net_amount},
                                {"title": "Comp %",         "value": comp_percentage},
                                {"title": "Order GUID", "value": str(data.get("order_guid", "N/A"))},
                            ]
                        }
                    ]
                }
            }
        ]
    }


def handle_high_comp(data: dict):
    """
    Enriches the payload, builds the Teams card, and sends it.
    Called by the listener when alert_type == 'high_comp'.
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
        logger.info(f"High comp alert sent — check: {enriched['check_number']} | comp%: {data.get('comp_percentage')}%")
    else:
        raise RuntimeError(f"Teams webhook failed: {response.status_code} — {response.text}")
