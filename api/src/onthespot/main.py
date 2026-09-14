import asyncio
import json
import mimetypes
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter
from pydantic_core import ValidationError

# librespot currently ships protobuf files generated for the compatibility
# runtime. Keep source launches aligned with Docker, which sets this variable
# in its runtime environment, so local Windows starts do not fail on import.
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")


from .accounts import FillAccountPool, get_account_token
from .api.apple_music import apple_music_add_account
from .api.bandcamp import bandcamp_add_account
from .api.crunchyroll import crunchyroll_add_account
from .api.deezer import deezer_add_account
from .api.generic import generic_add_account
from .api.qobuz import qobuz_add_account
from .api.registry import SERVICE_SEARCH_FUNCTIONS
from .api.soundcloud import soundcloud_add_account
from .api.spotify import (
    add_spotify_zeroconf_login,
    spotify_connect_status,
    spotify_get_search_results,
    spotify_new_session,
)
from .api.tidal import tidal_add_account_pt1, tidal_add_account_pt2
from .api.youtube_music import youtube_music_add_account
from .basemodels import *
from .constants import _SEARCH_FILTER_TYPES, _SEARCH_SERVICE_FILTER_KEYS, ItemStatus
from .downloader import DownloadWorker, RetryWorker
from .export_locations import (
    default_export_directory,
    playlist_backup_directory,
    set_default_export_directory,
    set_playlist_backup_directory,
    write_export_file,
)
from .library import (
    export_index,
    import_index,
    verify_file,
)
from .otsconfig import config
from .parse_item import get_search_results
from .parsingworker import ParsingWorker
from .runtimedata import (
    account_pool,
    download_paused,
    download_queue,
    download_queue_lock,
    get_logger,
    get_rate_limit_state,
    notification_hook,
    parsing,
    parsing_lock,
    pending,
    pending_lock,
    progress_hook,
    subscribe_websocket,
    unsubscribe_websocket,
)
from .statistics import clear_history, export_history, import_history
from .updater import (
    check_for_updates,
)
from .utils import open_item, retry_single_item
from .youtube_auth import (
    managed_youtube_cookie_path,
    store_youtube_cookie_file,
    validate_youtube_browser,
    validate_youtube_cookie_file,
    youtube_auth_status,
)

log_level = int(os.environ.get("LOG_LEVEL", 20))
logger = get_logger("gui")
# ---------------------------------------------------------------------------
# ONTHESPOT BOOTSTRAP
# ---------------------------------------------------------------------------

# define workers here to allow app to access them
# but start/stop them on lifespan events
parsing_worker = ParsingWorker()
downloadworker = DownloadWorker()
# spotifymirrorworker = MirrorSpotifyPlayback()
retryworker = RetryWorker()
fillaccountpool = FillAccountPool()
_spotify_companion_pairings: dict[str, float] = {}
_spotify_companion_pairing_lock = threading.Lock()
_SPOTIFY_COMPANION_PAIRING_TTL = 10 * 60


##ONTHESPOT BRIDGE FUNCTIONS
def add_spotify_account():
    """
    Initiates the process to add a Spotify account.
    """
    logger.info("Add spotify account clicked")
    login_worker = threading.Thread(target=add_spotify_account_worker)
    login_worker.daemon = True
    login_worker.start()


def add_spotify_account_worker():
    """
    Worker function to add a Spotify account.
    """
    try:
        if spotify_new_session():
            config.set("active_account_number", len(account_pool))
            config.save()
        else:
            logger.info("Spotify account already exists or sign-in was cancelled")
    except Exception as exc:
        logger.exception("Spotify Connect sign-in worker failed")
        notification_hook(
            "Spotify Connect sign-in failed",
            f"Could not start the local Spotify Connect worker: {exc}",
        )


def add_tidal_account():
    """
    Initiates the process to add a Tidal account.
    """
    logger.info("Add Tidal account clicked")
    device_code, verification_url = tidal_add_account_pt1()
    logger.info(
        "Login Service Started head to <a style='color: #6495ed;' href='https://%s'>https://%s</a> to continue.",
        verification_url,
        verification_url,
    )
    notification_hook(title="Continue Login - Go to the URL", url=f"https://{verification_url}")
    login_worker = threading.Thread(target=add_tidal_account_worker, args=(device_code,))
    login_worker.daemon = True
    login_worker.start()


def add_tidal_account_worker(device_code):
    """
    Worker function to complete the Tidal account addition.

    :param device_code: Device code required for Tidal login.
    """
    if tidal_add_account_pt2(device_code):
        config.set("active_account_number", len(account_pool))
        config.save()
        fillaccountpool.stop()
        time.sleep(1)
        relogin()
        notification_hook("Login Complete", "Refresh the page")
    else:
        logger.info("Account Already Exists")


def search(search_term, search_filters: dict | None = None) -> None:
    """
    Parse the url and add the item to the pending queue.
    """

    results = get_search_results(search_term)
    return results


