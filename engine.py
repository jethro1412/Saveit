import asyncio
import os
import random
import re
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

from tracker import FileTracker, extract_telegram_file_id

try:
    from telethon import TelegramClient, events, utils
    from telethon.errors import (
        ChatForwardsRestrictedError,
        FloodWaitError,
        SessionPasswordNeededError,
    )
except ImportError:
    TelegramClient = None
    events = None
    utils = None
    ChatForwardsRestrictedError = Exception
    FloodWaitError = Exception
    SessionPasswordNeededError = Exception


class RateLimiter:
    """
    Enforces randomized intervals between outgoing Telegram operations
    to stay compliant with rate limits and protect accounts from automated spam heuristics.
    """

    def __init__(self, delay: float = 1.5, jitter: bool = True):
        self.delay = max(0.0, float(delay))
        self.jitter = jitter
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def wait(self):
        """Pauses with randomized human-like jitter to guarantee safe intervals between calls."""
        if self.delay <= 0:
            return
        async with self._lock:
            extra_jitter = random.uniform(0.2, 0.8) if self.jitter else 0.0
            effective_delay = self.delay + extra_jitter

            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_call
            if elapsed < effective_delay:
                await asyncio.sleep(effective_delay - elapsed)
            self._last_call = asyncio.get_event_loop().time()

    def backoff_on_flood(self, penalty: float = 0.5):
        """Automatically slows down future requests if a FloodWait is encountered."""
        self.delay = round(self.delay + penalty, 2)


def parse_group_targets(raw_string: Optional[str]) -> List[Union[int, str]]:
    """Parses a comma-separated list of group IDs, usernames, or links into clean items."""
    if not raw_string:
        return []
    targets = []
    for item in str(raw_string).split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        if cleaned.startswith("https://t.me/"):
            cleaned = cleaned.replace("https://t.me/", "")
        try:
            targets.append(int(cleaned))
        except ValueError:
            targets.append(cleaned)
    return targets


def is_timed_media(message) -> bool:
    """Telegram exposes the self-destruct timer on the media object."""
    return bool(
        message
        and getattr(message, "media", None)
        and getattr(message.media, "ttl_seconds", None)
    )


def is_downloadable_file(message) -> bool:
    """Determines whether a message contains an actual downloadable media file."""
    if not message or not getattr(message, "media", None):
        return False
    media = message.media
    media_name = type(media).__name__
    if media_name in {
        "MessageMediaWebPage",
        "MessageMediaContact",
        "MessageMediaGeo",
        "MessageMediaGeoLive",
        "MessageMediaPoll",
        "MessageMediaDice",
        "MessageMediaGame",
        "MessageMediaInvoice",
        "MessageMediaUnsupported",
        "MessageMediaEmpty",
    }:
        return False
    if hasattr(media, "document") and media.document is not None:
        return True
    if hasattr(media, "photo") and media.photo is not None:
        return True
    return False


def get_media_size(message) -> Optional[int]:
    """Extracts file size in bytes from Telegram media metadata without downloading."""
    if not message or not getattr(message, "media", None):
        return None
    media = message.media
    if hasattr(media, "document") and media.document:
        return getattr(media.document, "size", None)
    return None


def cleanup_temp_file(file_path: Optional[Union[str, Path]]):
    """Safely removes a temporary downloaded file if it exists."""
    if not file_path:
        return
    try:
        p = Path(file_path)
        if p.exists() and p.is_file():
            p.unlink(missing_ok=True)
    except Exception:
        pass


def cleanup_empty_downloads_dir(downloads_path: Union[str, Path]):
    """Removes the downloads directory if it exists and contains no files."""
    try:
        p = Path(downloads_path)
        if p.exists() and p.is_dir():
            if not any(p.iterdir()):
                p.rmdir()
    except Exception:
        pass


def purge_downloads_folder(downloads_path: Union[str, Path]):
    """Purges any lingering files in downloads/ folder and removes the directory."""
    try:
        p = Path(downloads_path)
        if p.exists() and p.is_dir():
            for item in p.iterdir():
                try:
                    if item.is_file() or item.is_symlink():
                        item.unlink(missing_ok=True)
                    elif item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                except Exception:
                    pass
            p.rmdir()
    except Exception:
        pass


