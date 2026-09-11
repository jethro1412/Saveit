import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

from tracker import FileTracker, extract_telegram_file_id

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from telethon import TelegramClient, events, utils
    from telethon.errors import ChatForwardsRestrictedError, FloodWaitError
except ImportError:
    TelegramClient = None
    events = None
    utils = None
    ChatForwardsRestrictedError = Exception
    FloodWaitError = Exception

# Telegram API credentials
api_id = os.getenv("API_ID")
api_hash = os.getenv("API_HASH")
handler = os.getenv("HANDLER", ".saveit")
auto_save_timed = os.getenv("AUTO_SAVE_TIMED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Group forwarding configuration from environment
forward_group_ids_env = os.getenv(
    "FORWARD_GROUP_IDS",
    os.getenv("FORWARD_GROUPS", os.getenv("FORWARD_CHATS", os.getenv("WATCH_GROUPS", ""))),
)
forward_media_only_env = os.getenv("FORWARD_MEDIA_ONLY", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
forward_mode_env = os.getenv("FORWARD_MODE", "forward").lower()
force_document_env = os.getenv("FORCE_DOCUMENT", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
cleanup_downloads_env = os.getenv("CLEANUP_DOWNLOADS", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

try:
    backfill_limit_env = int(os.getenv("BACKFILL_LIMIT", "0"))
except ValueError:
    backfill_limit_env = 0

tracker_db_env = os.getenv("TRACKER_DB", "saveit_tracker.db")


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
        type=int,
        default=None,
        help="Number of recent messages to backfill/forward from monitored groups on startup",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        default=None,
        help="Clean up downloaded files in downloads/ after uploading to Saved Messages",
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
    # Use parse_known_args to avoid crashing if unrecognized flags are passed
    parsed, _ = parser.parse_known_args()
    return parsed


args = parse_args()

# Command line arguments take precedence over environment variables
configured_groups_raw = (
    args.forward_groups if args.forward_groups is not None else forward_group_ids_env
)
forward_media_only = (
    args.media_only if args.media_only is not None else forward_media_only_env
)
forward_mode = args.mode if args.mode is not None else forward_mode_env
backfill_limit = args.backfill if args.backfill is not None else backfill_limit_env
cleanup_downloads = (
    args.cleanup if args.cleanup is not None else cleanup_downloads_env
)
force_document = force_document_env

tracker = FileTracker(tracker_db_env)

if args.stats:
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
    print("=" * 60)
    sys.exit(0)

if TelegramClient is None:
    print("Error: Required package 'telethon' is not installed.")
    print("Please install requirements: pip install telethon python-dotenv")
    sys.exit(1)

if not api_id or not api_hash:
    print("Error: API_ID and API_HASH must be configured in your .env file or environment.")
    print("Please check .env.example or run run.sh / run.bat to configure credentials.")
    sys.exit(1)

client = TelegramClient("save", int(api_id), api_hash)
downloads_path = Path("downloads")
saved_message_ids = set()
save_lock = asyncio.Lock()
your_user_id = None

# Resolved monitored entities for group auto-forwarding
resolved_group_ids = set()
monitored_chat_names = {}


def parse_group_targets(raw_string):
    """Parses a comma-separated list of group IDs, usernames, or links into clean items."""
    if not raw_string:
        return []
    targets = []
    for item in raw_string.split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        # Handle links like https://t.me/username
        if cleaned.startswith("https://t.me/"):
            cleaned = cleaned.replace("https://t.me/", "")
        # Attempt integer conversion for numerical IDs
        try:
            targets.append(int(cleaned))
        except ValueError:
            targets.append(cleaned)
    return targets


async def resolve_monitored_groups(client_instance, targets):
    """Resolves user-specified group targets to canonical peer IDs and titles."""
    for target in targets:
        try:
            entity = await client_instance.get_entity(target)
            peer_id = utils.get_peer_id(entity)
            resolved_group_ids.add(peer_id)
            if hasattr(entity, "id"):
                resolved_group_ids.add(entity.id)
            title = getattr(entity, "title", getattr(entity, "username", str(peer_id)))
            monitored_chat_names[peer_id] = title
            print(f"  • Monitored: {title} (ID: {peer_id})")
        except Exception as err:
            if isinstance(target, int):
                resolved_group_ids.add(target)
                if target > 0:
                    resolved_group_ids.add(int(f"-100{target}"))
                monitored_chat_names[target] = f"Group {target}"
                print(f"  • Monitored (raw numeric ID): {target} (Resolution warning: {err})")
            else:
                print(f"  • Warning: Could not resolve target '{target}': {err}")


def is_timed_media(message):
    """Telegram exposes the self-destruct timer on the media object."""
    return bool(
        message
        and message.media
        and getattr(message.media, "ttl_seconds", None)
    )


async def forward_or_save_message(
    message,
    source_label=None,
    mode="forward",
    force_doc=True,
    cleanup=False,
):
    """
    Forwards or copies a message to Saved Messages ('me').
    Attempts direct Telegram forwarding first (if mode == 'forward').
    If forwarding is restricted by channel/group ('noforwards' protection),
    automatically falls back to downloading media and sending as original file or copying text.
    Tracks all saved messages, Telegram File IDs, and SHA-256 hashes in SQLite.
    """
    message_key = (message.chat_id, message.id)

    async with save_lock:
        if message_key in saved_message_ids or tracker.is_message_saved(message.chat_id, message.id):
            saved_message_ids.add(message_key)
            return False
        saved_message_ids.add(message_key)

    # Check Telegram internal file ID (zero-download duplicate check)
    telegram_file_id = extract_telegram_file_id(message)
    if telegram_file_id and tracker.is_file_id_saved(telegram_file_id):
        print(f"[Duplicate Skipped] Telegram File ID {telegram_file_id} already archived; skipping.")
        tracker.record_saved(
            chat_id=message.chat_id,
            message_id=message.id,
            telegram_file_id=telegram_file_id,
            caption=message.text,
        )
        return False

    try:
        # 1. Attempt native forwarding if mode is 'forward'
        if mode == "forward":
            try:
                await client.forward_messages("me", message)
                tracker.record_saved(
                    chat_id=message.chat_id,
                    message_id=message.id,
                    telegram_file_id=telegram_file_id,
                    caption=message.text,
                )
                print(f"[Forwarded] Message {message.id} from chat {message.chat_id} to Saved Messages.")
                return True
            except ChatForwardsRestrictedError:
                print(
                    f"[Restricted] Chat {message.chat_id} has forwarding restricted; downloading/copying directly..."
                )
            except Exception as e:
                print(f"[Fallback] Direct forward failed ({e}); falling back to copy/download...")

        # 2. Copy/Download mode (or fallback after restricted forward)
        if message.media:
            downloads_path.mkdir(parents=True, exist_ok=True)
            file_path = await client.download_media(message, file=str(downloads_path))
            if not file_path:
                raise RuntimeError("Telegram did not return a downloadable file")

            path_obj = Path(file_path)
            file_size = path_obj.stat().st_size
            file_name = path_obj.name
            file_hash = tracker.compute_sha256(file_path)

            if tracker.is_file_hash_saved(file_hash):
                print(f"[Duplicate Skipped] Content SHA-256 ({file_hash[:10]}...) already saved; skipping re-upload.")
                path_obj.unlink(missing_ok=True)
                tracker.record_saved(
                    chat_id=message.chat_id,
                    message_id=message.id,
                    telegram_file_id=telegram_file_id,
                    file_name=file_name,
                    file_size=file_size,
                    file_hash=file_hash,
                    caption=message.text,
                )
                return False

            caption = message.text or (f"File saved from {source_label}" if source_label else "")
            full_text = None
            # Standard Telegram caption limit is 1024 characters for free accounts
            if len(caption) > 1024:
                full_text = caption
                caption = caption[:1020] + "..."

            await client.send_file(
                "me",
                file_path,
                caption=caption if caption else None,
                force_document=force_doc,
            )

            if full_text:
                await client.send_message("me", full_text)

            tracker.record_saved(
                chat_id=message.chat_id,
                message_id=message.id,
                telegram_file_id=telegram_file_id,
                file_name=file_name,
                file_size=file_size,
                file_hash=file_hash,
                caption=caption,
            )

            print(f"[Saved Media] Message {message.id} from chat {message.chat_id}: {file_path}")

            if cleanup:
                try:
                    path_obj.unlink(missing_ok=True)
                except Exception as del_err:
                    print(f"Warning: Could not remove temporary file {file_path}: {del_err}")

            return True

        elif message.text:
            # Text tutorial/code snippet without media
            chat_title = source_label or str(message.chat_id)
            header = f"📚 **[{chat_title}]**\n\n"
            await client.send_message("me", header + message.text)
            tracker.record_saved(
                chat_id=message.chat_id,
                message_id=message.id,
                file_size=len(message.text),
                caption=message.text,
            )
            print(f"[Saved Text] Message {message.id} from chat {message.chat_id} to Saved Messages.")
            return True

        return False

    except FloodWaitError as fwe:
        print(f"[Rate Limit] Telegram FloodWait: sleeping for {fwe.seconds} seconds...")
        await asyncio.sleep(fwe.seconds)
        async with save_lock:
            saved_message_ids.discard(message_key)
        raise
    except Exception:
        async with save_lock:
            saved_message_ids.discard(message_key)
        raise


async def save_media(message, sender_id):
    """Saves media locally and sends to Saved Messages (preserves original behavior)."""
    message_key = (message.chat_id, message.id)

    async with save_lock:
        if message_key in saved_message_ids or tracker.is_message_saved(message.chat_id, message.id):
            saved_message_ids.add(message_key)
            return
        saved_message_ids.add(message_key)

    telegram_file_id = extract_telegram_file_id(message)
    if telegram_file_id and tracker.is_file_id_saved(telegram_file_id):
        print(f"[Duplicate Skipped] Telegram File ID {telegram_file_id} already saved.")
        tracker.record_saved(
            chat_id=message.chat_id,
            message_id=message.id,
            telegram_file_id=telegram_file_id,
            caption=message.text,
        )
        return

    downloads_path.mkdir(parents=True, exist_ok=True)

    try:
        file_path = await client.download_media(message, file=str(downloads_path))
        if not file_path:
            raise RuntimeError("Telegram did not return a downloadable file")

        path_obj = Path(file_path)
        file_size = path_obj.stat().st_size
        file_name = path_obj.name
        file_hash = tracker.compute_sha256(file_path)

        if tracker.is_file_hash_saved(file_hash):
            print(f"[Duplicate Skipped] Content SHA-256 ({file_hash[:10]}...) already saved; skipping upload.")
            path_obj.unlink(missing_ok=True)
            tracker.record_saved(
                chat_id=message.chat_id,
                message_id=message.id,
                telegram_file_id=telegram_file_id,
                file_name=file_name,
                file_size=file_size,
                file_hash=file_hash,
                caption=message.text,
            )
            return

        caption_text = message.text or f"File saved from {sender_id}"
        await client.send_file(
            "me",
            file_path,
            caption=caption_text,
            force_document=force_document,
        )
        tracker.record_saved(
            chat_id=message.chat_id,
            message_id=message.id,
            telegram_file_id=telegram_file_id,
            file_name=file_name,
            file_size=file_size,
            file_hash=file_hash,
            caption=caption_text,
        )
        print(f"Saved media from {sender_id}: {file_path}")
        if cleanup_downloads:
            try:
                path_obj.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        async with save_lock:
            saved_message_ids.discard(message_key)
        raise


@client.on(events.NewMessage(incoming=True))
async def auto_save_timed_media(event):
    """Automatically saves incoming timed/self-destructing media."""
    if not auto_save_timed or not is_timed_media(event.message):
        return

    try:
        await save_media(event.message, event.sender_id)
    except Exception as err:
        print(f"Failed to auto-save timed media {event.chat_id}/{event.id}: {err}")


@client.on(events.NewMessage)
async def auto_forward_group_message(event):
    """Automatically intercepts and forwards/saves messages from monitored tutorial groups."""
    if not resolved_group_ids:
        return

    is_monitored = (
        event.chat_id in resolved_group_ids
        or (hasattr(event.message, "peer_id") and utils.get_peer_id(event.message.peer_id) in resolved_group_ids)
    )

    if not is_monitored:
        return

    # Skip service messages (user joined, pinned message notifications, etc.)
    if getattr(event.message, "action", None) is not None:
        return

    # Check media filter if enabled
    if forward_media_only and not event.message.media:
        return

    source_label = monitored_chat_names.get(event.chat_id)
    if not source_label:
        try:
            chat = await event.get_chat()
            source_label = getattr(chat, "title", str(event.chat_id))
        except Exception:
            source_label = str(event.chat_id)

    try:
        await forward_or_save_message(
            event.message,
            source_label=source_label,
            mode=forward_mode,
            force_doc=force_document,
            cleanup=cleanup_downloads,
        )
    except Exception as err:
        print(f"Failed to forward message {event.id} from group {event.chat_id}: {err}")


async def batch_save_chat_messages(
    client_instance,
    chat_entity,
    limit=None,
    media_only=False,
    progress_callback=None,
):
    """
    Iterates through a chat's history in chronological order (oldest to newest)
    and saves/forwards messages to Saved Messages.
    If limit is None, processes ALL matching messages in the chat.
    """
    entity = await client_instance.get_entity(chat_entity)
    title = getattr(entity, "title", getattr(entity, "username", str(chat_entity)))

    total_processed = 0
    total_saved = 0
    last_callback_time = asyncio.get_event_loop().time()

    if limit is not None and limit > 0:
        msgs = []
        async for msg in client_instance.iter_messages(entity, limit=limit):
            msgs.append(msg)
        msgs.reverse()

        for msg in msgs:
            total_processed += 1
            if msg.action or (media_only and not msg.media):
                continue
            try:
                saved = await forward_or_save_message(
                    msg,
                    source_label=title,
                    mode=forward_mode,
                    force_doc=force_document,
                    cleanup=cleanup_downloads,
                )
                if saved:
                    total_saved += 1
            except Exception as e:
                print(f"  Error saving message {msg.id}: {e}")

            now = asyncio.get_event_loop().time()
            if progress_callback and (now - last_callback_time >= 3.0):
                await progress_callback(total_processed, total_saved, False)
                last_callback_time = now
    else:
        # Stream from oldest to newest across ALL messages in the chat
        async for msg in client_instance.iter_messages(entity, reverse=True):
            total_processed += 1
            if msg.action or (media_only and not msg.media):
                continue
            try:
                saved = await forward_or_save_message(
                    msg,
                    source_label=title,
                    mode=forward_mode,
                    force_doc=force_document,
                    cleanup=cleanup_downloads,
                )
                if saved:
                    total_saved += 1
            except Exception as e:
                print(f"  Error saving message {msg.id}: {e}")

            now = asyncio.get_event_loop().time()
            if progress_callback and (now - last_callback_time >= 3.0):
                await progress_callback(total_processed, total_saved, False)
                last_callback_time = now

    if progress_callback:
        await progress_callback(total_processed, total_saved, True)

    return total_processed, total_saved, title


@client.on(
    events.NewMessage(
        pattern=rf"^(?:{re.escape(handler)}|\.id|\.chatid|\.stats|\.savehere|\.saveall|\.savegroup)(?:\s+(.*))?$"
    )
)
async def handle_userbot_command(event):
    """Handles manual commands (.saveit, .id, .stats, .savehere, .saveall, .savegroup) sent by the userbot owner."""
    if event.sender_id != your_user_id:
        return

    raw_text = event.raw_text.strip()
    parts = raw_text.split(maxsplit=2)
    cmd = parts[0].lower()
    args_str = raw_text[len(cmd):].strip()
    subparts = args_str.split()
    subcmd = subparts[0].lower() if subparts else ""

    # Command: .stats / <handler> stats
    if cmd in {".stats"} or (cmd == handler.lower() and subcmd in {"stats", "stat"}):
        st = tracker.get_stats()
        mb = st["total_bytes"] / (1024 * 1024)
        gb = mb / 1024
        size_str = f"{gb:.2f} GB" if gb >= 1.0 else f"{mb:.2f} MB"
        info_text = (
            f"📊 **Saveit Duplicate Tracker Statistics**\n"
            f"• **Archived Messages**: `{st['total_records']}`\n"
            f"• **Archived Media Size**: `{size_str}`\n"
            f"• **Unique File Hashes**: `{st['unique_hashes']}`\n"
            f"• **Database Engine**: `SQLite (WAL Mode)`"
        )
        await event.respond(info_text)
        return

    # Command: .id / .chatid / <handler> id
    if cmd in {".id", ".chatid"} or (cmd == handler.lower() and subcmd in {"id", "info", "chat"}):
        chat = await event.get_chat()
        chat_type = type(chat).__name__
        title = getattr(chat, "title", getattr(chat, "first_name", "Unknown"))
        username = f"@{chat.username}" if getattr(chat, "username", None) else "None"
        info_text = (
            f"📋 **Chat Information**\n"
            f"• **Title**: {title}\n"
            f"• **Chat ID**: `{event.chat_id}`\n"
            f"• **Username**: {username}\n"
            f"• **Type**: {chat_type}"
        )
        await event.respond(info_text)
        return

    # Command: .savehere [limit|all] / .saveall / <handler> here [limit|all]
    if cmd in {".savehere", ".saveall"} or (cmd == handler.lower() and subcmd in {"here", "all"}):
        limit = None if (cmd == ".saveall" or subcmd == "all") else 20
        limit_arg = subparts[1] if (cmd == handler.lower() and len(subparts) > 1) else (subparts[0] if (cmd in {".savehere", ".saveall"} and len(subparts) > 0) else "")
        if limit_arg.lower() in {"all", "full", "max", "0"}:
            limit = None
        elif limit_arg.isdigit():
            limit = int(limit_arg)

        label_scope = "ALL media" if limit is None else f"last {limit} messages"
        status = await event.respond(f"⏳ Scanning and saving {label_scope} from this chat to Saved Messages...")

        async def update_status(processed, saved, done):
            if done:
                return
            try:
                await status.edit(f"⏳ Processed {processed} messages... Saved {saved} media files so far.")
            except Exception:
                pass

        try:
            _, saved_count, title = await batch_save_chat_messages(
                client,
                event.chat_id,
                limit=limit,
                media_only=True if (cmd == ".saveall" or subcmd == "all" or limit is None) else forward_media_only,
                progress_callback=update_status,
            )
            await status.edit(f"✅ Finished! Saved {saved_count} media files from '{title}' to Saved Messages.")
            await asyncio.sleep(5)
            await status.delete()
            await event.delete()
        except Exception as err:
            await status.edit(f"❌ Failed to save media: {err}")
        return

    # Command: .savegroup <target> [limit|all] / <handler> group <target> [limit|all]
    if cmd == ".savegroup" or (cmd == handler.lower() and subcmd == "group"):
        target_str = subparts[1] if (cmd == handler.lower() and len(subparts) > 1) else (subparts[0] if (cmd == ".savegroup" and len(subparts) > 0) else "")
        limit_arg = subparts[2] if (cmd == handler.lower() and len(subparts) > 2) else (subparts[1] if (cmd == ".savegroup" and len(subparts) > 1) else "")

        limit = 20
        if limit_arg.lower() in {"all", "full", "max", "0"}:
            limit = None
        elif limit_arg.isdigit():
            limit = int(limit_arg)

        if not target_str:
            await event.respond("Usage: `.savegroup <chat_id_or_username> [limit|all]`")
            return

        label_scope = "ALL media" if limit is None else f"last {limit} messages"
        status = await event.respond(f"⏳ Scanning and saving {label_scope} from '{target_str}' to Saved Messages...")

        async def update_group_status(processed, saved, done):
            if done:
                return
            try:
                await status.edit(f"⏳ [{target_str}] Processed {processed} messages... Saved {saved} files.")
            except Exception:
                pass

        try:
            target_entity = int(target_str) if (target_str.startswith("-") or target_str.isdigit()) else target_str
            _, saved_count, title = await batch_save_chat_messages(
                client,
                target_entity,
                limit=limit,
                media_only=True if limit is None else forward_media_only,
                progress_callback=update_group_status,
            )
            await status.edit(f"✅ Finished! Saved {saved_count} files from '{title}' to Saved Messages.")
        except Exception as err:
            await status.respond(f"❌ Failed to save from group: {err}")
        return

    # Default .saveit behavior: Save replied media message
    if cmd == handler.lower() and not subcmd:
        status = await event.client.send_message(event.chat_id, "Downloading...")

        if not event.reply_to_msg_id:
            await status.edit("Reply to a message with media to save it.")
            return

        message = await event.get_reply_message()
        await event.delete()

        if not message or not message.media:
            await status.edit("No media found in the replied message.")
            return

        try:
            await save_media(message, str(message.sender_id))
        except Exception as err:
            await status.edit(f"Failed to save media: {err}")
            return

        await status.delete()


async def list_chats_and_exit(client_instance):
    """Fetches and displays all dialogs (groups and channels) with their Chat IDs."""
    print("\n" + "=" * 85)
    print("Listing joined groups and channels:")
    print(f"{'Type':<12} {'Chat ID':<20} {'Username':<25} {'Title'}")
    print("-" * 85)

    async for dialog in client_instance.iter_dialogs():
        entity = dialog.entity
        chat_type = type(entity).__name__
        if chat_type in {"Channel", "Chat"}:
            peer_id = utils.get_peer_id(entity)
            username = f"@{entity.username}" if getattr(entity, "username", None) else "-"
            title = getattr(entity, "title", "Untitled")
            is_megagroup = getattr(entity, "megagroup", False)
            display_type = (
                "Supergroup"
                if is_megagroup
                else ("Channel" if getattr(entity, "broadcast", False) else "Group")
            )
            print(f"{display_type:<12} {peer_id:<20} {username:<25} {title}")

    print("=" * 85)
    print("💡 Copy the Chat ID or Username into FORWARD_GROUP_IDS in .env to auto-forward.")
    print("=" * 85 + "\n")


async def perform_backfill(client_instance, target_ids, limit):
    """Backfills/forwards the last N messages from monitored groups on startup."""
    print(f"\n[Backfill] Starting catch-up for {len(target_ids)} group(s) (limit: {limit} messages each)...")
    for chat_id in target_ids:
        try:
            entity = await client_instance.get_entity(chat_id)
            title = getattr(entity, "title", str(chat_id))
            print(f"[Backfill] Fetching last {limit} messages from '{title}' ({chat_id})...")

            msgs = []
            async for msg in client_instance.iter_messages(entity, limit=limit):
                msgs.append(msg)
            msgs.reverse()

            saved_count = 0
            for msg in msgs:
                if msg.action or (forward_media_only and not msg.media):
                    continue
                try:
                    saved = await forward_or_save_message(
                        msg,
                        source_label=title,
                        mode=forward_mode,
                        force_doc=force_document,
                        cleanup=cleanup_downloads,
                    )
                    if saved:
                        saved_count += 1
                except Exception as e:
                    print(f"  [Backfill] Error saving message {msg.id}: {e}")
            print(f"[Backfill] Completed '{title}': saved {saved_count} message(s).")
        except Exception as e:
            print(f"[Backfill] Failed for target {chat_id}: {e}")
    print("[Backfill] Backfill catch-up process complete.\n")


async def main():
    global your_user_id

    async with client:
        if args.list_chats:
            await list_chats_and_exit(client)
            return

        me = await client.get_me()
        your_user_id = me.id

        if args.save_all_media:
            target = args.save_all_media
            target_entity = int(target) if (target.startswith("-") or target.isdigit()) else target
            print("=" * 65)
            print(f"Logged in as: {me.username or me.first_name} (User ID: {your_user_id})")
            print(f"Scanning and saving ALL media from '{target}' to Saved Messages...")
            print("=" * 65)

            async def cli_progress(processed, saved, done):
                if not done:
                    print(f"\r[Save-All] Scanned {processed} messages | Saved {saved} media files...", end="", flush=True)
                else:
                    print(f"\n[Save-All] Done! Successfully saved {saved} media files to Saved Messages.")

            try:
                await batch_save_chat_messages(
                    client,
                    target_entity,
                    limit=None,
                    media_only=True,
                    progress_callback=cli_progress,
                )
            except Exception as err:
                print(f"\n[Save-All] Failed: {err}")
            return

        existing_keys = tracker.load_all_message_keys()
        saved_message_ids.update(existing_keys)

        print("=" * 65)
        print(f"Logged in as: {me.username or me.first_name} (User ID: {your_user_id})")
        print(f"Command handler prefix: '{handler}'")
        print(f"Automatic timed-media saving: {'enabled' if auto_save_timed else 'disabled'}")
        print(f"Duplicate tracker: SQLite active ({len(existing_keys)} cached records)")

        targets = parse_group_targets(configured_groups_raw)
        if targets:
            print(f"\nGroup Auto-Forwarding ({len(targets)} configured target(s)):")
            print(f"  • Forward mode: {forward_mode}")
            print(
                f"  • Filter: {'Media only' if forward_media_only else 'All messages (media + text tutorials)'}"
            )
            print(f"  • Original uncompressed document: {force_document}")
            print(f"  • Cleanup local downloads: {cleanup_downloads}")
            await resolve_monitored_groups(client, targets)

            if backfill_limit > 0:
                await perform_backfill(client, list(resolved_group_ids), backfill_limit)
        else:
            print("\nGroup auto-forwarding: disabled (no FORWARD_GROUP_IDS configured)")
            print("Tip: set FORWARD_GROUP_IDS in .env or run with --forward-groups")

        print("=" * 65)
        print("Saveit is active and listening for messages. Press Ctrl+C to stop.")
        await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