def search_service_catalogs(
    search_term: str,
    search_filters: dict[str, Any] | None = None,
    selected_services: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search every currently authenticated worker service.

    Provider search functions are blocking and independent, so they run in a
    small bounded thread pool.  A failing provider is isolated from the other
    workers and simply contributes no results.
    """
    query = search_term.strip()
    if not query:
        return []

    filters = search_filters or {}
    if selected_services is None and isinstance(filters.get("services"), list):
        selected_services = [str(value) for value in filters["services"]]
    allowed_services = (
        {service for service in selected_services if service in SERVICE_SEARCH_FUNCTIONS}
        if selected_services is not None
        else None
    )
    if allowed_services == set():
        return []
    selected_filter_keys = {key for key in _SEARCH_FILTER_TYPES if filters.get(key, True)}
    if not selected_filter_keys:
        return []

    services = list(
        dict.fromkeys(
            str(account.get("service", ""))
            for account in account_pool
            if account.get("service") in SERVICE_SEARCH_FUNCTIONS
            and (allowed_services is None or account.get("service") in allowed_services)
        )
    )
    jobs: list[tuple[str, Any, Any]] = []
    for service in services:
        try:
            token = get_account_token(service)
        except (IndexError, KeyError, TypeError) as exc:
            logger.warning("Unable to obtain a %s search token: %s", service, exc)
            continue
        if token is False:
            continue
        jobs.append((service, token, SERVICE_SEARCH_FUNCTIONS[service]))

    def run_provider(
        job: tuple[str, Any, Any],
    ) -> tuple[str, list[dict[str, Any]], set[str]]:
        service, token, search_function = job
        service_filter_keys = selected_filter_keys.intersection(
            _SEARCH_SERVICE_FILTER_KEYS.get(service, selected_filter_keys)
        )
        if not service_filter_keys:
            return service, [], set()

        provider_types: list[str] = []
        accepted_result_types: set[str] = set()
        for key in service_filter_keys:
            for item_type in _SEARCH_FILTER_TYPES[key]:
                if item_type not in provider_types:
                    provider_types.append(item_type)
                accepted_result_types.add(item_type)
        if "podcasts" in service_filter_keys:
            accepted_result_types.update({"podcast", "podcast_episode"})

        try:
            raw = search_function(token, query, provider_types)
            return (
                service,
                raw if isinstance(raw, list) else [],
                accepted_result_types,
            )
        except Exception as exc:  # A provider outage must not blank every service.
            logger.warning("%s catalogue search failed: %s", service, exc)
            return service, [], accepted_result_types

    if not jobs:
        return []
    worker_count = min(4, len(jobs))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ots-search") as executor:
        provider_results = list(executor.map(run_provider, jobs))

    # Keep each provider's own relevance ordering, but interleave provider
    # batches below.  Appending an entire provider at once made a healthy
    # multi-service search look broken: SoundCloud could occupy the first 40
    # cards while equally valid Spotify results were hidden several screens
    # below it.
    provider_batches: list[list[dict[str, Any]]] = []
    seen: set[tuple[str, str, str]] = set()
    for service, items, accepted_result_types in provider_results:
        batch: list[dict[str, Any]] = []
        for item in items:
            item_type = str(item.get("item_type") or "track")
            if item_type not in accepted_result_types:
                continue
            item_id = str(item.get("item_id") or item.get("id") or "").strip()
            item_url = str(item.get("item_url") or item.get("url") or "").strip()
            if not item_id or not item_url:
                continue
            identity = (service, item_type, item_id)
            if identity in seen:
                continue
            seen.add(identity)
            batch.append(
                {
                    "id": f"{service}:{item_type}:{item_id}",
                    "item_id": item_id,
                    "item_service": str(item.get("item_service") or service),
                    "item_type": item_type,
                    "name": str(item.get("item_name") or item.get("name") or "Untitled"),
                    "artist": str(item.get("item_by") or item.get("artist") or ""),
                    "album": str(item.get("item_album") or item.get("album") or ""),
                    "thumbnail": str(item.get("item_thumbnail_url") or item.get("thumbnail") or ""),
                    "url": item_url,
                    "item_url": item_url,
                }
            )

        if batch:
            provider_batches.append(batch)

    results: list[dict[str, Any]] = []
    longest_batch = max((len(batch) for batch in provider_batches), default=0)
    for item_index in range(longest_batch):
        for batch in provider_batches:
            if item_index < len(batch):
                results.append(batch[item_index])
    return results


def relogin():
    """
    Reloads the account pool to refresh accounts.
    """

    global fillaccountpool
    previous_worker = fillaccountpool
    if previous_worker is not None and previous_worker.is_running:
        previous_worker.stop()
    fillaccountpool = FillAccountPool()
    account_pool.clear()
    fillaccountpool.start()


# ---------------------------------------------------------------------------
# FASTAPI INIT
# ---------------------------------------------------------------------------
# START ONTHESPOT WORKERS HERE
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Context manager for FastAPI application lifecycle events.

    :param app: The FastAPI application instance.
    """
    logger.info("OnTheSpot Version: %s", config.get("version"))
    parsing_worker.start()
    downloadworker.start()
    if config.get("enable_retry_worker"):
        retryworker.start()

    fillaccountpool.start()

    logger.info("Initializing...")

    yield

    parsing_worker.stop()
    downloadworker.stop()

    fillaccountpool.stop()
    # stop_spotify_connect_service()

    logger.info("Application shutdown")


app = FastAPI(
    title="OnTheSpot API",
    version=str(config.get("version") or "2.0.0"),
    lifespan=lifespan,
)

# Production requests are same-origin because FastAPI serves the UI. Vite's
# local development origins remain enabled, and operators can add explicit
# cross-origin frontends with a comma-separated ONTHESPOT_CORS_ORIGINS value.
cors_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]
cors_origins.extend(
    origin.strip() for origin in os.environ.get("ONTHESPOT_CORS_ORIGINS", "").split(",") if origin.strip()
)
cors_origins = list(dict.fromkeys(cors_origins))

# Register correct MIME types for frontend files
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/wasm", ".wasm")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Enabled Self-serving static frontend files from FastAPI. This could work for single binary deployment
# but we need to implement better path handling and a separate composer file
# app.frontend("/", directory="dist")


##PROFILES ENDPOINTS
@app.get("/profiles")
async def get_download_profiles():
    return {
        "active": config.get("active_download_profile", ""),
        "profiles": config.get("download_profiles", []) or [],
    }


