# -*- coding: utf-8 -*-
import json
import logging
import os
import select
import sys
import time
import psycopg2
import psycopg2.extensions
from dotenv import load_dotenv
from alerts.late_cash_void import handle_late_cash_void

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("listener")

# =============================================================================
# Alert router — register new alert types here
# =============================================================================
ALERT_HANDLERS = {
    "late_cash_void": handle_late_cash_void,
}


def get_raw_connection():
    """
    Returns a raw psycopg2 connection for LISTEN/NOTIFY.
    SQLAlchemy's connection pool doesn't support LISTEN, so we use psycopg2
    directly here.
    """
    return psycopg2.connect(
        host=os.getenv("PGHOST"),
        port=os.getenv("PGPORT"),
        dbname=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
        sslmode="require",
    )


def listen(conn):
    """Set the connection to LISTEN on the alerts channel."""
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute("LISTEN alerts;")
    cur.close()
    logger.info("Listening on channel: alerts")


def process_notification(raw_payload, conn):
    """
    Parse the JSON payload, route to the correct handler, log to alert_log.
    """
    try:
        data = json.loads(raw_payload)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse notify payload: {e} | raw: {raw_payload}")
        return

    alert_type  = data.get("alert_type")
    entity_guid = data.get("payment_guid")

    if not alert_type or not entity_guid:
        logger.warning(f"Payload missing alert_type or entity_guid: {data}")
        return

    # Route to handler
    handler = ALERT_HANDLERS.get(alert_type)
    if not handler:
        logger.warning(f"No handler registered for alert_type: {alert_type}")
        return

    try:
        handler(data)
        logger.info(f"Alert sent — type: {alert_type} | guid: {entity_guid}")
    except Exception as e:
        logger.error(f"Handler failed for {alert_type}: {e}", exc_info=True)
        return

    # Log to alert_log so we never double-send
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO alert_log (alert_type, entity_guid)
            VALUES (%s, %s)
            ON CONFLICT (alert_type, entity_guid) DO NOTHING;
            """,
            (alert_type, str(entity_guid)),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Failed to write to alert_log: {e}", exc_info=True)


def run():
    """
    Main loop — connects, listens, processes notifications.
    Reconnects automatically if the connection drops.
    """
    logger.info("Alert listener starting...")

    while True:
        conn = None
        try:
            conn = get_raw_connection()
            listen(conn)

            while True:
                # Block for up to 30s waiting for a notification
                # select() avoids busy-waiting — CPU stays at ~0% when idle
                if select.select([conn], [], [], 30) == ([], [], []):
                    # Timeout — send a keepalive so Azure doesn't kill the connection
                    conn.cursor().execute("SELECT 1")
                else:
                    conn.poll()
                    while conn.notifies:
                        notify = conn.notifies.pop(0)
                        logger.info(f"Notification received on channel: {notify.channel}")
                        process_notification(notify.payload, conn)

        except psycopg2.OperationalError as e:
            logger.error(f"DB connection lost: {e} — reconnecting in 10s...")
            time.sleep(10)

        except Exception as e:
            logger.error(f"Unexpected error: {e} — reconnecting in 10s...", exc_info=True)
            time.sleep(10)

        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


if __name__ == "__main__":
    run()
