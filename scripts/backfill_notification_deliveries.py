from __future__ import annotations

import argparse
import asyncio
import json

from api.services.alert_store import AlertStore
from api.services.notification_service import NotificationDispatcher
from api.services.notification_store import NotificationStore
from streaming.sqlite_store import workflow_event_store_path


def parser() -> argparse.ArgumentParser:
    selected = argparse.ArgumentParser(description="Safely inspect or create notification deliveries for open alerts.")
    selected.add_argument("--include-open-alerts", action="store_true")
    selected.add_argument("--include-external", action="store_true")
    selected.add_argument("--allow-external", action="store_true")
    selected.add_argument("--execute", action="store_true", help="Create deliveries; default is dry-run.")
    return selected


async def main(argv: list[str] | None = None) -> dict[str, int | bool]:
    args = parser().parse_args(argv)
    if args.include_external and not args.allow_external:
        raise SystemExit("--include-external requires --allow-external")
    path = workflow_event_store_path(); alerts = AlertStore(path); notifications = NotificationStore(path)
    await alerts.initialize(); await notifications.initialize()
    open_alerts, _, _ = await alerts.list_alerts(statuses=["open", "acknowledged"], limit=100_000, offset=0)
    channels = {item.channel_id: item for item in await notifications.list_channels()}
    policies = await notifications.list_policies()
    candidates = 0; created = 0
    for alert in open_alerts if args.include_open_alerts else []:
        for policy in policies:
            if not NotificationDispatcher._matches(policy, alert, "alert_opened"): continue
            for channel_id in policy.channel_ids:
                channel = channels.get(channel_id)
                if channel and (channel.channel_type == "internal" or args.include_external): candidates += 1
        if args.execute:
            # Historical external delivery is impossible without both explicit flags.
            selected = [policy for policy in policies if all(channels.get(item) and (channels[item].channel_type == "internal" or args.include_external) for item in policy.channel_ids)]
            original = notifications.list_policies
            notifications.list_policies = lambda: asyncio.sleep(0, result=selected)  # type: ignore[method-assign]
            try:
                result = await NotificationDispatcher(notifications).dispatch_alert_event(alert=alert, alert_event="alert_opened", occurrence_id=f"backfill:{alert.alert_id}")
            finally:
                notifications.list_policies = original  # type: ignore[method-assign]
            created += result.created
    summary = {"dry_run": not args.execute, "open_alerts": len(open_alerts), "candidate_deliveries": candidates, "created": created, "external_allowed": bool(args.include_external and args.allow_external)}
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True)); return summary


if __name__ == "__main__": asyncio.run(main())