@app.post("/profiles")
async def save_download_profile(profile: DownloadProfile):
    profiles = list(config.get("download_profiles", []) or [])
    clean_id = re.sub(r"[^a-z0-9_-]+", "-", profile.id.lower()).strip("-")
    if not clean_id:
        clean_id = f"profile-{uuid.uuid4().hex[:8]}"
    value = profile.model_dump()
    value["id"] = clean_id
    value["format"] = profile.format.lstrip(".").lower()
    if value["format"] not in {"mp3", "flac", "m4a", "opus", "ogg", "wav"}:
        return {"success": False, "error": "Unsupported audio format"}
    value["bitrate"] = int(profile.bitrate or 320)
    value["download_path"] = os.path.abspath(profile.download_path) if profile.download_path else ""
    profiles = [entry for entry in profiles if entry.get("id") != clean_id]
    profiles.append(value)
    config.set("download_profiles", profiles)
    if not config.get("active_download_profile"):
        config.set("active_download_profile", clean_id)
    config.save()
    return value


@app.post("/profiles/active")
async def set_active_download_profile(profile: ActiveProfile):
    profiles = config.get("download_profiles", []) or []
    if not any(entry.get("id") == profile.profile_id for entry in profiles):
        return {"success": False, "error": "Unknown profile"}
    config.set("active_download_profile", profile.profile_id)
    config.save()
    return {"success": True, "active": profile.profile_id}


@app.delete("/profiles/{profile_id}")
async def delete_download_profile(profile_id: str):
    profiles = [entry for entry in (config.get("download_profiles", []) or []) if entry.get("id") != profile_id]
    if not profiles:
        return {"success": False, "error": "At least one profile is required"}
    config.set("download_profiles", profiles)
    if config.get("active_download_profile") == profile_id:
        config.set("active_download_profile", profiles[0].get("id"))
    config.save()
    return {"success": True}


##SEARCH ENDPOINT
@app.post("/query/url")
async def query_url(q: str | None = None, filters: dict | None = None):
    """
    Endpoint to perform a URL-based search.

    :param q: The search term.
    :param filters: Optional dictionary of filters for the search.
    :return: Search results.
    """
    result = None
    if q:
        result = search(q, filters)
    return result


@app.post("/search")
async def search_catalog(q: str, filters: dict[str, Any] | None = None):
    """Search all configured worker catalogues without enqueueing anything."""
    raise NotImplementedError
    return await run_in_threadpool(search_service_catalogs, q, filters)


@app.get("/catalog/spotify")
async def search_spotify_catalog(q: str, types: str = "track"):
    """Search the Spotify public catalogue for the browse view."""
    raise NotImplementedError
    content_types = [
        value for value in types.split(",") if value in {"track", "album", "artist", "playlist", "show", "episode"}
    ]
    if not content_types:
        content_types = ["track"]

    token = None
    try:
        if account_pool:
            token = get_account_token("spotify")
    except (IndexError, KeyError, TypeError):
        token = None

    # Client-credentials catalog searches do not need the paired user session,
    # but a paired session remains the fallback when no override is configured.
    if token is False and not config.get("spotify_webapi_override_client_id"):
        return []

    raw_results = spotify_get_search_results(
        token,
        q.strip(),
        content_types,
        search_prefix="",
    )
    return [
        {
            "id": item.get("item_id", ""),
            "item_id": item.get("item_id", ""),
            "item_service": item.get("item_service", "spotify"),
            "item_type": item.get("item_type", "track"),
            "name": item.get("item_name", ""),
            "artist": item.get("item_by", ""),
            "thumbnail": item.get("item_thumbnail_url", ""),
            "url": item.get("item_url", ""),
            "item_url": item.get("item_url", ""),
        }
        for item in raw_results
        if item.get("item_id") and item.get("item_url")
    ]


@app.post("/spotify/mirror")
async def mirror_spotify(state: bool = False):
    """Enable or disable automatic downloads of the currently playing Spotify track."""
    raise NotImplementedError
    config.set("mirror_spotify_playback", state)
    config.save()
    worker_action = spotifymirrorworker.start if state else spotifymirrorworker.stop
    await asyncio.to_thread(worker_action)
    return {"enabled": state}


## QUEUES ENDPOINTS
def _public_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    """Return stable, serializable queue fields without worker internals."""
    fields = (
        "local_id",
        "item_service",
        "service",
        "item_type",
        "item_name",
        "name",
        "item_by",
        "artist",
        "item_status",
        "progress",
        "item_progress",
        "queue_position",
        "priority",
        "playlist_name",
    )
    snapshot = {key: item.get(key) for key in fields if key in item}
    if "item_status" in snapshot:
        status = snapshot["item_status"]
        snapshot["item_status"] = str(getattr(status, "value", status))
    return snapshot


@app.get("/queue/downloads")
async def query_download_queue():
    """
    Endpoint to get the current download queue.

    :return: Sorted dictionary of items in the download queue.
    """

    def sort_key(entry):
        local_id, item = entry
        position = item.get("queue_position", 10**9)
        priority = item.get("priority", 0)
        try:
            numeric_id = int(local_id)
        except (TypeError, ValueError):
            numeric_id = 10**9
        return (position, -priority, numeric_id)

    with download_queue_lock:
        return dict(sorted(download_queue.items(), key=sort_key))


@app.get("/queue/downloads/state")
async def query_download_state():
    with download_queue_lock:
        active = [
            item
            for item in download_queue.values()
            if item.get("item_status") in (ItemStatus.DOWNLOADING, ItemStatus.PAUSED)
        ]
        return {
            "paused": download_paused.is_set(),
            "active": len(active),
            "speed": sum(float(item.get("download_speed_bps", 0) or 0) for item in active),
            "eta_seconds": max(
                [item.get("eta_seconds") or 0 for item in active],
                default=0,
            ),
        }


@app.post("/queue/downloads/reorder")
async def reorder_download_queue(order: QueueOrder):
    requested = [str(local_id) for local_id in order.local_ids]
    with download_queue_lock:
        for position, local_id in enumerate(requested):
            if local_id in download_queue:
                download_queue[local_id]["queue_position"] = position
                download_queue[local_id]["priority"] = len(requested) - position

    pending_items = pending.get_items()
    pending_by_id = {str(item.get("local_id")): item for item in pending_items}
    ordered_pending = [pending_by_id[local_id] for local_id in requested if local_id in pending_by_id]
    ordered_ids = {str(item.get("local_id")) for item in ordered_pending}
    ordered_pending.extend(item for item in pending_items if str(item.get("local_id")) not in ordered_ids)
    pending.replace_items(ordered_pending)
    return {"success": True, "order": requested}