class SaveitEngine:
    """
    Core engine for Saveit Telegram Userbot.
    Can be controlled via CLI or embedded in the Windows GUI application.
    """

    def __init__(
        self,
        api_id: Optional[Union[int, str]] = None,
        api_hash: Optional[str] = None,
        session_name: str = "save",
        handler: str = ".saveit",
        auto_save_timed: bool = True,
        forward_groups: Optional[str] = "",
        forward_media_only: bool = False,
        forward_mode: str = "forward",
        force_document: bool = True,
        cleanup_downloads: bool = True,
        backfill_limit: Union[int, str] = 0,
        rate_limit_delay: float = 1.5,
        flood_sleep_threshold: int = 60,
        tracker_db: str = "saveit_tracker.db",
        downloads_dir: Union[str, Path] = "downloads",
        log_callback: Optional[Callable[[str, str], None]] = None,
        status_callback: Optional[Callable[[str, dict], None]] = None,
        auth_callbacks: Optional[dict] = None,
    ):
        self.api_id = int(api_id) if api_id else None
        self.api_hash = str(api_hash).strip() if api_hash else ""
        self.session_name = session_name
        self.handler = handler or ".saveit"
        self.auto_save_timed = auto_save_timed
        self.forward_groups_raw = forward_groups or ""
        self.forward_media_only = forward_media_only
        self.forward_mode = forward_mode or "forward"
        self.force_document = force_document
        self.cleanup_downloads = cleanup_downloads
        self.backfill_limit = backfill_limit
        self.rate_limit_delay = rate_limit_delay
        self.flood_sleep_threshold = flood_sleep_threshold
        self.tracker_db = tracker_db
        self.downloads_path = Path(downloads_dir)

        self.log_callback = log_callback
        self.status_callback = status_callback
        self.auth_callbacks = auth_callbacks or {}

        self.rate_limiter = RateLimiter(self.rate_limit_delay)
        self.tracker = FileTracker(self.tracker_db)
        self.client: Optional[TelegramClient] = None
        self.your_user_id: Optional[int] = None
        self.me = None

        self.saved_message_ids: Set[Tuple[int, int]] = set()
        self.save_lock = asyncio.Lock()
        self.resolved_group_ids: Set[int] = set()
        self.monitored_chat_names: Dict[int, str] = {}
        self.resolved_entities: Dict[int, object] = {}

        self.is_running = False
        self._stop_event = asyncio.Event()
        self.status = "STOPPED"

    def log(self, level: str, message: str):
        """Dispatches log events to stdout and UI listener."""
        ts = datetime.now().strftime("%H:%M:%S")
        prefix = f"[{ts}] [{level.upper()}]"
        print(f"{prefix} {message}")
        if self.log_callback:
            try:
                self.log_callback(level.lower(), f"{prefix} {message}")
            except Exception:
                pass

    def set_status(self, status: str, **kwargs):
        """Updates internal status and notifies listeners."""
        self.status = status
        if self.status_callback:
            try:
                self.status_callback(status, kwargs)
            except Exception:
                pass

    async def call_with_rate_limit(self, coro_fn, *args, **kwargs):
        """Calls an async Telegram operation with rate limiting and flood backoff."""
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            await self.rate_limiter.wait()
            try:
                return await coro_fn(*args, **kwargs)
            except FloodWaitError as fwe:
                safety_buffer = random.uniform(2.0, 5.0)
                wait_time = int(fwe.seconds + safety_buffer)
                self.rate_limiter.backoff_on_flood(0.5)
                self.log(
                    "warning",
                    f"Telegram FloodWait ({fwe.seconds}s). Sleeping {wait_time}s "
                    f"(rate delay increased to {self.rate_limiter.delay}s, retry {attempt}/{max_retries})...",
                )
                await asyncio.sleep(wait_time)
                if attempt == max_retries:
                    raise

    async def initialize_client(self):
        """Creates TelegramClient instance if not already created."""
        if TelegramClient is None:
            raise RuntimeError("telethon is not installed.")
        if not self.api_id or not self.api_hash:
            raise ValueError("API_ID and API_HASH are required.")

        if self.client is None:
            self.client = TelegramClient(
                self.session_name,
                self.api_id,
                self.api_hash,
                flood_sleep_threshold=self.flood_sleep_threshold,
            )

    async def authenticate(self):
        """
        Connects and authenticates with Telegram.
        Supports non-blocking GUI dialogs or standard interactive CLI inputs.
        """
        await self.initialize_client()
        self.set_status("CONNECTING", message="Connecting to Telegram servers...")
        self.log("info", "Connecting to Telegram...")
        await self.client.connect()

        if not await self.client.is_user_authorized():
            self.set_status("AWAITING_AUTH", message="Authentication required.")
            self.log("info", "Telegram authorization needed.")

            if self.auth_callbacks.get("request_phone"):
                phone_fn = self.auth_callbacks["request_phone"]
                code_fn = self.auth_callbacks["request_code"]
                pwd_fn = self.auth_callbacks.get("request_password")

                phone = await phone_fn()
                if not phone:
                    raise RuntimeError("Authentication cancelled: No phone number provided.")

                self.log("info", f"Requesting Telegram verification code for {phone}...")
                sent_code = await self.client.send_code_request(phone)

                code = await code_fn(phone)
                if not code:
                    raise RuntimeError("Authentication cancelled: No verification code provided.")

                try:
                    await self.client.sign_in(phone=phone, code=code)
                except SessionPasswordNeededError:
                    self.log("info", "Two-step verification (2FA) password required.")
                    if not pwd_fn:
                        raise RuntimeError("2FA password required but no password callback provided.")
                    password = await pwd_fn()
                    if not password:
                        raise RuntimeError("Authentication cancelled: 2FA password empty.")
                    await self.client.sign_in(password=password)
            else:
                # Fallback for CLI: Telethon default interactive prompt
                await self.client.start()

        self.me = await self.client.get_me()
        self.your_user_id = self.me.id
        user_display = self.me.username or f"{self.me.first_name or ''} {self.me.last_name or ''}".strip() or str(self.your_user_id)
        self.log("success", f"Authenticated as: {user_display} (User ID: {self.your_user_id})")
        self.set_status("AUTHENTICATED", user=user_display, user_id=self.your_user_id)

    async def resolve_monitored_groups(self, targets: Optional[List[Union[int, str]]] = None):
        """Resolves target groups/channels to peer IDs and titles."""
        if targets is None:
            targets = parse_group_targets(self.forward_groups_raw)

        if not targets:
            self.log("info", "No monitored groups configured.")
            return

        self.resolved_group_ids.clear()
        self.monitored_chat_names.clear()
        self.resolved_entities.clear()

        dialogs_cache = None
        for target in targets:
            entity = None
            try:
                entity = await self.client.get_entity(target)
            except Exception:
                pass

            if entity is None:
                if dialogs_cache is None:
                    try:
                        dialogs_cache = await self.client.get_dialogs()
                    except Exception as err:
                        self.log("warning", f"Could not fetch dialogs cache: {err}")
                        dialogs_cache = []

                for d in dialogs_cache:
                    d_peer_id = utils.get_peer_id(d.entity)
                    d_raw_id = getattr(d.entity, "id", None)
                    d_username = getattr(d.entity, "username", None)

                    if isinstance(target, str) and d_username and target.lstrip("@").lower() == d_username.lower():
                        entity = d.entity
                        break
                    if target in (d_peer_id, d_raw_id):
                        entity = d.entity
                        break
                    if isinstance(target, int):
                        target_str = str(target)
                        if target_str.startswith("-100"):
                            without_100 = int(f"-{target_str[4:]}")
                            if d_peer_id == without_100 or d_raw_id == without_100:
                                entity = d.entity
                                break
                        elif target_str.startswith("-"):
                            with_100 = int(f"-100{target_str[1:]}")
                            if d_peer_id == with_100 or d_raw_id == with_100:
                                entity = d.entity
                                break
                        else:
                            cand_1 = int(f"-100{target}")
                            cand_2 = int(f"-{target}")
                            if d_peer_id in (cand_1, cand_2) or d_raw_id == target:
                                entity = d.entity
                                break

            if entity is not None:
                peer_id = utils.get_peer_id(entity)
                self.resolved_group_ids.add(peer_id)
                if hasattr(entity, "id"):
                    self.resolved_group_ids.add(entity.id)
                title = getattr(entity, "title", getattr(entity, "username", str(peer_id)))
                self.monitored_chat_names[peer_id] = title
                self.resolved_entities[peer_id] = entity
                self.log("info", f"Monitored Target: {title} (ID: {peer_id})")
            else:
                if isinstance(target, int):
                    self.resolved_group_ids.add(target)
                    if target > 0:
                        self.resolved_group_ids.add(int(f"-100{target}"))
                    self.monitored_chat_names[target] = f"Group {target}"
                    self.log("warning", f"Target ID {target} resolved speculatively (not in joined dialogs).")
                else:
                    self.log("warning", f"Could not resolve target '{target}'. Ensure you have joined it.")

    async def forward_or_save_message(
        self,
        message,
        source_label: Optional[str] = None,
        mode: str = "forward",
        force_doc: bool = True,
        cleanup: bool = True,
    ) -> bool:
        """Forwards or copies a message to Saved Messages ('me')."""
        message_key = (message.chat_id, message.id)

        async with self.save_lock:
            if message_key in self.saved_message_ids or self.tracker.is_message_saved(message.chat_id, message.id):
                self.saved_message_ids.add(message_key)
                return False
            self.saved_message_ids.add(message_key)

        telegram_file_id = extract_telegram_file_id(message)
        if telegram_file_id and self.tracker.is_file_id_saved(telegram_file_id):
            self.log("info", f"[Duplicate Skipped] File ID {telegram_file_id} already archived.")
            self.tracker.record_saved(
                chat_id=message.chat_id,
                message_id=message.id,
                telegram_file_id=telegram_file_id,
                caption=message.text,
            )
            return False

        try:
            if mode == "forward":
                try:
                    await self.call_with_rate_limit(self.client.forward_messages, "me", message)
                    self.tracker.record_saved(
                        chat_id=message.chat_id,
                        message_id=message.id,
                        telegram_file_id=telegram_file_id,
                        caption=message.text,
                    )
                    self.log("success", f"[Forwarded] Message {message.id} from {source_label or message.chat_id} to Saved Messages.")
                    return True
                except ChatForwardsRestrictedError:
                    self.log("info", f"[Restricted] Chat {message.chat_id} has forward protection; copying media directly...")
                except Exception as e:
                    self.log("info", f"[Fallback] Forward failed ({e}); falling back to download/upload...")

            if is_downloadable_file(message):
                media_size = get_media_size(message)
                if media_size and media_size > 2000 * 1024 * 1024:
                    self.log(
                        "warning",
                        f"[File Too Large] Message {message.id} media is {media_size / (1024 * 1024):.1f} MB (exceeds 2GB limit). Skipped.",
                    )
                    return False

                self.downloads_path.mkdir(parents=True, exist_ok=True)
                file_path = None
                try:
                    file_path = await self.call_with_rate_limit(
                        self.client.download_media, message, file=str(self.downloads_path)
                    )
                    if not file_path:
                        self.log("warning", f"[Download Warning] Could not download media for message {message.id}.")
                        return False

                    path_obj = Path(file_path)
                    file_size = path_obj.stat().st_size
                    file_name = path_obj.name
                    file_hash = self.tracker.compute_sha256(file_path)

                    if file_hash and self.tracker.is_file_hash_saved(file_hash):
                        self.log("info", f"[Duplicate Skipped] Content hash {file_hash[:10]}... already saved.")
                        self.tracker.record_saved(
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
                    if len(caption) > 1024:
                        full_text = caption
                        caption = caption[:1020] + "..."

                    await self.call_with_rate_limit(
                        self.client.send_file,
                        "me",
                        file_path,
                        caption=caption if caption else None,
                        force_document=force_doc,
                    )

                    if full_text:
                        await self.call_with_rate_limit(self.client.send_message, "me", full_text)

                    self.tracker.record_saved(
                        chat_id=message.chat_id,
                        message_id=message.id,
                        telegram_file_id=telegram_file_id,
                        file_name=file_name,
                        file_size=file_size,
                        file_hash=file_hash,
                        caption=caption,
                    )
                    self.log("success", f"[Saved Media] Message {message.id} from {source_label or message.chat_id}: {file_name}")
                    return True
                finally:
                    if cleanup:
                        cleanup_temp_file(file_path)
                        cleanup_empty_downloads_dir(self.downloads_path)

            elif message.text:
                chat_title = source_label or str(message.chat_id)
                header = f"📚 **[{chat_title}]**\n\n"
                await self.call_with_rate_limit(self.client.send_message, "me", header + message.text)
                self.tracker.record_saved(
                    chat_id=message.chat_id,
                    message_id=message.id,
                    file_size=len(message.text),
                    caption=message.text,
                )
                self.log("success", f"[Saved Text] Message {message.id} from {chat_title} to Saved Messages.")
                return True

            return False

        except FloodWaitError as fwe:
            self.log("warning", f"[Rate Limit] FloodWait encountered: sleeping {fwe.seconds}s...")
            await asyncio.sleep(fwe.seconds)
            async with self.save_lock:
                self.saved_message_ids.discard(message_key)
            raise
        except Exception as err:
            async with self.save_lock:
                self.saved_message_ids.discard(message_key)
            self.log("error", f"Failed to forward/save message {message.id}: {err}")
            return False

    async def save_media(self, message, sender_id: Union[int, str]):
        """Downloads self-destructing/timed media and uploads to Saved Messages."""
        message_key = (message.chat_id, message.id)

        async with self.save_lock:
            if message_key in self.saved_message_ids or self.tracker.is_message_saved(message.chat_id, message.id):
                self.saved_message_ids.add(message_key)
                return
            self.saved_message_ids.add(message_key)

        telegram_file_id = extract_telegram_file_id(message)
        if telegram_file_id and self.tracker.is_file_id_saved(telegram_file_id):
            self.log("info", f"[Duplicate Skipped] Telegram File ID {telegram_file_id} already saved.")
            self.tracker.record_saved(
                chat_id=message.chat_id,
                message_id=message.id,
                telegram_file_id=telegram_file_id,
                caption=message.text,
            )
            return

        if not is_downloadable_file(message):
            self.log("info", f"[Save Skipped] Message {message.id} does not contain downloadable media.")
            return

        media_size = get_media_size(message)
        if media_size and media_size > 2000 * 1024 * 1024:
            self.log("warning", f"[File Too Large] Message {message.id} media is {media_size / (1024 * 1024):.1f} MB. Skipping.")
            return

        self.downloads_path.mkdir(parents=True, exist_ok=True)
        file_path = None

        try:
            file_path = await self.call_with_rate_limit(
                self.client.download_media, message, file=str(self.downloads_path)
            )
            if not file_path:
                self.log("warning", f"[Download Warning] Telegram could not download media for message {message.id}.")
                return

            path_obj = Path(file_path)
            file_size = path_obj.stat().st_size
            file_name = path_obj.name
            file_hash = self.tracker.compute_sha256(file_path)

            if file_hash and self.tracker.is_file_hash_saved(file_hash):
                self.log("info", f"[Duplicate Skipped] Content SHA-256 ({file_hash[:10]}...) already saved.")
                self.tracker.record_saved(
                    chat_id=message.chat_id,
                    message_id=message.id,
                    telegram_file_id=telegram_file_id,
                    file_name=file_name,
                    file_size=file_size,
                    file_hash=file_hash,
                    caption=message.text,
                )
                return

            caption_text = message.text or f"Disappearing media saved from {sender_id}"
            await self.call_with_rate_limit(
                self.client.send_file,
                "me",
                file_path,
                caption=caption_text,
                force_document=self.force_document,
            )
            self.tracker.record_saved(
                chat_id=message.chat_id,
                message_id=message.id,
                telegram_file_id=telegram_file_id,
                file_name=file_name,
                file_size=file_size,
                file_hash=file_hash,
                caption=caption_text,
            )
            self.log("success", f"[Saved Timed Media] Preserved media from {sender_id}: {file_name}")

        except Exception as err:
            async with self.save_lock:
                self.saved_message_ids.discard(message_key)
            self.log("error", f"Failed to save timed media {message.id}: {err}")
            raise
        finally:
            if self.cleanup_downloads:
                cleanup_temp_file(file_path)
                cleanup_empty_downloads_dir(self.downloads_path)

    async def batch_save_chat_messages(
        self,
        chat_entity: Union[int, str],
        limit: Optional[int] = None,
        media_only: bool = False,
        progress_callback: Optional[Callable[[int, int, bool], None]] = None,
    ) -> Tuple[int, int, str]:
        """Iterates through a chat's history chronologically and saves matching messages."""
        entity = await self.client.get_entity(chat_entity)
        title = getattr(entity, "title", getattr(entity, "username", str(chat_entity)))

        total_processed = 0
        total_saved = 0
        last_callback_time = asyncio.get_event_loop().time()

        if limit is not None and limit > 0:
            msgs = []
            async for msg in self.client.iter_messages(entity, limit=limit):
                msgs.append(msg)
            msgs.reverse()

            for msg in msgs:
                if self._stop_event.is_set():
                    break
                total_processed += 1
                if msg.action or (media_only and not is_downloadable_file(msg)):
                    continue
                try:
                    saved = await self.forward_or_save_message(
                        msg,
                        source_label=title,
                        mode=self.forward_mode,
                        force_doc=self.force_document,
                        cleanup=self.cleanup_downloads,
                    )
                    if saved:
                        total_saved += 1
                        if total_saved % 25 == 0:
                            breather = round(random.uniform(3.0, 6.0), 1)
                            self.log("info", f"[Anti-Ban] Processed 25 items; cooling breather for {breather}s...")
                            await asyncio.sleep(breather)
                    else:
                        await asyncio.sleep(0.005)
                except Exception as e:
                    self.log("error", f"Error saving message {msg.id}: {e}")

                now = asyncio.get_event_loop().time()
                if progress_callback and (now - last_callback_time >= 1.5):
                    if asyncio.iscoroutinefunction(progress_callback):
                        await progress_callback(total_processed, total_saved, False)
                    else:
                        progress_callback(total_processed, total_saved, False)
                    last_callback_time = now
        else:
            async for msg in self.client.iter_messages(entity, reverse=True):
                if self._stop_event.is_set():
                    break
                total_processed += 1
                if msg.action or (media_only and not is_downloadable_file(msg)):
                    continue
                try:
                    saved = await self.forward_or_save_message(
                        msg,
                        source_label=title,
                        mode=self.forward_mode,
                        force_doc=self.force_document,
                        cleanup=self.cleanup_downloads,
                    )
                    if saved:
                        total_saved += 1
                        if total_saved % 25 == 0:
                            breather = round(random.uniform(3.0, 6.0), 1)
                            self.log("info", f"[Anti-Ban] Processed 25 items; cooling breather for {breather}s...")
                            await asyncio.sleep(breather)
                    else:
                        await asyncio.sleep(0.005)
                except Exception as e:
                    self.log("error", f"Error saving message {msg.id}: {e}")

                now = asyncio.get_event_loop().time()
                if progress_callback and (now - last_callback_time >= 1.5):
                    if asyncio.iscoroutinefunction(progress_callback):
                        await progress_callback(total_processed, total_saved, False)
                    else:
                        progress_callback(total_processed, total_saved, False)
                    last_callback_time = now

        if progress_callback:
            if asyncio.iscoroutinefunction(progress_callback):
                await progress_callback(total_processed, total_saved, True)
            else:
                progress_callback(total_processed, total_saved, True)

        if self.cleanup_downloads:
            cleanup_empty_downloads_dir(self.downloads_path)

        return total_processed, total_saved, title

    async def perform_backfill(self, target_ids: List[Union[int, str]], limit: Union[int, str]):
        """Backfills messages from monitored groups on startup."""
        limit_val = (
            None
            if str(limit).lower() in {"all", "full", "max"}
            else (int(limit) if str(limit).isdigit() else 20)
        )
        label = "ALL historical messages" if limit_val is None else f"last {limit_val} messages"
        self.log("info", f"[Backfill] Starting catch-up for {len(target_ids)} group(s) ({label})...")

        for chat_id in target_ids:
            if self._stop_event.is_set():
                break
            try:
                entity = self.resolved_entities.get(chat_id)
                if not entity:
                    entity = await self.client.get_entity(chat_id)
                title = getattr(entity, "title", str(chat_id))
                self.log("info", f"[Backfill] Scanning {label} from '{title}' ({chat_id})...")

                async def backfill_progress(processed, saved, done):
                    if done:
                        self.log("success", f"[Backfill] '{title}': Completed! Saved {saved} item(s).")

                await self.batch_save_chat_messages(
                    entity,
                    limit=limit_val,
                    media_only=self.forward_media_only,
                    progress_callback=backfill_progress,
                )
            except Exception as e:
                self.log("error", f"[Backfill] Failed for target {chat_id}: {e}")
        self.log("info", "[Backfill] Catch-up process complete.")

    async def get_dialogs_list(self) -> List[Dict[str, Union[str, int]]]:
        """Fetches joined dialogs for chat explorer in GUI."""
        if not self.client or not self.client.is_connected():
            await self.authenticate()

        results = []
        async for dialog in self.client.iter_dialogs():
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
                results.append(
                    {
                        "id": peer_id,
                        "title": title,
                        "username": username,
                        "type": display_type,
                    }
                )
        return results

    def register_handlers(self):
        """Attaches Telethon event listeners for incoming messages and userbot commands."""

        @self.client.on(events.NewMessage(incoming=True))
        async def handle_timed(event):
            if not self.auto_save_timed or not is_timed_media(event.message):
                return
            try:
                await self.save_media(event.message, event.sender_id)
            except Exception as err:
                self.log("error", f"Failed to auto-save timed media {event.chat_id}/{event.id}: {err}")

        @self.client.on(events.NewMessage)
        async def handle_group(event):
            if not self.resolved_group_ids:
                return

            is_monitored = (
                event.chat_id in self.resolved_group_ids
                or (hasattr(event.message, "peer_id") and utils.get_peer_id(event.message.peer_id) in self.resolved_group_ids)
            )
            if not is_monitored:
                return

            if getattr(event.message, "action", None) is not None:
                return

            if self.forward_media_only and not is_downloadable_file(event.message):
                return

            source_label = self.monitored_chat_names.get(event.chat_id)
            if not source_label:
                try:
                    chat = await event.get_chat()
                    source_label = getattr(chat, "title", str(event.chat_id))
                except Exception:
                    source_label = str(event.chat_id)

            try:
                await self.forward_or_save_message(
                    event.message,
                    source_label=source_label,
                    mode=self.forward_mode,
                    force_doc=self.force_document,
                    cleanup=self.cleanup_downloads,
                )
            except Exception as err:
                self.log("error", f"Failed to forward message {event.id} from group {event.chat_id}: {err}")

        @self.client.on(
            events.NewMessage(
                pattern=rf"^(?:{re.escape(self.handler)}|\.id|\.chatid|\.stats|\.rate|\.savehere|\.saveall|\.savegroup)(?:\s+(.*))?$"
            )
        )
        async def handle_cmd(event):
            if event.sender_id != self.your_user_id:
                return

            raw_text = event.raw_text.strip()
            parts = raw_text.split(maxsplit=2)
            cmd = parts[0].lower()
            args_str = raw_text[len(cmd):].strip()
            subparts = args_str.split()
            subcmd = subparts[0].lower() if subparts else ""

            if cmd in {".rate"} or (cmd == self.handler.lower() and subcmd in {"rate", "delay"}):
                new_rate = subparts[1] if (cmd == self.handler.lower() and len(subparts) > 1) else (subparts[0] if (cmd == ".rate" and len(subparts) > 0) else "")
                if new_rate:
                    try:
                        rate_val = float(new_rate)
                        if rate_val < 0:
                            raise ValueError
                        self.rate_limiter.delay = rate_val
                        await event.respond(f"⏱️ Rate limiter delay updated to `{rate_val}s` per action.")
                    except ValueError:
                        await event.respond("Usage: `.rate <seconds>` (e.g. `.rate 1.5` or `.rate 2.0`)")
                else:
                    await event.respond(
                        f"⏱️ **Rate Limiter Settings**\n"
                        f"• **Current Delay**: `{self.rate_limiter.delay}s` per action\n"
                        f"• Change with: `.rate <seconds>` (e.g. `.rate 2.0`)"
                    )
                return

            if cmd in {".stats"} or (cmd == self.handler.lower() and subcmd in {"stats", "stat"}):
                st = self.tracker.get_stats()
                mb = st["total_bytes"] / (1024 * 1024)
                gb = mb / 1024
                size_str = f"{gb:.2f} GB" if gb >= 1.0 else f"{mb:.2f} MB"
                info_text = (
                    f"📊 **Saveit Duplicate Tracker Statistics**\n"
                    f"• **Archived Messages**: `{st['total_records']}`\n"
                    f"• **Archived Media Size**: `{size_str}`\n"
                    f"• **Unique File Hashes**: `{st['unique_hashes']}`\n"
                    f"• **Rate Limit Delay**: `{self.rate_limiter.delay}s`\n"
                    f"• **Database Engine**: `SQLite (WAL Mode)`"
                )
                await event.respond(info_text)
                return

            if cmd in {".id", ".chatid"} or (cmd == self.handler.lower() and subcmd in {"id", "info", "chat"}):
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

            if cmd in {".savehere", ".saveall"} or (cmd == self.handler.lower() and subcmd in {"here", "all"}):
                limit = None if (cmd == ".saveall" or subcmd == "all") else 20
                limit_arg = subparts[1] if (cmd == self.handler.lower() and len(subparts) > 1) else (subparts[0] if (cmd in {".savehere", ".saveall"} and len(subparts) > 0) else "")
                if limit_arg.lower() in {"all", "full", "max", "0"}:
                    limit = None
                elif limit_arg.isdigit():
                    limit = int(limit_arg)

                label_scope = "ALL media" if limit is None else f"last {limit} messages"
                status_msg = await event.respond(f"⏳ Scanning and saving {label_scope} from this chat to Saved Messages...")

                async def update_status(processed, saved, done):
                    if done:
                        return
                    try:
                        await status_msg.edit(f"⏳ Processed {processed} messages... Saved {saved} media files so far.")
                    except Exception:
                        pass

                try:
                    _, saved_count, title = await self.batch_save_chat_messages(
                        event.chat_id,
                        limit=limit,
                        media_only=True if (cmd == ".saveall" or subcmd == "all" or limit is None) else self.forward_media_only,
                        progress_callback=update_status,
                    )
                    await status_msg.edit(f"✅ Finished! Saved {saved_count} media files from '{title}' to Saved Messages.")
                    await asyncio.sleep(5)
                    await status_msg.delete()
                    await event.delete()
                except Exception as err:
                    await status_msg.edit(f"❌ Failed to save media: {err}")
                return

            if cmd == ".savegroup" or (cmd == self.handler.lower() and subcmd == "group"):
                target_str = subparts[1] if (cmd == self.handler.lower() and len(subparts) > 1) else (subparts[0] if (cmd == ".savegroup" and len(subparts) > 0) else "")
                limit_arg = subparts[2] if (cmd == self.handler.lower() and len(subparts) > 2) else (subparts[1] if (cmd == ".savegroup" and len(subparts) > 1) else "")

                limit = 20
                if limit_arg.lower() in {"all", "full", "max", "0"}:
                    limit = None
                elif limit_arg.isdigit():
                    limit = int(limit_arg)

                if not target_str:
                    await event.respond("Usage: `.savegroup <chat_id_or_username> [limit|all]`")
                    return

                label_scope = "ALL media" if limit is None else f"last {limit} messages"
                status_msg = await event.respond(f"⏳ Scanning and saving {label_scope} from '{target_str}' to Saved Messages...")

                async def update_group_status(processed, saved, done):
                    if done:
                        return
                    try:
                        await status_msg.edit(f"⏳ [{target_str}] Processed {processed} messages... Saved {saved} files.")
                    except Exception:
                        pass

                try:
                    target_entity = int(target_str) if (target_str.startswith("-") or target_str.isdigit()) else target_str
                    _, saved_count, title = await self.batch_save_chat_messages(
                        target_entity,
                        limit=limit,
                        media_only=True if limit is None else self.forward_media_only,
                        progress_callback=update_group_status,
                    )
                    await status_msg.edit(f"✅ Finished! Saved {saved_count} files from '{title}' to Saved Messages.")
                except Exception as err:
                    await event.respond(f"❌ Failed to save from group: {err}")
                return

            if cmd == self.handler.lower() and not subcmd:
                status_msg = await self.client.send_message(event.chat_id, "Downloading...")
                if not event.reply_to_msg_id:
                    await status_msg.edit("Reply to a message with media to save it.")
                    return

                message = await event.get_reply_message()
                await event.delete()

                if not message or not is_downloadable_file(message):
                    await status_msg.edit("No downloadable media found in the replied message.")
                    return

                try:
                    await self.save_media(message, str(message.sender_id))
                except Exception as err:
                    await status_msg.edit(f"Failed to save media: {err}")
                    return

                await status_msg.delete()

    async def start(self):
        """Initializes, authenticates, registers handlers, and starts monitoring."""
        if self.cleanup_downloads:
            cleanup_empty_downloads_dir(self.downloads_path)

        await self.authenticate()
        existing_keys = self.tracker.load_all_message_keys()
        self.saved_message_ids.update(existing_keys)

        self.register_handlers()
        self.log("info", f"Loaded {len(existing_keys)} duplicate tracking records from SQLite.")

        targets = parse_group_targets(self.forward_groups_raw)
        if targets:
            self.log("info", f"Resolving {len(targets)} configured group targets...")
            await self.resolve_monitored_groups(targets)

            raw_bf = str(self.backfill_limit).strip().lower()
            if raw_bf in {"all", "full", "max"} or (raw_bf.isdigit() and int(raw_bf) > 0):
                backfill_targets = list(self.resolved_entities.keys()) if self.resolved_entities else list(self.resolved_group_ids)
                await self.perform_backfill(backfill_targets, self.backfill_limit)

        self.is_running = True
        self._stop_event.clear()
        self.set_status("RUNNING", message="Listening for Telegram messages...")
        self.log("success", "Saveit is active and running.")

    async def run_until_stopped(self):
        """Listening loop with automatic reconnection shield."""
        retry_delay = 3
        while not self._stop_event.is_set():
            try:
                # Wait either until disconnected or until stop is requested
                disconnected_task = asyncio.create_task(self.client.run_until_disconnected())
                stop_task = asyncio.create_task(self._stop_event.wait())
                done, pending = await asyncio.wait(
                    [disconnected_task, stop_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()

                if self._stop_event.is_set():
                    break

                if disconnected_task in done:
                    self.set_status("RECONNECTING", message="Disconnected from Telegram. Reconnecting...")
                    self.log("warning", f"Connection dropped. Reconnecting in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
                    if not self.client.is_connected():
                        await self.client.connect()
                    self.log("success", "Reconnected successfully to Telegram.")
                    self.set_status("RUNNING", message="Listening for Telegram messages...")
                    retry_delay = 3

            except asyncio.CancelledError:
                break
            except Exception as unhandled:
                self.log("error", f"Crash shield recovered from unexpected error: {unhandled}")
                traceback.print_exc()
                await asyncio.sleep(5)

    async def stop(self):
        """Gracefully halts the engine and disconnects."""
        self.log("info", "Stopping Saveit engine...")
        self.is_running = False
        self._stop_event.set()

        if self.client and self.client.is_connected():
            try:
                await self.client.disconnect()
            except Exception:
                pass

        if self.cleanup_downloads:
            cleanup_empty_downloads_dir(self.downloads_path)

        self.set_status("STOPPED", message="Engine stopped.")
        self.log("info", "Saveit engine stopped.")
