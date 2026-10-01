import argparse
import asyncio
import os
import sys
from pathlib import Path

from engine import (
    SaveitEngine,
    cleanup_empty_downloads_dir,
    parse_group_targets,
)
from tracker import FileTracker

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from telethon import utils
except ImportError:
    utils = None


def parse_args():
    """Parses optional command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Saveit - Telegram Timed Media Saver & Group Tutorial Forwarder"
    )
    parser.add_argument(
        "-g",
        "--forward-groups",
        type=str,
        default=None,
        help="Comma-separated group/channel IDs or usernames to auto-forward from (e.g. -1001234567890,@learnpython)",
    )
    parser.add_argument(
        "--media-only",
        action="store_true",
        default=None,
        help="Only forward messages containing media (videos, documents, photos)",
    )
    parser.add_argument(
        "--mode",
        choices=["forward", "copy"],
        default=None,
        help="Forwarding mode: 'forward' (direct forward with fallback) or 'copy' (download and re-upload)",
    )
    parser.add_argument(
        "--backfill",
        type=str,
        default=None,
        help="Number of recent messages or 'all' to backfill/forward from monitored groups on startup (e.g. 50, all)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        default=None,
        help="Clean up downloaded files and remove the downloads/ directory after uploading to Saved Messages (default: enabled)",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        default=False,
        help="Disable automatic cleanup of downloaded files and the downloads/ directory",
    )
    parser.add_argument(
        "--list-chats",
        action="store_true",
        help="List all joined groups, channels, and their IDs, then exit",
    )
    parser.add_argument(
        "--save-all-media",
        type=str,
        default=None,
        metavar="TARGET",
        help="Save ALL media from a specific group/channel ID or username to Saved Messages, then exit",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Display SQLite duplicate tracker statistics and storage totals, then exit",
    )
    parser.add_argument(
        "-r",
        "--rate-limit",
        type=float,
        default=None,
        help="Rate limit delay in seconds between Telegram actions (default: 1.5)",
    )
    parsed, _ = parser.parse_known_args()
    return parsed


async def cli_list_chats(engine: SaveitEngine):
    """Fetches and displays joined groups and channels."""
    chats = await engine.get_dialogs_list()
    print("\n" + "=" * 85)
    print("Listing joined groups and channels:")
    print(f"{'Type':<12} {'Chat ID':<20} {'Username':<25} {'Title'}")
    print("-" * 85)
    for c in chats:
        print(f"{c['type']:<12} {c['id']:<20} {c['username']:<25} {c['title']}")
    print("=" * 85)
    print("💡 Copy the Chat ID or Username into FORWARD_GROUP_IDS in .env to auto-forward.")
    print("=" * 85 + "\n")


async def cli_save_all(engine: SaveitEngine, target: str):
    """Executes on-demand save of all media in a chat."""
    await engine.authenticate()
    target_entity = int(target) if (target.startswith("-") or target.isdigit()) else target
    print("=" * 65)
    print(f"Logged in as: {engine.me.username or engine.me.first_name} (User ID: {engine.your_user_id})")
    print(f"Scanning and saving ALL media from '{target}' to Saved Messages...")
    print("=" * 65)

    def cli_progress(processed, saved, done):
        if not done:
            print(f"\r[Save-All] Scanned {processed} messages | Saved {saved} media files...", end="", flush=True)
        else:
            print(f"\n[Save-All] Done! Successfully saved {saved} media files to Saved Messages.")

    try:
        await engine.batch_save_chat_messages(
            target_entity,
            limit=None,
            media_only=True,
            progress_callback=cli_progress,
        )
    except Exception as err:
        print(f"\n[Save-All] Failed: {err}")


def print_stats(tracker_db: str, rate_delay: float):
    """Displays duplicate tracker database statistics."""
    tracker = FileTracker(tracker_db)
    st = tracker.get_stats()
    mb = st["total_bytes"] / (1024 * 1024)
    gb = mb / 1024
    size_str = f"{gb:.2f} GB" if gb >= 1.0 else f"{mb:.2f} MB"
    print("=" * 60)
    print("Saveit SQLite Duplicate Tracker Statistics:")
    print(f"  • Database Path:       {st['db_path']}")
    print(f"  • Total Saved Records: {st['total_records']}")
    print(f"  • Total Archived Size: {size_str}")
    print(f"  • Unique File Hashes:  {st['unique_hashes']}")
    print(f"  • Rate Limit Delay:    {rate_delay}s per action")
    print("=" * 60)


async def main():
    args = parse_args()

    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    handler = os.getenv("HANDLER", ".saveit")
    auto_save_timed = os.getenv("AUTO_SAVE_TIMED", "true").lower() in {"1", "true", "yes", "on"}

    forward_group_ids_env = os.getenv(
        "FORWARD_GROUP_IDS",
        os.getenv("FORWARD_GROUPS", os.getenv("FORWARD_CHATS", os.getenv("WATCH_GROUPS", ""))),
    )
    configured_groups_raw = args.forward_groups if args.forward_groups is not None else forward_group_ids_env

    forward_media_only_env = os.getenv("FORWARD_MEDIA_ONLY", "false").lower() in {"1", "true", "yes", "on"}
    forward_media_only = args.media_only if args.media_only is not None else forward_media_only_env

    forward_mode_env = os.getenv("FORWARD_MODE", "forward").lower()
    forward_mode = args.mode if args.mode is not None else forward_mode_env

    force_document = os.getenv("FORCE_DOCUMENT", "true").lower() in {"1", "true", "yes", "on"}

    cleanup_downloads_env = os.getenv("CLEANUP_DOWNLOADS", "true").lower() in {"1", "true", "yes", "on"}
    if args.no_cleanup:
        cleanup_downloads = False
    elif args.cleanup is not None:
        cleanup_downloads = args.cleanup
    else:
        cleanup_downloads = cleanup_downloads_env

    raw_backfill = os.getenv("BACKFILL_LIMIT", "0").strip().lower()
    if args.backfill is not None:
        raw_arg = str(args.backfill).strip().lower()
        backfill_limit = "all" if raw_arg in {"all", "full", "max"} else (int(raw_arg) if raw_arg.isdigit() else 0)
    else:
        backfill_limit = "all" if raw_backfill in {"all", "full", "max"} else (int(raw_backfill) if raw_backfill.isdigit() else 0)

    try:
        rate_limit_delay_env = float(os.getenv("RATE_LIMIT_DELAY", os.getenv("RATE_LIMIT", "1.5")))
    except ValueError:
        rate_limit_delay_env = 1.5
    rate_limit_delay = args.rate_limit if args.rate_limit is not None else rate_limit_delay_env

    try:
        flood_sleep_threshold = int(os.getenv("FLOOD_SLEEP_THRESHOLD", "60"))
    except ValueError:
        flood_sleep_threshold = 60

    tracker_db = os.getenv("TRACKER_DB", "saveit_tracker.db")

    if args.stats:
        print_stats(tracker_db, rate_limit_delay)
        return

    if not api_id or not api_hash:
        print("Error: API_ID and API_HASH must be configured in your .env file or environment.")
        print("Please check .env.example or run run.sh / run.bat to configure credentials.")
        sys.exit(1)

    engine = SaveitEngine(
        api_id=api_id,
        api_hash=api_hash,
        handler=handler,
        auto_save_timed=auto_save_timed,
        forward_groups=configured_groups_raw,
        forward_media_only=forward_media_only,
        forward_mode=forward_mode,
        force_document=force_document,
        cleanup_downloads=cleanup_downloads,
        backfill_limit=backfill_limit,
        rate_limit_delay=rate_limit_delay,
        flood_sleep_threshold=flood_sleep_threshold,
        tracker_db=tracker_db,
    )

    if args.list_chats:
        await engine.authenticate()
        await cli_list_chats(engine)
        await engine.stop()
        return

    if args.save_all_media:
        await cli_save_all(engine, args.save_all_media)
        await engine.stop()
        return

    # Banner display
    print("=" * 65)
    print("Saveit - Telegram Timed Media Saver & Group Tutorial Forwarder")
    print("=" * 65)

    try:
        await engine.start()
        print("=" * 65)
        print("Saveit is active and listening for messages. Press Ctrl+C to stop.")
        await engine.run_until_stopped()
    except KeyboardInterrupt:
        print("\n[Saveit] Stopped by user (Ctrl+C). Goodbye!")
    finally:
        await engine.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Saveit] Stopped by user. Goodbye!")