@app.post("/queue/downloads/batch")
async def batch_download_queue_action(batch: QueueBatch):
    """Apply one control to several queue items at once."""
    action = batch.action.strip().lower()
    allowed = {"pause", "resume", "retry", "cancel", "delete", "priority", "profile"}
    if action not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported queue batch action")
    if action == "profile" and not batch.profile_id:
        raise HTTPException(status_code=400, detail="A profile is required")
    if action == "profile" and not any(
        entry.get("id") == batch.profile_id for entry in (config.get("download_profiles", []) or [])
    ):
        raise HTTPException(status_code=400, detail="Unknown download profile")

    selected: list[dict] = []
    retry_items: list[dict] = []
    changed = 0
    with download_queue_lock:
        for local_id in {str(value) for value in batch.local_ids}:
            item = download_queue.get(local_id)
            if item is None:
                continue
            selected.append(item)
            if action == "pause":
                item["_pause_requested"] = True
                item["item_status"] = ItemStatus.PAUSED
            elif action == "resume":
                item["_pause_requested"] = False
                if not item.get("_active_download"):
                    item["item_status"] = ItemStatus.WAITING
            elif action == "cancel":
                item["_pause_requested"] = False
                item["_manual_cancelled"] = True
                item["available"] = False
                item["item_status"] = ItemStatus.CANCELLED
                item["error"] = "Cancelled by the user."
            elif action == "delete":
                if item.get("_active_download"):
                    item["_pause_requested"] = False
                    item["_manual_cancelled"] = True
                    item["_discarded"] = True
                    item["available"] = False
                    item["item_status"] = ItemStatus.CANCELLED
                    item["error"] = "Deleted by the user."
                else:
                    item["_discarded"] = True
                    download_queue.pop(local_id, None)
            elif action == "retry":
                retry_items.append(item)
            elif action == "priority":
                item["priority"] = int(batch.priority or 0)
            elif action == "profile":
                profile = next(
                    entry
                    for entry in (config.get("download_profiles", []) or [])
                    if entry.get("id") == batch.profile_id
                )
                item["profile_id"] = profile.get("id")
                item["profile_name"] = profile.get("name", profile.get("id", "Default"))
            changed += 1

        if action == "priority":
            waiting = sorted(
                (item for item in download_queue.values() if item.get("item_status") == ItemStatus.WAITING),
                key=lambda item: (
                    -int(item.get("priority", 0) or 0),
                    int(item.get("queue_position", 10**9) or 10**9),
                ),
            )
            for position, item in enumerate(waiting):
                item["queue_position"] = position

    for item in retry_items:
        retry_single_item(item)
    for item in selected:
        if action in {"pause", "resume", "cancel"}:
            progress_hook(item, int(item.get("progress", 0) or 0), item.get("item_status"))

    if action == "resume" and selected:
        notification_hook("Downloads resumed", f"Resumed {len(selected)} selected item(s).")
    return {"success": True, "changed": changed, "action": action}


@app.post("/queue/downloads/verify")
async def verify_download_queue(request: QueueVerify):
    """Check completed queue files and optionally put corrupt ones back in the queue."""
    with download_queue_lock:
        candidates = [
            item
            for item in download_queue.values()
            if item.get("item_status") in (ItemStatus.DOWNLOADED, ItemStatus.ALREADY_EXISTS)
            and (not request.local_ids or item.get("local_id") in request.local_ids)
        ]

    corrupt: list[dict] = []
    for item in candidates:
        path = item.get("file_path") or ""
        try:
            result = verify_file(path)
        except ValueError as exc:
            result = {"path": path, "valid": False, "reason": str(exc), "size": 0}
        if not result.get("valid"):
            item["item_status"] = ItemStatus.FAILED
            item["progress"] = 0
            item["error"] = f"Verification failed: {result.get('reason', 'invalid file')}"
            item["_stats_recorded"] = False
            corrupt.append(item)

    if request.retry:
        for item in corrupt:
            retry_single_item(item)
    return {
        "checked": len(candidates),
        "healthy": len(candidates) - len(corrupt),
        "corrupt": len(corrupt),
        "retried": len(corrupt) if request.retry else 0,
        "items": [{"local_id": item.get("local_id"), "error": item.get("error", "")} for item in corrupt],
    }


@app.post("/queue/pending/action")
async def pending_action(lid: str, action: str):
    """
    Endpoint to perform actions on a specific item in the pending queue.

    :param lid: Local ID of the item.
    :param action: Action to perform (e.g., retry, cancel, delete).
    :return: Boolean indicating success or failure of the action.
    """

    for item in pending.get_items():
        if item["local_id"] == lid:
            match action:
                case "cancel":
                    pending.remove(item)
                    return True
                case _:
                    return False


@app.get("/queue/downloads/clear")
async def remove_queue_items(status: str = "Completed"):
    """
    Endpoint to clear items from the download queue based on their status.

    :param status: Status of items to be removed. Defaults to "Completed".
    """
    with download_queue_lock:
        if status.lower() == "all":
            removed_count = len(download_queue)
            download_queue.clear()
            return removed_count

        normalized_status = status.lower()
        completed_status = normalized_status in {"completed", "downloaded"}
        failed_status = normalized_status in {"failed", "errors", "error"}
        failure_values = {
            ItemStatus.FAILED,
            ItemStatus.CANCELLED,
            ItemStatus.UNAVAILABLE,
        }
        keys_to_remove = [
            key
            for key, item in download_queue.items()
            if item["item_status"] == status
            or (completed_status and item["item_status"] == "Already Exists")
            or (failed_status and item["item_status"] in failure_values)
        ]
        for key in keys_to_remove:
            download_queue.pop(key, None)
        return len(keys_to_remove)


@app.post("/queue/downloads/action")
async def queue_action(lid: str, action: str):
    """
    Endpoint to perform actions on a specific item in the download queue.

    :param lid: Local ID of the item.
    :param action: Action to perform (e.g., retry, cancel, delete).
    :return: Boolean indicating success or failure of the action.
    """

    retry_item = None
    changed_item = None
    notification = None
    result_status = None
    with download_queue_lock:
        for key, item in download_queue.items():
            if item["local_id"] == lid:
                match action:
                    case "retry":
                        # need to retry later to free the lock
                        retry_item = item
                    case "cancel":
                        item["_pause_requested"] = False
                        item["_manual_cancelled"] = True
                        item["available"] = False
                        item["item_status"] = ItemStatus.CANCELLED
                        item["error"] = "Cancelled by the user."
                        changed_item = item
                        notification = (
                            "Download cancelled",
                            item.get("name", "The current track"),
                        )
                        result_status = ItemStatus.CANCELLED
                    case "delete":
                        if item.get("_active_download"):
                            item["_pause_requested"] = False
                            item["_manual_cancelled"] = True
                            item["_discarded"] = True
                            item["available"] = False
                            item["item_status"] = ItemStatus.CANCELLED
                            item["error"] = "Deleted by the user."
                            changed_item = item
                            notification = (
                                "Download removed",
                                item.get("name", "The current track"),
                            )
                            result_status = ItemStatus.CANCELLED
                        else:
                            item["_discarded"] = True
                            download_queue.pop(key)
                            result_status = ItemStatus.DELETED
                    case _:
                        return {"success": False, "error": "Unknown queue action."}
                break
    if changed_item is not None:
        raw_progress = changed_item.get("progress", changed_item.get("item_progress", 0))
        try:
            current_progress = int(float(raw_progress or 0))
        except (TypeError, ValueError):
            current_progress = 0
        # Publish the terminal state immediately. The worker will observe the
        # same state and stop at its next cancellation checkpoint.
        progress_hook(changed_item, current_progress, ItemStatus.CANCELLED)
        if notification is not None:
            notification_hook(*notification)
        return {"success": True, "action": action, "status": result_status}
    if retry_item is not None:
        retry_single_item(retry_item)
        return {"success": True, "action": action, "status": ItemStatus.WAITING}
    if result_status == ItemStatus.DELETED:
        return {"success": True, "action": action, "status": result_status}
    return {"success": False, "error": "Queue item not found."}


@app.get("/queue/downloads/retryfailed")
async def retry_failed_items():
    """
    Endpoint to retry all failed or cancelled items in the download queue.
    """
    retryable_statuses = {
        ItemStatus.CANCELLED,
        ItemStatus.FAILED,
        ItemStatus.UNAVAILABLE,
    }
    with download_queue_lock:
        found_items = [
            item
            for item in download_queue.values()
            if (
                item.get("item_status") in retryable_statuses
                and (item.get("available", True) or item.get("_manual_cancelled"))
                and not item.get("_discarded")
            )
        ]
        for item in found_items:
            item["available"] = True
            item.pop("_manual_cancelled", None)
            item["item_status"] = ItemStatus.WAITING
            item["error"] = ""
            item["_stats_recorded"] = False
            item["queue_preloaded"] = None
            item["retry_count"] = int(item.get("retry_count", 0) or 0) + 1
            download_queue.pop(item["local_id"], None)

    for item in found_items:
        pending.put_nowait(item)
    return {"success": True, "count": len(found_items)}


@app.get("/queue/downloads/download")
async def download_file(lid):
    """
    Endpoint to download a file by its local ID.

    :param lid: Local ID of the item to download.
    :return: File response containing the downloaded file.
    """
    file_path = None
    with download_queue_lock:
        item = download_queue.get(str(lid))
        if item is not None:
            file_path = item.get("file_path")
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Downloaded file not found")
    file_name = os.path.basename(file_path)
    media_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    return FileResponse(file_path, media_type=media_type, filename=file_name)


@app.get("/queue/pending")
async def query_pending_queue():
    """
    Endpoint to get the current pending queue.

    :return: Public snapshot of items waiting to enter the download queue.
    """
    with pending_lock:
        items = [_public_queue_item(item) for item in pending.get_items()]
    return {"items": items, "count": len(items)}


@app.get("/queue/parsing")
async def query_parsing_queue():
    """
    Endpoint to get the current parsing queue.

    :return: Public snapshot of items currently being parsed.
    """
    with parsing_lock:
        items = [_public_queue_item(item) for item in parsing.values()]
    return {"items": items, "count": len(items)}


## CONFIG ENDPOINTS
@app.get("/config/get")
async def get_config():
    """
    Endpoint to get the current configuration.

    :return: Current configuration settings.
    """
    return config.as_dict()


@app.patch("/config/set", status_code=status.HTTP_200_OK)
async def update_partial_config(patch_data: dict):
    model_fields = AppSettings.model_fields
    result = None

    for key, value in patch_data.items():
        # 1. Verify the key exists in AppSettings
        if key not in model_fields:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Field '{key}' is not a valid setting.",
            )

        # 2. Get the field's declared type and validate the value against it
        field_info = model_fields[key]
        adapter = TypeAdapter(field_info.annotation)

        try:
            # Validates type, handles lists/dicts/optionals, and coerces if needed
            validated_value = adapter.validate_python(value)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid value for field '{key}': {exc.errors()}",
            )

        # 3. Store the validated value
        result = config.set(key, validated_value)

    return result


@app.post("/config/save")
async def save_config():
    """
    Endpoint to save the current configuration.

    :return: Result of saving the configuration.
    """
    return config.save()


@app.get("/exports/location")
async def get_export_location():
    return {"directory": default_export_directory()}


@app.post("/exports/location")
async def update_export_location(payload: dict[str, Any]):
    try:
        return {"directory": set_default_export_directory(str(payload.get("directory") or ""))}
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/exports/playlist-backup-location")
async def get_playlist_backup_location():
    return {"directory": playlist_backup_directory()}


@app.post("/exports/playlist-backup-location")
async def update_playlist_backup_location(payload: dict[str, Any]):
    try:
        return {"directory": set_playlist_backup_directory(str(payload.get("directory") or ""))}
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/exports/write")
async def write_text_export(payload: dict[str, Any]):
    raise NotImplementedError

    filename = (
        re.sub(r"[^A-Za-z0-9._-]+", "-", str(payload.get("filename") or "export.txt")).strip(".-") or "export.txt"
    )
    stem, extension = os.path.splitext(filename)
    try:
        path = write_export_file(
            stem or "export",
            extension or ".txt",
            str(payload.get("content") or ""),
            str(payload.get("directory") or ""),
        )
        return {"success": True, "path": path}
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/exports/open-folder")
async def open_export_folder(payload: dict[str, Any]):
    try:
        directory = playlist_backup_directory() if payload.get("playlist_backups") else default_export_directory()
        os.makedirs(directory, exist_ok=True)
        open_item(directory)
        return {"success": True, "path": directory}
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/config/reset")
async def reset_config():
    """
    Endpoint to reset the configuration to default settings.

    :return: Result of resetting the configuration.
    """
    config.reset()
    return config.as_dict()


def _exportable_config() -> dict:
    exported = config.as_dict()
    if exported.get("spotify_webapi_override_client_secret_configured"):
        exported["spotify_webapi_override_client_secret"] = "<redacted>"
    return exported


@app.get("/config/export")
async def export_config():
    return JSONResponse(content=_exportable_config())


@app.post("/config/export-file")
async def export_config_file(payload: dict[str, Any]):
    raise NotImplementedError
    try:
        path = write_export_file(
            "onthespot-config",
            "json",
            json.dumps(_exportable_config(), indent=2, ensure_ascii=False),
            str(payload.get("directory") or ""),
        )
        return {"success": True, "path": path}
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/config/import")
async def import_config(payload: dict):
    raise NotImplementedError
    ## Guard the imported entry using the AppSettings interface
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Configuration must be a JSON object")
    protected = {"_ffmpeg_bin_path", "_log_file", "_cache_dir"}
    for key, value in payload.items():
        if key in protected or key.startswith("_"):
            continue
        if key == "spotify_webapi_override_client_secret" and value in {
            "",
            "<redacted>",
            None,
        }:
            continue
        if key == "accounts" and isinstance(value, list):
            # Accounts contain authentication material and are deliberately
            # not imported from a redacted export.
            continue
        config.set(key, value)
    config.save()
    return {"success": True, "config": _exportable_config()}


def _safe_queue_snapshot() -> list[dict]:
    with download_queue_lock:
        snapshot = []
        for item in download_queue.values():
            safe = {
                key: value
                for key, value in item.items()
                if not key.startswith("_") and key not in {"token", "credentials", "login"}
            }
            snapshot.append(safe)
        return snapshot


@app.get("/statistics")
async def download_statistics():
    raise NotImplementedError


@app.post("/statistics/clear")
async def clear_download_statistics():
    clear_history()
    return {"success": True}


@app.get("/backup/export")
async def export_backup():
    return JSONResponse(
        content={
            "version": 1,
            "created_at": int(time.time()),
            "settings": _exportable_config(),
            "download_profiles": config.get("download_profiles", []) or [],
            "queue": _safe_queue_snapshot(),
            "queue_history": export_history(),
            "library_metadata": export_index(),
        }
    )


@app.post("/backup/export-file")
async def export_backup_file(payload: dict[str, Any]):
    raise NotImplementedError
    backup = payload.get("backup") if isinstance(payload.get("backup"), dict) else payload
    try:
        path = write_export_file(
            "onthespot-backup",
            "json",
            json.dumps(backup, indent=2, ensure_ascii=False),
            str(payload.get("directory") or ""),
        )
        return {"success": True, "path": path}
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/backup/import")
async def import_backup(payload: dict):
    raise NotImplementedError
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Backup must be a JSON object")
    settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
    protected = {"_ffmpeg_bin_path", "_log_file", "_cache_dir"}
    for key, value in settings.items():
        if key in protected or str(key).startswith("_"):
            continue
        if key in {"accounts", "spotify_webapi_override_client_secret"}:
            continue
        config.set(key, value)
    config.save()
    history_restored = (
        import_history(payload.get("queue_history")) if payload.get("queue_history") is not None else False
    )
    library_restored = (
        import_index(payload.get("library_metadata")) if payload.get("library_metadata") is not None else False
    )
    return {
        "success": True,
        "history_restored": history_restored,
        "library_restored": library_restored,
        "config": _exportable_config(),
    }


@app.get("/config/version")
async def check_version():
    # Keep this legacy boolean endpoint for the existing diagnostics view.
    status = await run_in_threadpool(check_for_updates)
    try:
        status = not bool(status.get("update_available", False))
        return status
    except Exception:
        return status


@app.get("/updates/check")
async def updates_check(force: bool = False):
    """Return release metadata."""
    return await run_in_threadpool(check_for_updates)


# ACCOUNTS ENDPOINTS
@app.get("/accounts/youtube-auth/status")
async def get_youtube_auth_status():
    """Report whether the selected YouTube session source is usable."""
    return await run_in_threadpool(youtube_auth_status)


@app.post("/accounts/youtube-auth/upload")
async def upload_youtube_auth(cookies: UploadFile):
    """Store an uploaded Netscape cookies.txt file in private app data."""
    contents = await cookies.read((5 * 1024 * 1024) + 1)
    try:
        destination = await run_in_threadpool(store_youtube_cookie_file, contents)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not store the cookies file: {exc}") from exc

    config.set("youtube_auth_mode", "cookie_file")
    config.set("youtube_cookies_browser", "")
    config.set("youtube_cookies_file", str(destination))
    config.save()
    notification_hook(
        "YouTube cookies saved",
        "The uploaded YouTube cookies file is available to downloads.",
    )
    return await run_in_threadpool(youtube_auth_status)


@app.post("/accounts/youtube-auth")
async def configure_youtube_auth(authentication: YouTubeAuthentication):
    """Save explicit local-only yt-dlp authentication settings for YouTube."""
    allowed_browsers = {
        "chrome",
        "chromium",
        "edge",
        "firefox",
        "brave",
        "opera",
        "vivaldi",
    }
    mode = authentication.mode.strip().lower()
    if mode not in {"none", "browser", "cookie_file"}:
        raise HTTPException(status_code=400, detail="Unsupported YouTube authentication mode")

    browser = (authentication.browser or "").strip().lower()
    cookie_file = (authentication.cookie_file or "").strip()
    if mode == "browser" and browser not in allowed_browsers:
        raise HTTPException(status_code=400, detail="Choose a supported browser profile")
    if mode == "browser":
        try:
            await run_in_threadpool(validate_youtube_browser, browser)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if mode == "cookie_file":
        path = Path(cookie_file).expanduser()
        try:
            await run_in_threadpool(validate_youtube_cookie_file, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        cookie_file = str(path)
    if mode == "none":
        managed_cookie_file = managed_youtube_cookie_path()
        try:
            managed_cookie_file.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove managed YouTube cookies file: %s", managed_cookie_file)

    config.set("youtube_auth_mode", mode)
    config.set("youtube_cookies_browser", browser if mode == "browser" else "")
    config.set("youtube_cookies_file", cookie_file if mode == "cookie_file" else "")
    config.save()
    status = "disabled" if mode == "none" else "configured"
    notification_hook("YouTube authentication updated", f"YouTube session authentication is {status}.")
    return {"success": True, **(await run_in_threadpool(youtube_auth_status))}


@app.post("/accounts/add")
async def add_account(service: str, item: AccountData | None = None):
    """
    Endpoint to add an account for a specific service.

    :param service: The name of the service (e.g., "spotify", "tidal").
    :param item: Optional data required for adding the account.
    :return: Boolean indicating success or failure of account addition.
    """
    found = False
    match service:
        case "generic":
            generic_add_account()
            found = True
        case "spotify":
            add_spotify_account()
            # found = True
        case "tidal":
            add_tidal_account()
            # found = True
        case "applemusic":
            apple_music_add_account(item.token)
            found = True
        case "youtube":
            youtube_music_add_account()
            found = True
        case "bandcamp":
            bandcamp_add_account()
            found = True
        case "qobuz":
            qobuz_add_account(item.username, item.token)
            found = True
        case "deezer":
            deezer_add_account(item.token)
            found = True
        case "soundcloud":
            soundcloud_add_account(oauth_token=item.token)
            found = True
        case "crunchyroll":
            crunchyroll_add_account(item.username, item.token)
            # found = True
        case _:
            raise NotImplementedError
    if found:
        await run_in_threadpool(relogin)
    notification_hook(title="Logging in...")
    return found


@app.post("/accounts/spotify/companion/pair")
async def create_spotify_companion_pairing():
    """Create a short-lived token for a local Spotify companion."""
    now = time.time()
    token = secrets.token_urlsafe(32)
    with _spotify_companion_pairing_lock:
        _spotify_companion_pairings.clear()
        _spotify_companion_pairings[token] = now + _SPOTIFY_COMPANION_PAIRING_TTL
    return {
        "pairing_token": token,
        "expires_at": int(now + _SPOTIFY_COMPANION_PAIRING_TTL),
        "expires_in": _SPOTIFY_COMPANION_PAIRING_TTL,
        "device_name": "OnTheSpot Companion",
    }


@app.post("/accounts/spotify/companion/complete")
async def complete_spotify_companion_pairing(payload: SpotifyCompanionLogin):
    """Accept one Spotify ZeroConf login from a paired local companion."""
    token = payload.pairing_token.strip()
    with _spotify_companion_pairing_lock:
        expires_at = _spotify_companion_pairings.pop(token, None)
    if not expires_at or expires_at < time.time():
        raise HTTPException(status_code=401, detail="The companion pairing code is invalid or expired")

    if not add_spotify_zeroconf_login(payload.login):
        raise HTTPException(
            status_code=409,
            detail="This Spotify account is already configured or the login payload is invalid",
        )

    await run_in_threadpool(relogin)
    notification_hook(
        "Spotify account connected",
        "The Spotify companion delivered a new account login.",
    )
    return {"success": True}


@app.post("/accounts/remove")
async def remove_account(luuid: str):
    """
    Endpoint to remove an account by its UUID.

    :param luuid: UUID of the account to be removed.
    :return: Boolean indicating success or failure of account removal.
    """
    index = None
    for idx, item in enumerate(account_pool):
        if item["uuid"] == luuid:
            index = idx
    if index is None:
        return None
    del account_pool[index]
    accounts = config.get("accounts").copy()
    del accounts[index]
    config.set("accounts", accounts)
    config.save()
    return True


@app.get("/accounts/get")
async def get_accounts():
    """
    Endpoint to get the list of all accounts.

    :return: List of accounts.
    """
    # librespot sessions and HTTP clients are present in the in-memory account
    # objects but are not JSON serializable (and should never be exposed to the
    # browser). Return only the account identity/status fields the UI needs.
    safe_accounts = []
    for account in account_pool:
        if not isinstance(account, dict):
            continue
        safe_accounts.append(
            {
                "uuid": account.get("uuid", ""),
                "service": account.get("service", ""),
                "active": bool(account.get("active", True)),
                # Never expose token/cookie-like login fields to the browser.
                # A Spotify/Tidal account name is safe to display; service
                # tokens are intentionally omitted.
                "username": account.get("username", "")
                if account.get("service") in {"spotify", "tidal", "qobuz"}
                else "",
            }
        )
    return safe_accounts


@app.get("/accounts/health")
async def get_account_health():
    configured = [
        account
        for account in (config.get("accounts", []) or [])
        if isinstance(account, dict) and account.get("active", True)
    ]
    authenticated_services = {
        account.get("service") for account in account_pool if isinstance(account, dict) and account.get("active", True)
    }
    configured_services = {account.get("service") for account in configured}
    missing_services = sorted(service for service in configured_services if service not in authenticated_services)
    spotify_online = "spotify" in authenticated_services
    return {
        "healthy": bool(configured) and not missing_services,
        "spotify": {
            "configured": "spotify" in configured_services,
            "connected": spotify_online,
            "status": "Connected"
            if spotify_online
            else ("Not configured" if "spotify" not in configured_services else "Needs reconnect"),
            "connect_service": spotify_connect_status(),
        },
        "configured_accounts": len(configured),
        "authenticated_accounts": len(account_pool),
        "missing_services": missing_services,
        "checked_at": time.time(),
    }


@app.post("/accounts/reconnect")
async def reconnect_accounts():
    await run_in_threadpool(relogin)
    notification_hook("Reconnecting accounts", "The account pool is refreshing in the background.")
    return {"success": True}


@app.get("/system/rate-limit")
async def get_system_rate_limit():
    return get_rate_limit_state()


@app.get("/system/diagnostics")
async def get_system_diagnostics():
    with download_queue_lock:
        status_counts: dict[str, int] = {}
        for item in download_queue.values():
            status = str(item.get("item_status", "Unknown"))
            status_counts[status] = status_counts.get(status, 0) + 1
    root = config.get("audio_download_path") or os.getcwd()
    try:
        usage = shutil.disk_usage(root)
        disk = {"total": usage.total, "free": usage.free, "used": usage.used}
    except OSError:
        disk = {"total": 0, "free": 0, "used": 0}
    rate_limit = get_rate_limit_state()
    spotify_rate_limited = bool(rate_limit.get("active")) and "spotify" in str(rate_limit.get("host") or "").casefold()
    spotify_api_status = "Rate limited" if spotify_rate_limited else "ND"
    return {
        "backend": {"status": "online", "version": config.get("version")},
        "workers": {
            "parsing": parsing_worker.thread.is_alive(),
            "downloads": downloadworker.thread.is_alive(),
            "accounts": bool(account_pool),
            "retry": retryworker.thread.is_alive() if config.get("enable_retry_worker") else False,
        },
        "queue": {
            "pending": pending.qsize(),
            "parsing": len(parsing),
            "downloads": len(download_queue),
            "statuses": status_counts,
            "paused": download_paused.is_set(),
        },
        "ffmpeg": {
            "path": config.get("_ffmpeg_bin_path", ""),
            "available": bool(config.get("_ffmpeg_bin_path")),
        },
        "disk": disk,
        "rate_limit": rate_limit,
        "spotify_api": {
            "configured": False,
            "connected": False,
            "status": spotify_api_status,
            "rate_limited": spotify_rate_limited,
            "seconds_remaining": int(rate_limit.get("seconds_remaining") or 0) if spotify_rate_limited else 0,
            "connect_service": [],
        },
    }


# LOGS ENDPOINTS
@app.get("/logs")
async def get_logs():
    """
    Endpoint to retrieve logs from the log file.

    :return: List of log entries.
    """
    log_path = config.get("_log_file")
    lines = None
    data = []
    with open(log_path, "r") as f:
        lines = f.readlines()
    for line in lines:
        main = re.findall(r"(\[*.+\])( -> *.+)", line)
        try:
            message = main[0][1]
        except IndexError:
            message = None
            data.append(
                {
                    "id": uuid.uuid4(),
                    "timestamp": "",
                    "level": "ERROR",
                    "message": line,
                }
            )
            continue

        try:
            log_info = re.findall(r"\[(.+?) :: (\w+?) :: (.+) :: (\w.+)]", main[0][0])
            date = log_info[0][0][:-4]
            source = log_info[0][2]
            level = log_info[0][3]
            formatted_message = main if message is None else source + message

        except IndexError:
            date = ""
            source = ""
            level = ""
            formatted_message = main if message is None else message
        data.append(
            {
                "id": uuid.uuid4(),
                "timestamp": date,
                "level": level,
                "message": formatted_message,
            }
        )
    return data


@app.get("/logs/download")
async def download_logs():
    """
    Returns the log file

    :return: List of log entries.
    """
    log_path = config.get("_log_file")
    directory, file_name = os.path.split(log_path)
    return FileResponse(log_path, media_type="text/plain", filename=file_name)


# SSE Methods and endpoint
_SSE_CONNECTION_LIFETIME_SECONDS = 5400


async def event_generator(user_id: str, request: Request):
    """Listens for items in the user's queue and pushes them to the frontend."""
    subscription_id, event_queue = subscribe_websocket(user_id)
    # EventSource reconnects automatically.  Bounding a connection's lifetime
    # prevents an idle stream from holding graceful shutdown open forever.

    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                data = event_queue.get_nowait()
            except (TimeoutError, IndexError):
                continue
            yield f"data: {json.dumps(data, skipkeys=True)}\n\n"
    finally:
        unsubscribe_websocket(subscription_id)


@app.get("/api/sse/{user_id}")
async def sse_endpoint(user_id: str, request: Request):
    """The Vite frontend connects here exactly ONCE."""
    return StreamingResponse(
        event_generator(user_id, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# The production UI is built by Vite into ui/dist and served by this same
# FastAPI process. Set ONTHESPOT_WEBUI_DIST when the files live elsewhere.
_workspace_root = Path(__file__).resolve().parents[3]
_ui_dist = Path(os.environ.get("ONTHESPOT_WEBUI_DIST") or _workspace_root / "ui" / "dist")
if _ui_dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_ui_dist), html=True), name="web-ui")


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("ONTHESPOT_HOST", "127.0.0.1"),
        port=int(os.environ.get("ONTHESPOT_PORT", "8000")),
    )
