#!/usr/bin/env python3
"""
🎬 Google Photos API Backend

RESTful API for accessing Google Photos:
- GET /api/files/mp4 - List all MP4 files
- GET /api/files/download?id=xxx - Download file by ID
- GET /api/files/stream?id=xxx - Stream video by ID
- GET /api/files/info?id=xxx - Get file metadata

Usage:
    pip install fastapi uvicorn
    python google_photos_api.py
    
    Then visit: http://localhost:8000/docs
"""

import os
import sys
import asyncio
import tempfile
import threading
import time
import json
import struct
import base64
from pathlib import Path
from typing import List, Optional, Dict, Any
from urllib.parse import quote
from collections import defaultdict

# Add gpm to Python path for Linux compatibility
current_dir = Path(__file__).parent
gpm_path = current_dir / "gpm"
if gpm_path.exists() and str(gpm_path) not in sys.path:
    sys.path.insert(0, str(gpm_path))

# Load environment variables from .env file
def load_env_file():
    """Load environment variables from .env file"""
    env_file = Path(".env")
    if env_file.exists():
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key] = value

# Load .env on import
load_env_file()

# Add the gpm directory to the path
sys.path.insert(0, str(Path(__file__).parent / "gpm"))

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import requests

# Import gpmc with fallback for Linux
try:
    from gpmc import Client
    print("✅ Successfully imported gpmc.Client")
except ImportError as e:
    print(f"❌ Failed to import gpmc: {e}")
    print("🔍 Trying alternative import methods...")

    # Try adding gpm to path again
    import sys
    from pathlib import Path

    # Multiple path attempts for different environments
    possible_paths = [
        Path(__file__).parent / "gpm",
        Path(__file__).parent / "gpm" / "gpmc",
        Path("/app/gpm"),
        Path("/app/gpm/gpmc"),
    ]

    for path in possible_paths:
        if path.exists():
            print(f"🔍 Trying path: {path}")
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))

    try:
        from gpmc import Client
        print("✅ Successfully imported gpmc.Client after path adjustment")
    except ImportError:
        try:
            from gpmc.client import Client
            print("✅ Successfully imported gpmc.client.Client")
        except ImportError:
            print("❌ All import attempts failed")
            raise ImportError("Could not import gpmc.Client - check gpm installation")

# Initialize FastAPI app
app = FastAPI(
    title="Google Photos API",
    description="RESTful API for accessing Google Photos media",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global Google Photos client
gp_client: Optional[Client] = None
file_cache: Dict[str, dict] = {}
cache_timestamp = 0

# Auto-refresh configuration
AUTO_REFRESH_ENABLED = True
AUTO_REFRESH_INTERVAL_MINUTES = 15  # Default: refresh every 15 minutes
auto_refresh_task = None
last_auto_refresh = 0

# Hybrid RAM + Disk Cache System for Video Streaming
# 5 minutes backward, 15 minutes forward, 1GB max per movie
import hashlib

# Download-Ahead Cache System - Simple and Fast
cache_dir = Path(__file__).parent / "video_cache"
cache_dir.mkdir(exist_ok=True)

# Metadata Cache System for Instant Seeking
metadata_cache_dir = Path(__file__).parent / "metadata_cache"
metadata_cache_dir.mkdir(exist_ok=True)

# Track download status for each file
download_status: Dict[str, Dict] = defaultdict(lambda: {
    'downloading': False,
    'completed': False,
    'file_path': None,
    'download_start': 0,
    'bytes_downloaded': 0,
    'total_bytes': 0,
    'download_speed_mbps': 0,
    'last_access': 0,
    'active_users': 0,  # Count of users currently watching
    'user_sessions': set()  # Track unique user sessions
})

download_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)

# Cache cleanup configuration
CACHE_CLEANUP_SECONDS = 20  # Clean cache after 20 seconds of no activity
CLEANUP_CHECK_INTERVAL = 20 # Check for cleanup every 20 seconds

# Background cleanup task
cleanup_task_running = False

def get_google_photos_client() -> Client:
    """Get or create Google Photos client"""
    global gp_client
    if gp_client is None:
        try:
            # Use auth data from environment variable if available
            auth_data = os.environ.get('GP_AUTH_DATA')
            if auth_data:
                gp_client = Client(auth_data=auth_data)
            else:
                gp_client = Client()
            print("✅ Google Photos client initialized")
        except Exception as e:
            print(f"❌ Failed to initialize Google Photos client: {e}")
            raise HTTPException(status_code=500, detail="Failed to initialize Google Photos client")
    return gp_client

def refresh_file_cache():
    """Refresh the file cache from Google Photos"""
    global file_cache, cache_timestamp
    import time
    
    # Only refresh if cache is older than 5 minutes
    if time.time() - cache_timestamp < 300 and file_cache:
        return
    
    print("🔄 Refreshing file cache...")
    client = get_google_photos_client()
    
    try:
        all_media = client.list_media_from_library_state(
            media_type="all",
            show_progress=False
        )

        file_cache.clear()
        for media in all_media:
            file_id = media['media_key']
            file_cache[file_id] = {
                'id': file_id,
                'filename': media['file_name'],
                'size_bytes': media.get('size_bytes', 0),
                'duration_ms': media.get('duration', 0),
                'type': 'video' if media.get('type') == 2 else 'image',
                'timestamp': media.get('timestamp', 0),
                'collection_id': media.get('collection_id', '')
            }
        
        cache_timestamp = time.time()
        print(f"✅ Cached {len(file_cache)} files")
        
    except Exception as e:
        print(f"❌ Error refreshing cache: {e}")
        raise HTTPException(status_code=500, detail="Failed to refresh file cache")

def get_cache_file_path(file_id: str) -> Path:
    """Get the cache file path for a video"""
    return cache_dir / f"{file_id}.mp4"

def start_full_download(file_id: str, download_url: str, file_size: int):
    """Start downloading the complete file in background"""
    with download_locks[file_id]:
        # Skip if already downloading or completed
        if download_status[file_id]['downloading'] or download_status[file_id]['completed']:
            print(f"📦 Download already in progress/completed for {file_id}")
            return

        download_status[file_id]['downloading'] = True
        download_status[file_id]['download_start'] = time.time()
        download_status[file_id]['total_bytes'] = file_size

        print(f"🚀 Starting download for {file_id}")

    def download_worker():
        cache_file = get_cache_file_path(file_id)

        try:
            # Get file size from download_status (already set above)
            current_file_size = download_status[file_id]['total_bytes']
            print(f"🚀 Starting full download: {current_file_size/1024/1024/1024:.1f}GB")

            response = requests.get(download_url, stream=True)
            if response.status_code == 200:
                # Get actual file size from response headers if available
                actual_size = response.headers.get('Content-Length')
                if actual_size:
                    actual_size = int(actual_size)
                    if actual_size != current_file_size:
                        print(f"⚠️ File size mismatch! Expected: {current_file_size/1024/1024:.0f}MB, Actual: {actual_size/1024/1024:.0f}MB")
                        current_file_size = actual_size  # Use the correct size
                        download_status[file_id]['total_bytes'] = current_file_size
                        print(f"✅ Updated file size to: {current_file_size/1024/1024:.0f}MB")
                with open(cache_file, 'wb') as f:
                    bytes_downloaded = 0
                    start_time = time.time()
                    metadata_extracted = False
                    first_chunk_data = b''

                    for chunk in response.iter_content(chunk_size=1024*1024):  # 1MB chunks
                        if chunk:
                            f.write(chunk)
                            bytes_downloaded += len(chunk)

                            # 🎬 METADATA EXTRACTION: Extract from first 50MB
                            if not metadata_extracted and bytes_downloaded <= 50 * 1024 * 1024:
                                first_chunk_data += chunk

                                # Try to extract metadata when we have enough data
                                if len(first_chunk_data) >= 10 * 1024 * 1024:  # 10MB should be enough for metadata
                                    try:
                                        # Check if we already have metadata cached
                                        existing_metadata = load_metadata_cache(file_id)
                                        if not existing_metadata:
                                            print(f"🎬 Extracting MP4 metadata from first {len(first_chunk_data)/1024/1024:.1f}MB...")
                                            filename = file_cache.get(file_id, {}).get('filename', 'unknown.mp4')
                                            metadata = extract_mp4_metadata(first_chunk_data, current_file_size, filename)
                                            save_metadata_cache(file_id, metadata)
                                            print(f"✅ Metadata extraction complete!")
                                        else:
                                            print(f"📖 Metadata already cached, skipping extraction")
                                        metadata_extracted = True
                                    except Exception as e:
                                        print(f"⚠️ Metadata extraction failed: {e}")
                                        metadata_extracted = True  # Don't try again

                            # Update progress
                            download_status[file_id]['bytes_downloaded'] = bytes_downloaded

                            # Calculate speed every 1MB
                            if bytes_downloaded % (10 * 1024 * 1024) == 0:  # Every 10MB
                                elapsed = time.time() - start_time
                                speed_mbps = (bytes_downloaded / 1024 / 1024) / max(elapsed, 0.1)
                                download_status[file_id]['download_speed_mbps'] = speed_mbps

                                # Calculate progress with safety checks - CAP AT 100%
                                if current_file_size > 0:
                                    progress = min((bytes_downloaded / current_file_size) * 100, 100.0)  # Never exceed 100%
                                else:
                                    progress = 0

                                print(f"📥 Download progress: {progress:.1f}% ({speed_mbps:.0f} MB/s) [{bytes_downloaded/1024/1024:.0f}MB/{current_file_size/1024/1024:.0f}MB]")

                # Mark as completed
                download_status[file_id]['downloading'] = False
                download_status[file_id]['completed'] = True

                elapsed = time.time() - start_time
                final_speed = (current_file_size / 1024 / 1024) / max(elapsed, 0.1)
                print(f"✅ Download completed: {current_file_size/1024/1024/1024:.1f}GB in {elapsed:.1f}s ({final_speed:.0f} MB/s)")

            else:
                print(f"❌ Download failed: HTTP {response.status_code}")
                download_status[file_id]['downloading'] = False

        except Exception as e:
            print(f"❌ Download error: {e}")
            download_status[file_id]['downloading'] = False

    # Start download in background thread
    threading.Thread(target=download_worker, daemon=True).start()
    print(f"🎯 Background download started for {file_id}")

def get_download_progress(file_id: str) -> dict:
    """Get download progress for a file"""
    status = download_status[file_id]

    if status['completed']:
        return {
            'status': 'completed',
            'progress': 100.0,
            'speed_mbps': 0,
            'eta_seconds': 0
        }
    elif status['downloading']:
        if status['total_bytes'] > 0:
            progress = min((status['bytes_downloaded'] / status['total_bytes']) * 100, 100.0)  # Cap at 100%
            remaining_bytes = max(status['total_bytes'] - status['bytes_downloaded'], 0)  # Never negative
            eta = remaining_bytes / max(status['download_speed_mbps'] * 1024 * 1024, 1)
        else:
            progress = 0
            eta = 0

        return {
            'status': 'downloading',
            'progress': progress,
            'speed_mbps': status['download_speed_mbps'],
            'eta_seconds': eta
        }
    else:
        return {
            'status': 'not_started',
            'progress': 0,
            'speed_mbps': 0,
            'eta_seconds': 0
        }

def is_file_cached(file_id: str) -> bool:
    """Check if file is fully cached"""
    cache_file = get_cache_file_path(file_id)
    return cache_file.exists() and download_status[file_id]['completed']

def register_user_access(file_id: str, user_session: str = None):
    """Register that a user is accessing a file - simplified version"""
    with download_locks[file_id]:
        download_status[file_id]['last_access'] = time.time()
        # Don't track individual users, just update access time
        print(f"👤 User access registered for {file_id} (last_access updated)")

def unregister_user_access(file_id: str, user_session: str = None):
    """Unregister user access when they stop watching"""
    # Not needed in simplified version
    pass

def cleanup_inactive_cache():
    """Clean up cache files that haven't been accessed recently"""
    current_time = time.time()
    files_to_cleanup = []

    print(f"🔍 Cleanup check: scanning {len(download_status)} files...")

    for file_id, status in download_status.items():
        # Check if file has been inactive for more than CACHE_CLEANUP_SECONDS
        time_since_access = current_time - status['last_access']

        # Don't cleanup files that are currently downloading or recently accessed
        is_downloading = status['downloading']
        is_recently_accessed = time_since_access <= CACHE_CLEANUP_SECONDS
        has_no_access_time = status['last_access'] == 0

        print(f"📊 {file_id[:8]}... - downloading: {is_downloading}, completed: {status['completed']}, inactive: {time_since_access:.1f}s, threshold: {CACHE_CLEANUP_SECONDS}s")

        if not is_downloading and not is_recently_accessed and not has_no_access_time:
            files_to_cleanup.append((file_id, time_since_access))
            print(f"🗑️ Marked for cleanup: {file_id[:8]}... ({time_since_access:.1f}s inactive)")

    if not files_to_cleanup:
        print(f"✅ No files to cleanup")

    for file_id, time_since_access in files_to_cleanup:
        cache_file = get_cache_file_path(file_id)

        try:
            if cache_file.exists():
                # Double-check that file is not being downloaded
                if download_status[file_id]['downloading']:
                    print(f"⏭️ Skipping cleanup for {file_id}: still downloading")
                    continue

                # Check if file was accessed very recently (last 5 seconds)
                very_recent_access = time.time() - download_status[file_id]['last_access'] < 5
                if very_recent_access:
                    print(f"⏭️ Skipping cleanup for {file_id}: accessed {time.time() - download_status[file_id]['last_access']:.1f}s ago")
                    continue

                file_size = cache_file.stat().st_size

                # Try to delete the file
                cache_file.unlink()

                # Reset download status
                download_status[file_id] = {
                    'downloading': False,
                    'completed': False,
                    'file_path': None,
                    'download_start': 0,
                    'bytes_downloaded': 0,
                    'total_bytes': 0,
                    'download_speed_mbps': 0,
                    'last_access': 0,
                    'active_users': 0,
                    'user_sessions': set()
                }

                print(f"🧹 Cleaned up inactive cache: {file_id} ({file_size/1024/1024/1024:.1f}GB freed, {time_since_access:.1f}s inactive)")

        except PermissionError as e:
            # File is being used by another process (could be antivirus, file explorer, etc.)
            print(f"⏭️ Skipping cleanup for {file_id}: file locked by system")
        except FileNotFoundError:
            # File was already deleted
            print(f"✅ File {file_id} already cleaned up")
            # Reset download status anyway
            download_status[file_id] = {
                'downloading': False,
                'completed': False,
                'file_path': None,
                'download_start': 0,
                'bytes_downloaded': 0,
                'total_bytes': 0,
                'download_speed_mbps': 0,
                'last_access': 0,
                'active_users': 0,
                'user_sessions': set()
            }
        except Exception as e:
            print(f"❌ Error cleaning up cache for {file_id}: {e}")

def start_cleanup_task():
    """Start the background cleanup task"""
    global cleanup_task_running

    if cleanup_task_running:
        return

    cleanup_task_running = True

    def cleanup_worker():
        while cleanup_task_running:
            try:
                cleanup_inactive_cache()
                time.sleep(CLEANUP_CHECK_INTERVAL)
            except Exception as e:
                print(f"❌ Cleanup task error: {e}")
                time.sleep(CLEANUP_CHECK_INTERVAL)

    threading.Thread(target=cleanup_worker, daemon=True).start()
    print(f"🧹 Background cleanup task started (check every {CLEANUP_CHECK_INTERVAL}s, cleanup after {CACHE_CLEANUP_SECONDS}s)")

# Legacy functions removed - using simple download-ahead strategy now

# ========================================
# 🎬 METADATA CACHE SYSTEM
# ========================================

def get_metadata_file_path(file_id: str) -> Path:
    """Get the metadata file path for a video"""
    return metadata_cache_dir / f"{file_id}.meta"

def extract_mp4_metadata(file_data: bytes, file_size: int, filename: str) -> dict:
    """Extract MP4 metadata from the first chunk of data"""
    try:
        metadata = {
            "file_size": file_size,
            "filename": filename,
            "duration_ms": 0,
            "has_moov": False,
            "ftyp_data": None,
            "moov_data": None,
            "created": time.time()
        }

        # Look for MP4 atoms in the data
        offset = 0
        while offset < len(file_data) - 8:
            # Read atom size and type
            if offset + 8 > len(file_data):
                break

            atom_size = struct.unpack('>I', file_data[offset:offset+4])[0]
            atom_type = file_data[offset+4:offset+8].decode('ascii', errors='ignore')

            print(f"🔍 Found MP4 atom: {atom_type} (size: {atom_size})")

            if atom_type == 'ftyp':
                # File type atom - contains MP4 format info
                end_pos = min(offset + atom_size, len(file_data))
                metadata["ftyp_data"] = base64.b64encode(file_data[offset:end_pos]).decode()
                print(f"✅ Extracted ftyp atom ({end_pos - offset} bytes)")

            elif atom_type == 'moov':
                # Movie atom - contains all metadata
                end_pos = min(offset + atom_size, len(file_data))

                # OPTIMIZATION: Limit moov size to prevent huge metadata files
                max_moov_size = 1024 * 1024  # 1MB max for moov atom
                if atom_size > max_moov_size:
                    print(f"⚠️ Large moov atom ({atom_size/1024/1024:.1f}MB), limiting to 1MB")
                    end_pos = min(offset + max_moov_size, len(file_data))

                metadata["moov_data"] = base64.b64encode(file_data[offset:end_pos]).decode()
                metadata["has_moov"] = True
                print(f"✅ Extracted moov atom ({end_pos - offset} bytes, original: {atom_size} bytes)")

                # Try to extract duration from moov
                try:
                    moov_data = file_data[offset:end_pos]
                    # Look for mvhd (movie header) inside moov
                    mvhd_pos = moov_data.find(b'mvhd')
                    if mvhd_pos > 0 and mvhd_pos + 24 < len(moov_data):
                        # Skip mvhd header and read timescale and duration
                        timescale = struct.unpack('>I', moov_data[mvhd_pos+12:mvhd_pos+16])[0]
                        duration = struct.unpack('>I', moov_data[mvhd_pos+16:mvhd_pos+20])[0]
                        if timescale > 0:
                            metadata["duration_ms"] = int((duration / timescale) * 1000)
                            print(f"✅ Extracted duration: {metadata['duration_ms']/1000:.1f}s")
                except Exception as e:
                    print(f"⚠️ Could not extract duration: {e}")

            # Move to next atom
            if atom_size <= 8:
                offset += 8
            else:
                offset += atom_size

        return metadata

    except Exception as e:
        print(f"❌ Error extracting MP4 metadata: {e}")
        return {
            "file_size": file_size,
            "filename": filename,
            "duration_ms": 0,
            "has_moov": False,
            "ftyp_data": None,
            "moov_data": None,
            "created": time.time()
        }

def save_metadata_cache(file_id: str, metadata: dict):
    """Save metadata to cache file"""
    try:
        metadata_file = get_metadata_file_path(file_id)
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f, indent=2)
        print(f"💾 Saved metadata cache: {metadata_file.name} ({metadata_file.stat().st_size/1024:.1f}KB)")
    except Exception as e:
        print(f"❌ Error saving metadata cache: {e}")

def load_metadata_cache(file_id: str) -> Optional[dict]:
    """Load metadata from cache file"""
    try:
        metadata_file = get_metadata_file_path(file_id)
        if metadata_file.exists():
            with open(metadata_file, 'r') as f:
                metadata = json.load(f)
            print(f"📖 Loaded metadata cache: {metadata_file.name} ({metadata_file.stat().st_size/1024:.1f}KB)")
            return metadata
    except Exception as e:
        print(f"❌ Error loading metadata cache: {e}")
    return None

def create_virtual_mp4_header(metadata: dict) -> bytes:
    """Create a virtual MP4 header with correct metadata for seeking"""
    try:
        header_parts = []

        # Add ftyp atom if available
        if metadata.get("ftyp_data"):
            ftyp_bytes = base64.b64decode(metadata["ftyp_data"])
            header_parts.append(ftyp_bytes)
            print(f"📦 Added ftyp atom ({len(ftyp_bytes)} bytes)")

        # Add moov atom if available
        if metadata.get("moov_data"):
            moov_bytes = base64.b64decode(metadata["moov_data"])
            header_parts.append(moov_bytes)
            print(f"📦 Added moov atom ({len(moov_bytes)} bytes)")

        if header_parts:
            header = b''.join(header_parts)
            print(f"✅ Created virtual MP4 header ({len(header)} bytes)")
            return header
        else:
            print(f"⚠️ No metadata available for virtual header")
            return b''

    except Exception as e:
        print(f"❌ Error creating virtual MP4 header: {e}")
        return b''

async def auto_refresh_cache():
    """Auto-refresh cache periodically"""
    global last_auto_refresh

    while AUTO_REFRESH_ENABLED:
        try:
            await asyncio.sleep(AUTO_REFRESH_INTERVAL_MINUTES * 60)  # Convert minutes to seconds

            if not AUTO_REFRESH_ENABLED:
                break

            current_time = time.time()
            old_count = len(file_cache)

            print(f"🔄 Auto-refresh: Updating cache (interval: {AUTO_REFRESH_INTERVAL_MINUTES}min)")

            # Force refresh from Google Photos
            refresh_file_cache()
            new_count = len(file_cache)

            last_auto_refresh = current_time

            if new_count != old_count:
                print(f"✅ Auto-refresh complete: {old_count} → {new_count} files ({new_count - old_count:+d} change)")
            else:
                print(f"✅ Auto-refresh complete: {new_count} files (no changes)")

        except asyncio.CancelledError:
            print("🛑 Auto-refresh task cancelled")
            break
        except Exception as e:
            print(f"❌ Auto-refresh error: {e}")
            # Continue the loop even if there's an error

async def start_auto_refresh():
    """Start the auto-refresh background task"""
    global auto_refresh_task, last_auto_refresh

    if not AUTO_REFRESH_ENABLED:
        print("⚠️ Auto-refresh disabled")
        return

    if auto_refresh_task and not auto_refresh_task.done():
        print("⚠️ Auto-refresh task already running")
        return

    last_auto_refresh = time.time()
    auto_refresh_task = asyncio.create_task(auto_refresh_cache())
    print(f"🔄 Auto-refresh started (every {AUTO_REFRESH_INTERVAL_MINUTES} minutes)")

async def stop_auto_refresh():
    """Stop the auto-refresh background task"""
    global auto_refresh_task

    if auto_refresh_task and not auto_refresh_task.done():
        auto_refresh_task.cancel()
        try:
            await auto_refresh_task
        except asyncio.CancelledError:
            pass
        print("🛑 Auto-refresh stopped")

def clear_residual_cache():
    """Clear any residual cache files from previous sessions"""
    try:
        total_files = 0
        total_size = 0

        # Clear all cache files
        for cache_file in cache_dir.glob("*.mp4"):
            try:
                file_size = cache_file.stat().st_size
                cache_file.unlink()
                total_files += 1
                total_size += file_size
            except Exception as e:
                print(f"⚠️ Error removing residual cache {cache_file}: {e}")

        # Clear download status
        download_status.clear()

        if total_files > 0:
            print(f"🧹 Cleared residual cache: {total_files} files ({total_size/1024/1024/1024:.1f}GB freed)")
        else:
            print("✨ No residual cache found")

    except Exception as e:
        print(f"❌ Error clearing residual cache: {e}")

@app.on_event("startup")
async def startup_event():
    """Initialize the API on startup"""
    print("🚀 Starting Google Photos API...")

    # Debug environment
    print(f"🔍 Python version: {sys.version}")
    print(f"🔍 Current working directory: {os.getcwd()}")
    print(f"🔍 Python path: {sys.path[:3]}...")  # Show first 3 paths

    # Check gpm folder
    gpm_path = Path(__file__).parent / "gpm"
    print(f"🔍 GPM path exists: {gpm_path.exists()}")
    if gpm_path.exists():
        print(f"🔍 GPM contents: {list(gpm_path.iterdir())[:5]}")  # Show first 5 items

    # Check environment variables
    auth_data = os.environ.get('GP_AUTH_DATA')
    print(f"🔍 GP_AUTH_DATA configured: {'✅ Yes' if auth_data else '❌ No'}")
    if auth_data:
        print(f"🔍 GP_AUTH_DATA length: {len(auth_data)} chars")
    else:
        # Fallback: check if we can use hardcoded token for testing
        print("⚠️ No GP_AUTH_DATA found in environment")
        print("🔍 Available env vars:", [k for k in os.environ.keys() if 'GP' in k or 'AUTH' in k])

    # Clear any residual cache from previous sessions
    clear_residual_cache()

    # Initialize components
    try:
        get_google_photos_client()
        refresh_file_cache()
        start_cleanup_task()

        # Start auto-refresh task
        await start_auto_refresh()

        print("✅ API ready!")
    except Exception as e:
        print(f"❌ Startup failed: {e}")
        import traceback
        traceback.print_exc()

@app.get("/debug")
async def debug_info():
    """Debug endpoint to check system status"""
    gpm_path = Path(__file__).parent / "gpm"
    auth_data = os.environ.get('GP_AUTH_DATA')

    return {
        "system": {
            "python_version": sys.version,
            "working_directory": str(os.getcwd()),
            "python_path": sys.path[:5]
        },
        "gpm": {
            "path_exists": gpm_path.exists(),
            "path_location": str(gpm_path),
            "contents": [str(p.name) for p in gpm_path.iterdir()] if gpm_path.exists() else []
        },
        "auth": {
            "gp_auth_data_configured": bool(auth_data),
            "gp_auth_data_length": len(auth_data) if auth_data else 0
        },
        "cache": {
            "files_cached": len(file_cache),
            "cache_timestamp": cache_timestamp,
            "sample_files": [
                {
                    "id": file_id,
                    "filename": file_info['filename'],
                    "type": file_info['type']
                }
                for i, (file_id, file_info) in enumerate(file_cache.items()) if i < 5
            ]
        },
        "client": {
            "initialized": gp_client is not None,
            "client_type": str(type(gp_client)) if gp_client else None
        }
    }

@app.get("/")
async def root():
    """API root endpoint"""
    return {
        "message": "🎬 Google Photos API - Complete Media Management",
        "version": "2.1.0",
        "status": "✅ Ready",
        "cached_files": len(file_cache),
        "features": [
            "🚀 Download-ahead strategy (7-second full download)",
            "⚡ Instant seeking after download completes",
            "🔗 Direct redirect (zero server involvement)",
            "🌐 Passthrough streaming (no server storage)",
            "📱 Range request support",
            "🎯 Optimized Google URLs"
        ],
        "endpoints": {
            "list_files": {
                "mp4": "/api/files/mp4",
                "mp4_raw": "/api/files/mp4-raw",
                "all": "/api/files/all"
            },
            "download": {
                "passthrough": "/api/files/download?id=xxx",
                "direct_redirect": "/api/files/downloadDirect?id=xxx"
            },
            "urls": {
                "basic": "/api/files/direct-url?id=xxx",
                "optimized": "/api/files/google-url?id=xxx"
            },
            "streaming": {
                "proxy_stream": "/api/files/stream?id=xxx",
                "direct_stream": "/api/files/stream-direct?id=xxx",
                "smart_download": "/api/files/smart-stream?id=xxx"
            },
            "info": "/api/files/info?id=xxx",
            "download_status": "/api/files/download-status",
            "download_progress": "/api/files/download-status/{file_id}",
            "heartbeat": "/api/files/heartbeat?id=xxx",
            "cache": {
                "reset_cache": "/api/cache/reset",
                "clear_all_cache": "/api/cache/clear",
                "auto_refresh_status": "/api/cache/auto-refresh/status",
                "auto_refresh_configure": "/api/cache/auto-refresh/configure",
                "auto_refresh_trigger": "/api/cache/auto-refresh/trigger"
            },
            "docs": "/docs"
        },
        "examples": {
            "smart_download": "/api/files/smart-stream?id=FILE_ID",
            "download_progress": "/api/files/download-status/FILE_ID",
            "download_direct": "/api/files/downloadDirect?id=FILE_ID"
        }
    }

@app.get("/ui")
async def movie_ui():
    """Movie UI with TMDB integration"""
    from fastapi.responses import FileResponse
    import os

    html_file_path = os.path.join("html", "movie_library.html")
    if os.path.exists(html_file_path):
        return FileResponse(html_file_path, media_type="text/html")
    else:
        raise HTTPException(status_code=404, detail="UI file not found")

@app.get("/docs-ui")
async def api_docs_ui():
    """Interactive API Documentation"""
    from fastapi.responses import FileResponse
    import os

    html_file_path = os.path.join("html", "api_docs.html")
    if os.path.exists(html_file_path):
        return FileResponse(html_file_path, media_type="text/html")
    else:
        raise HTTPException(status_code=404, detail="API docs file not found")

@app.get("/api/files/search")
async def search_files(q: str = Query(..., description="Search query")):
    """Search files by filename or ID"""
    refresh_file_cache()

    results = []
    query = q.lower()

    for file_id, file_info in file_cache.items():
        # Search by ID or filename
        if (query in file_id.lower() or
            query in file_info['filename'].lower()):
            results.append({
                'id': file_id,
                'filename': file_info['filename'],
                'type': file_info['type'],
                'size_bytes': file_info['size_bytes'],
                'match_type': 'id' if query in file_id.lower() else 'filename'
            })

    return {
        "query": q,
        "count": len(results),
        "results": results[:20]  # Limit to 20 results
    }



def extract_tmdb_id_from_filename(filename):
    """Extract TMDB ID from filename with format MovieName_tmdbid.extension"""
    try:
        # Remove extension first
        name_without_ext = filename.rsplit('.', 1)[0]

        # Split by underscore and get the last part
        parts = name_without_ext.split('_')
        if len(parts) >= 2:
            # Check if the last part is a number (TMDB ID)
            potential_id = parts[-1]
            if potential_id.isdigit():
                return potential_id

        return None
    except Exception:
        return None

@app.get("/api/files/mp4")
async def list_mp4_files():
    """List all MP4 video files (deduplicated by filename)"""
    refresh_file_cache()

    # First collect all MP4 files
    all_mp4_files = []
    for file_id, file_info in file_cache.items():
        if file_info['type'] == 'video' and file_info['filename'].lower().endswith('.mp4'):
            # Extract TMDB ID from filename
            tmdb_id = extract_tmdb_id_from_filename(file_info['filename'])

            all_mp4_files.append({
                'id': file_id,
                'filename': file_info['filename'],
                'tmdb_id': tmdb_id,
                'size_bytes': file_info['size_bytes'],
                'size_mb': round(file_info['size_bytes'] / (1024 * 1024), 2),
                'duration_ms': file_info['duration_ms'],
                'duration_seconds': file_info['duration_ms'] // 1000 if file_info['duration_ms'] else 0,
                'timestamp': file_info['timestamp'],
                'collection_id': file_info['collection_id']
            })

    # Deduplicate by filename, keeping the most recent (highest timestamp)
    filename_map = {}
    for file_info in all_mp4_files:
        filename = file_info['filename']
        if filename not in filename_map or file_info['timestamp'] > filename_map[filename]['timestamp']:
            filename_map[filename] = file_info

    # Convert back to list and sort by timestamp (newest first)
    mp4_files = list(filename_map.values())
    mp4_files.sort(key=lambda x: x['timestamp'], reverse=True)

    print(f"📋 MP4 files: {len(all_mp4_files)} total, {len(mp4_files)} unique after deduplication")

    return {
        "count": len(mp4_files),
        "files": mp4_files,
        "total_before_dedup": len(all_mp4_files),
        "duplicates_removed": len(all_mp4_files) - len(mp4_files)
    }

@app.get("/api/files/mp4-raw")
async def list_mp4_files_raw():
    """List all MP4 video files (including duplicates) - for debugging"""
    refresh_file_cache()

    mp4_files = []
    for file_id, file_info in file_cache.items():
        if file_info['type'] == 'video' and file_info['filename'].lower().endswith('.mp4'):
            mp4_files.append({
                'id': file_id,
                'filename': file_info['filename'],
                'size_bytes': file_info['size_bytes'],
                'size_mb': round(file_info['size_bytes'] / (1024 * 1024), 2),
                'duration_seconds': file_info['duration_ms'] // 1000 if file_info['duration_ms'] else 0,
                'timestamp': file_info['timestamp'],
                'collection_id': file_info['collection_id']
            })

    # Sort by filename then timestamp
    mp4_files.sort(key=lambda x: (x['filename'], x['timestamp']))

    return {
        "count": len(mp4_files),
        "files": mp4_files,
        "note": "This endpoint shows ALL files including duplicates"
    }

@app.get("/api/files/all")
async def list_all_files():
    """List all files (videos and images)"""
    refresh_file_cache()

    all_files = []
    for file_id, file_info in file_cache.items():
        all_files.append({
            'id': file_id,
            'filename': file_info['filename'],
            'type': file_info['type'],
            'size_bytes': file_info['size_bytes'],
            'size_mb': round(file_info['size_bytes'] / (1024 * 1024), 2),
            'duration_seconds': file_info['duration_ms'] // 1000 if file_info['duration_ms'] else 0,
            'timestamp': file_info['timestamp'],
            'collection_id': file_info['collection_id']
        })

    return {
        "count": len(all_files),
        "files": all_files
    }

@app.get("/api/files/info")
async def get_file_info(id: str = Query(..., description="File ID")):
    """Get detailed information about a specific file"""
    refresh_file_cache()
    
    if id not in file_cache:
        raise HTTPException(status_code=404, detail="File not found")
    
    file_info = file_cache[id]
    return {
        'id': id,
        'filename': file_info['filename'],
        'type': file_info['type'],
        'size_bytes': file_info['size_bytes'],
        'size_mb': round(file_info['size_bytes'] / (1024 * 1024), 2),
        'size_gb': round(file_info['size_bytes'] / (1024 * 1024 * 1024), 2),
        'duration_ms': file_info['duration_ms'],
        'duration_seconds': file_info['duration_ms'] // 1000 if file_info['duration_ms'] else 0,
        'duration_formatted': f"{file_info['duration_ms'] // 60000}:{(file_info['duration_ms'] % 60000) // 1000:02d}" if file_info['duration_ms'] else "0:00",
        'timestamp': file_info['timestamp'],
        'collection_id': file_info['collection_id']
    }

@app.get("/api/files/download")
async def download_file(id: str = Query(..., description="File ID")):
    """Download a file by ID - Direct passthrough from Google Photos"""
    refresh_file_cache()

    if id not in file_cache:
        raise HTTPException(status_code=404, detail="File not found")

    file_info = file_cache[id]
    filename = file_info['filename']

    print(f"📥 Download request: {filename}")

    try:
        client = get_google_photos_client()
        download_data = client.api.get_download_urls(id)

        # Extract download URL using the corrected path
        try:
            download_url = download_data["1"]["5"]["3"]["5"]
        except KeyError:
            try:
                download_url = download_data["1"]["5"]["2"]["6"]
            except KeyError:
                try:
                    download_url = download_data["1"]["5"]["2"]["5"]
                except KeyError:
                    raise HTTPException(status_code=500, detail="No download URL found")

        # Stream directly from Google Photos (PASSTHROUGH)
        response = requests.get(download_url, stream=True)
        response.raise_for_status()

        # Encode filename properly for HTTP headers (handle Unicode characters)
        try:
            # Try ASCII encoding first
            filename_ascii = filename.encode('ascii').decode('ascii')
            content_disposition = f'attachment; filename="{filename_ascii}"'
        except UnicodeEncodeError:
            # Use RFC 5987 encoding for Unicode filenames
            filename_encoded = quote(filename.encode('utf-8'))
            content_disposition = f"attachment; filename*=UTF-8''{filename_encoded}"

        return StreamingResponse(
            response.iter_content(chunk_size=8192),
            media_type='application/octet-stream',
            headers={
                'Content-Disposition': content_disposition,
                'Content-Length': response.headers.get('Content-Length', ''),
                'Accept-Ranges': 'bytes'
            }
        )

    except Exception as e:
        print(f"❌ Download error: {e}")
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

@app.get("/api/files/stream")
async def stream_file(id: str = Query(..., description="File ID"), request: Request = None):
    """Stream a video file by ID - Proxy streaming through server for browser playback"""
    refresh_file_cache()

    if id not in file_cache:
        raise HTTPException(status_code=404, detail="File not found")

    file_info = file_cache[id]
    filename = file_info['filename']

    # Only allow video files for streaming
    if file_info['type'] != 'video':
        raise HTTPException(status_code=400, detail="File is not a video")

    print(f"🎬 Stream request: {filename}")

    try:
        client = get_google_photos_client()
        download_data = client.api.get_download_urls(id)

        # Extract download URL using the corrected path
        try:
            download_url = download_data["1"]["5"]["3"]["5"]
        except KeyError:
            try:
                download_url = download_data["1"]["5"]["2"]["6"]
            except KeyError:
                try:
                    download_url = download_data["1"]["5"]["2"]["5"]
                except KeyError:
                    raise HTTPException(status_code=500, detail="No download URL found")

        # Handle range requests for seeking
        range_header = request.headers.get('range') if request else None
        headers = {}
        if range_header:
            headers['Range'] = range_header
            print(f"🎯 Range request: {range_header}")

        # Make request to Google Photos
        response = requests.get(download_url, headers=headers, stream=True)

        if response.status_code not in [200, 206]:
            raise HTTPException(status_code=500, detail=f"Google Photos returned status {response.status_code}")

        # Prepare streaming headers for video playback
        response_headers = {
            "Content-Type": "video/mp4",
            "Accept-Ranges": "bytes",
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-cache"
        }

        # Copy relevant headers from Google Photos response
        if 'Content-Length' in response.headers:
            response_headers["Content-Length"] = response.headers['Content-Length']
        if 'Content-Range' in response.headers:
            response_headers["Content-Range"] = response.headers['Content-Range']

        print(f"🎬 Streaming {filename} (proxy mode) - Status: {response.status_code}")

        return StreamingResponse(
            response.iter_content(chunk_size=8192),
            media_type='video/mp4',
            headers=response_headers,
            status_code=response.status_code
        )

    except Exception as e:
        print(f"❌ Stream error: {e}")
        raise HTTPException(status_code=500, detail=f"Stream failed: {str(e)}")

@app.get("/api/files/downloadDirect")
async def download_direct_redirect(id: str = Query(..., description="File ID")):
    """Direct redirect to Google Photos download URL - Client downloads directly"""
    refresh_file_cache()

    if id not in file_cache:
        raise HTTPException(status_code=404, detail="File not found")

    file_info = file_cache[id]
    filename = file_info['filename']

    print(f"🔗 Direct redirect: {filename}")

    try:
        client = get_google_photos_client()
        download_data = client.api.get_download_urls(id)

        # Extract download URL using the corrected path
        try:
            download_url = download_data["1"]["5"]["3"]["5"]
        except KeyError:
            try:
                download_url = download_data["1"]["5"]["2"]["6"]
            except KeyError:
                try:
                    download_url = download_data["1"]["5"]["2"]["5"]
                except KeyError:
                    raise HTTPException(status_code=500, detail="No download URL found")

        # HTTP 302 Redirect - Client downloads directly from Google Photos
        return RedirectResponse(
            url=download_url,
            status_code=302,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )

    except Exception as e:
        print(f"❌ Direct redirect error: {e}")
        raise HTTPException(status_code=500, detail=f"Redirect failed: {str(e)}")

@app.get("/api/files/direct-url")
async def get_direct_url(id: str = Query(..., description="File ID")):
    """Get direct download URL from Google Photos (for client-side downloads)"""
    refresh_file_cache()

    if id not in file_cache:
        raise HTTPException(status_code=404, detail="File not found")

    file_info = file_cache[id]
    filename = file_info['filename']

    print(f"🔗 Direct URL request for: {filename}")

    try:
        client = get_google_photos_client()

        # Get direct download URL from Google Photos
        download_data = client.api.get_download_urls(id)

        # Extract download URL using the corrected path
        try:
            download_url = download_data["1"]["5"]["3"]["5"]
        except KeyError:
            try:
                download_url = download_data["1"]["5"]["2"]["6"]
            except KeyError:
                try:
                    download_url = download_data["1"]["5"]["2"]["5"]
                except KeyError:
                    raise HTTPException(status_code=500, detail="No download URL found")

        return {
            "id": id,
            "filename": filename,
            "download_url": download_url,
            "size_bytes": file_info['size_bytes'],
            "type": file_info['type'],
            "note": "This URL can be used for direct downloads from Google Photos"
        }

    except Exception as e:
        print(f"❌ Direct URL error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get direct URL: {str(e)}")

@app.get("/api/files/google-url")
async def get_google_streaming_url(id: str = Query(..., description="File ID")):
    """Get the long Google Photos streaming URL (optimized by Google)"""
    refresh_file_cache()

    if id not in file_cache:
        raise HTTPException(status_code=404, detail="File not found")

    file_info = file_cache[id]
    filename = file_info['filename']

    print(f"🔗 Google streaming URL request for: {filename}")

    try:
        client = get_google_photos_client()

        # Get the full download data to see all available URLs
        download_data = client.api.get_download_urls(id)

        # Extract the long Google URL (the one with all the streaming parameters)
        try:
            google_url = download_data["1"]["5"]["3"]["5"]
        except KeyError:
            try:
                google_url = download_data["1"]["5"]["2"]["6"]
            except KeyError:
                try:
                    google_url = download_data["1"]["5"]["2"]["5"]
                except KeyError:
                    raise HTTPException(status_code=500, detail="No Google URL found")

        print(f"✅ Google streaming URL obtained:")
        print(f"   📄 File: {filename}")
        print(f"   🔗 URL length: {len(google_url)} characters")
        print(f"   🎬 URL preview: {google_url[:100]}...")

        return {
            "id": id,
            "filename": filename,
            "google_streaming_url": google_url,
            "url_length": len(google_url),
            "file_info": {
                "size_bytes": file_info['size_bytes'],
                "size_mb": round(file_info['size_bytes'] / (1024 * 1024), 2) if file_info['size_bytes'] > 0 else 0,
                "duration_seconds": file_info['duration_ms'] // 1000 if file_info['duration_ms'] else 0,
                "type": file_info['type']
            },
            "usage_notes": [
                "This URL is optimized by Google Photos for streaming",
                "Use directly in video players (VLC, browsers, etc.)",
                "Supports range requests and seeking",
                "May have expiration time - refresh if needed"
            ]
        }

    except Exception as e:
        print(f"❌ Google URL error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get Google URL: {str(e)}")



@app.get("/api/files/smart-stream")
async def smart_stream_download_ahead(
    id: str = Query(..., description="File ID"),
    reset: bool = Query(False, description="Reset cache state"),
    request: Request = None
):
    """Smart streaming with download-ahead strategy (7-second full download)"""
    refresh_file_cache()

    if id not in file_cache:
        raise HTTPException(status_code=404, detail="File not found")

    file_info = file_cache[id]
    filename = file_info['filename']

    # Don't use hardcoded fallback - we'll get real size from HTTP response
    file_size = file_info['size_bytes'] if file_info['size_bytes'] > 0 else 0

    if file_size > 0:
        print(f"🚀 Smart stream request for: {filename} ({file_size/1024/1024/1024:.1f}GB) [reset={reset}]")
    else:
        print(f"🚀 Smart stream request for: {filename} (size unknown, will detect) [reset={reset}]")

    # Handle reset request
    if reset:
        print(f"🔄 Resetting cache state for {filename}")
        cache_file = get_cache_file_path(id)
        if cache_file.exists():
            try:
                cache_file.unlink()
                print(f"🗑️ Cache file removed")
            except Exception as e:
                print(f"⚠️ Error removing cache file: {e}")

        # Reset download status
        with download_locks[id]:
            download_status[id] = {
                'downloading': False,
                'completed': False,
                'file_path': None,
                'download_start': 0,
                'bytes_downloaded': 0,
                'total_bytes': 0,
                'download_speed_mbps': 0,
                'last_access': time.time(),
                'active_users': 0,
                'user_sessions': set()
            }
        print(f"✅ Cache state reset complete")

    # Register user access (for multi-user cache management)
    user_session = request.headers.get('x-session-id') if request else None
    register_user_access(id, user_session)

    try:
        client = get_google_photos_client()

        # Get download URL
        download_data = client.api.get_download_urls(id)

        try:
            download_url = download_data["1"]["5"]["3"]["5"]
        except KeyError:
            try:
                download_url = download_data["1"]["5"]["2"]["6"]
            except KeyError:
                try:
                    download_url = download_data["1"]["5"]["2"]["5"]
                except KeyError:
                    raise HTTPException(status_code=500, detail="No download URL found")

        # Get real file size if not available in metadata
        if file_size == 0:
            try:
                print(f"🔍 Detecting file size via HEAD request...")
                head_response = requests.head(download_url, timeout=10)
                if 'Content-Length' in head_response.headers:
                    file_size = int(head_response.headers['Content-Length'])
                    print(f"✅ Detected file size: {file_size/1024/1024/1024:.1f}GB")
                else:
                    # Fallback: try range request to get size
                    range_response = requests.get(download_url, headers={'Range': 'bytes=0-1'}, timeout=10)
                    if 'Content-Range' in range_response.headers:
                        # Content-Range: bytes 0-1/123456789
                        content_range = range_response.headers['Content-Range']
                        file_size = int(content_range.split('/')[-1])
                        print(f"✅ Detected file size via range: {file_size/1024/1024/1024:.1f}GB")
                    else:
                        file_size = 3500000000  # Last resort fallback
                        print(f"⚠️ Could not detect file size, using fallback: {file_size/1024/1024/1024:.1f}GB")
            except Exception as e:
                file_size = 3500000000  # Fallback on error
                print(f"⚠️ Error detecting file size: {e}, using fallback: {file_size/1024/1024/1024:.1f}GB")

        # 🎬 METADATA CACHE SYSTEM: Check if we have metadata cached
        metadata = load_metadata_cache(id)
        if not metadata:
            print(f"📥 No metadata cache found, will extract on first download")
        else:
            print(f"✅ Metadata cache found: {metadata['filename']} ({metadata['file_size']/1024/1024/1024:.1f}GB)")
            # Update file_size from metadata if it's more accurate
            if metadata['file_size'] > 0 and file_size != metadata['file_size']:
                print(f"🔄 Updating file size from metadata: {file_size/1024/1024/1024:.1f}GB → {metadata['file_size']/1024/1024/1024:.1f}GB")
                file_size = metadata['file_size']

        # Auto-detect position from Range header
        current_start_byte = 0
        range_header = None
        if request and hasattr(request, 'headers'):
            range_header = request.headers.get('range')

        # Initialize cache_file here so it's available in all code paths
        cache_file = get_cache_file_path(id)

        if range_header:
            try:
                range_match = range_header.replace('bytes=', '').split('-')
                requested_start_byte = int(range_match[0]) if range_match[0] else 0
                end_byte = int(range_match[1]) if len(range_match) > 1 and range_match[1] else file_size - 1

                # Check cache availability BEFORE processing the request
                max_available_byte = 0

                if cache_file.exists():
                    cached_size = cache_file.stat().st_size
                    max_available_byte = cached_size - 1  # Last available byte in cache

                    # UPDATE DOWNLOAD STATUS: Check if download completed since last check
                    if cached_size >= file_size * 0.95 and not download_status[id]['completed']:
                        download_status[id]['completed'] = True
                        print(f"✅ Download completed during request! Cache: {cached_size/1024/1024:.0f}MB")

                    # SMART SEEKING: Handle based on download status
                    if download_status[id]['completed']:
                        # Download complete - allow any range
                        current_start_byte = requested_start_byte
                        print(f"✅ Download complete - allowing seek to {requested_start_byte/1024/1024:.1f}MB")
                    elif requested_start_byte > max_available_byte:
                        print(f"🛡️ Seeking beyond cache! Requested: {requested_start_byte/1024/1024:.1f}MB, Available: {max_available_byte/1024/1024:.1f}MB")
                        current_start_byte = max_available_byte  # Limit to cache boundary
                        print(f"🔄 Auto-corrected to maximum available position")
                    else:
                        current_start_byte = requested_start_byte
                        print(f"✅ Seek within cache - allowing {requested_start_byte/1024/1024:.1f}MB")
                else:
                    current_start_byte = 0  # No cache, start from beginning

                # Estimate time position
                total_duration = file_info['duration_ms'] / 1000 if file_info['duration_ms'] else 7200
                bytes_per_second = file_size / total_duration
                current_time = current_start_byte / bytes_per_second
                cache_percentage = (max_available_byte / file_size) * 100 if max_available_byte > 0 else 0

                print(f"🎯 Seeking to: {current_time:.1f}s ({(current_start_byte/file_size)*100:.1f}%) [Cache: {cache_percentage:.1f}%]")
            except Exception as e:
                print(f"❌ Error parsing range header: {e}")
                current_start_byte = 0
                print(f"🎬 Starting from beginning")
        else:
            current_start_byte = 0
            print(f"🎬 Starting from beginning (no range header)")

        # Check if we can serve from cache (we already checked this above, but double-check)
        can_serve_from_cache = False
        if cache_file.exists():
            cached_size = cache_file.stat().st_size

            # Since we already limited current_start_byte to cache boundaries above,
            # we should ALWAYS be able to serve from cache if it exists
            if download_status[id]['completed']:
                can_serve_from_cache = True
                print(f"⚡ File fully cached! Serving from local file")
            elif current_start_byte < cached_size:
                can_serve_from_cache = True
                print(f"⚡ Partial cache available! Serving from local file ({cached_size/1024/1024:.1f}MB cached)")
            else:
                # This should NOT happen with our new smart seeking logic
                print(f"⚠️ UNEXPECTED: Cache miss after smart seeking! This is a bug.")
                print(f"   Requested: {current_start_byte/1024/1024:.1f}MB, Available: {cached_size/1024/1024:.1f}MB")
                # Force to cache boundary as fallback
                current_start_byte = min(current_start_byte, cached_size - 1)
                can_serve_from_cache = True if current_start_byte >= 0 else False

        if can_serve_from_cache:
            cached_size = cache_file.stat().st_size
            print(f"⚡ Serving from cache: {(cached_size/file_size)*100:.1f}% available")

            # Update last_access time when serving from cache
            download_status[id]['last_access'] = time.time()

            # Serve from cached file with range support
            def serve_from_cache():
                bytes_served = 0
                last_activity_update = time.time()

                with open(cache_file, 'rb') as f:
                    f.seek(current_start_byte)

                    # For partial files, only serve what we have
                    cached_size = cache_file.stat().st_size
                    max_bytes_to_serve = min(cached_size - current_start_byte, file_size - current_start_byte)

                    while bytes_served < max_bytes_to_serve:
                        chunk_size = min(8192, max_bytes_to_serve - bytes_served)
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        bytes_served += len(chunk)

                        # Update last_access every 5 seconds while streaming
                        current_time = time.time()
                        if current_time - last_activity_update >= 5:
                            download_status[id]['last_access'] = current_time
                            last_activity_update = current_time
                            print(f"🔄 Streaming activity detected - keeping session alive")

                        yield chunk

                # Final update when streaming completes
                download_status[id]['last_access'] = time.time()
                print(f"✅ Served {bytes_served:,} bytes from cache")

            # Calculate actual bytes we can serve from cache
            cached_size = cache_file.stat().st_size
            remaining_bytes = min(cached_size - current_start_byte, file_size - current_start_byte)

            # Calculate correct end_byte for Content-Range header
            end_byte = min(current_start_byte + remaining_bytes - 1, file_size - 1)

            # Calculate cache progress for headers
            cache_progress_percent = (cached_size / file_size) * 100
            cache_time_available = (cached_size / file_size) * (file_info['duration_ms'] / 1000) if file_info['duration_ms'] else 0

            response_headers = {
                "Accept-Ranges": "bytes",
                "Content-Length": str(remaining_bytes),
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "max-age=3600",
                "X-Cache-Status": "HIT",
                "X-Cache-Source": "LOCAL_FILE",
                "X-Cache-Progress": f"{cache_progress_percent:.1f}%",
                "X-Cache-Time-Available": f"{cache_time_available:.0f}s"
            }

            # Add Content-Range header for partial content (always add for range requests)
            if current_start_byte > 0 or remaining_bytes < file_size:
                response_headers["Content-Range"] = f"bytes {current_start_byte}-{end_byte}/{file_size}"
                print(f"📊 Content-Range: bytes {current_start_byte}-{end_byte}/{file_size} (serving {remaining_bytes:,} bytes)")

            return StreamingResponse(
                serve_from_cache(),
                media_type="video/mp4",
                headers=response_headers,
                status_code=206 if current_start_byte > 0 else 200
            )
        else:
            # 🎬 VIRTUAL MP4 STRATEGY: Use metadata to create virtual MP4 for instant seeking
            if metadata and metadata.get("has_moov") and range_header and current_start_byte > 0:
                print(f"🎭 Creating virtual MP4 with metadata for instant seeking")

                def create_virtual_mp4_stream():
                    try:
                        # Create virtual MP4 header with metadata
                        virtual_header = create_virtual_mp4_header(metadata)

                        if virtual_header:
                            print(f"📦 Virtual MP4 header created ({len(virtual_header)} bytes)")

                            # Serve virtual header first
                            yield virtual_header

                            # Then stream from Google Photos starting from requested position
                            print(f"🌐 Streaming from Google Photos starting at {current_start_byte/1024/1024:.1f}MB")

                            headers = {'Range': f'bytes={current_start_byte}-{file_size-1}'}
                            response = requests.get(download_url, headers=headers, stream=True, timeout=30)

                            if response.status_code in [200, 206]:
                                bytes_served = len(virtual_header)

                                for chunk in response.iter_content(chunk_size=8192):
                                    if chunk:
                                        bytes_served += len(chunk)
                                        yield chunk

                                        # Update activity
                                        if bytes_served % (1024*1024) == 0:  # Every 1MB
                                            download_status[id]['last_access'] = time.time()

                                print(f"✅ Virtual MP4 stream completed: {bytes_served:,} bytes")
                            else:
                                print(f"❌ Google Photos returned status {response.status_code}")
                        else:
                            print(f"⚠️ Could not create virtual header, falling back to redirect")
                            # Fallback to redirect
                            raise Exception("Virtual header creation failed")

                    except Exception as e:
                        print(f"❌ Virtual MP4 streaming error: {e}")
                        # Fallback: redirect to beginning
                        raise e

                try:
                    # Calculate virtual content length (header + requested range)
                    virtual_header = create_virtual_mp4_header(metadata)
                    virtual_content_length = len(virtual_header) + (file_size - current_start_byte)

                    response_headers = {
                        "Accept-Ranges": "bytes",
                        "Content-Length": str(virtual_content_length),
                        "Content-Type": "video/mp4",
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "no-cache",
                        "X-Stream-Strategy": "VIRTUAL_MP4_WITH_METADATA",
                        "X-Virtual-Header-Size": str(len(virtual_header)),
                        "Content-Range": f"bytes {current_start_byte}-{file_size-1}/{file_size}"
                    }

                    print(f"📤 Serving virtual MP4: header({len(virtual_header)}B) + range({current_start_byte/1024/1024:.1f}MB-end)")

                    return StreamingResponse(
                        create_virtual_mp4_stream(),
                        media_type="video/mp4",
                        headers=response_headers,
                        status_code=206
                    )

                except Exception as e:
                    print(f"❌ Virtual MP4 setup failed: {e}")
                    # Fall through to redirect logic below

            # If we have a range request but no cache/metadata, redirect to beginning
            if range_header and current_start_byte > 0:
                print(f"🔄 No cache/metadata available for seeking. Redirecting to beginning to start download.")
                # Return a redirect to the same endpoint without range to start from beginning
                from fastapi.responses import RedirectResponse
                redirect_url = f"/api/files/smart-stream?id={id}"
                return RedirectResponse(url=redirect_url, status_code=302)

            # Start background download if not already started
            if not download_status[id]['downloading'] and not download_status[id]['completed']:
                start_full_download(id, download_url, file_size)
                print(f"🎯 Background download started (ETA: ~7 seconds)")

            # Stream from Google Photos while downloading (only from beginning)
            print(f"🌐 Streaming from Google Photos (download in progress)")

            # Update last_access time when streaming from Google Photos
            download_status[id]['last_access'] = time.time()

            try:
                headers = {'Range': f'bytes={current_start_byte}-{file_size-1}'}
                response = requests.get(download_url, headers=headers, stream=True, timeout=30)

                if response.status_code in [200, 206]:
                    response_headers = {
                        "Accept-Ranges": "bytes",
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "max-age=3600",
                        "X-Cache-Status": "MISS",
                        "X-Cache-Strategy": "DOWNLOAD_AHEAD"
                    }

                    if 'Content-Length' in response.headers:
                        response_headers["Content-Length"] = response.headers['Content-Length']

                    if 'Content-Range' in response.headers:
                        response_headers["Content-Range"] = response.headers['Content-Range']
                        print(f"📊 Content-Range: {response.headers['Content-Range']}")

                    def stream_with_error_handling():
                        try:
                            bytes_served = 0
                            last_activity_update = time.time()

                            for chunk in response.iter_content(chunk_size=8192):
                                if chunk:  # Filter out keep-alive chunks
                                    bytes_served += len(chunk)

                                    # Update last_access every 5 seconds while streaming
                                    current_time = time.time()
                                    if current_time - last_activity_update >= 5:
                                        download_status[id]['last_access'] = current_time
                                        last_activity_update = current_time
                                        print(f"🔄 Google Photos streaming activity - keeping session alive")

                                    yield chunk

                            # Final update when streaming completes
                            download_status[id]['last_access'] = time.time()
                            print(f"✅ Streamed {bytes_served:,} bytes from Google Photos")
                        except Exception as e:
                            print(f"❌ Error during streaming: {e}")
                            raise

                    return StreamingResponse(
                        stream_with_error_handling(),
                        media_type="video/mp4",
                        headers=response_headers,
                        status_code=response.status_code
                    )
                else:
                    print(f"❌ Google Photos returned status {response.status_code}")
                    raise HTTPException(status_code=500, detail=f"Stream request failed: {response.status_code}")
            except requests.exceptions.RequestException as e:
                print(f"❌ Network error streaming from Google Photos: {e}")
                raise HTTPException(status_code=500, detail=f"Network error: {str(e)}")
            except Exception as e:
                print(f"❌ Unexpected error streaming from Google Photos: {e}")
                raise HTTPException(status_code=500, detail=f"Streaming error: {str(e)}")

    except Exception as e:
        print(f"❌ Smart stream error: {e}")
        raise HTTPException(status_code=500, detail=f"Smart stream failed: {str(e)}")

@app.get("/api/files/download-status")
async def get_download_status_all():
    """Get download status for all files"""
    refresh_file_cache()

    status_list = {}

    for file_id, status in download_status.items():
        if file_id in file_cache:
            filename = file_cache[file_id]['filename']
            progress_info = get_download_progress(file_id)

            status_list[file_id] = {
                'filename': filename,
                'status': progress_info['status'],
                'progress': progress_info['progress'],
                'speed_mbps': progress_info['speed_mbps'],
                'eta_seconds': progress_info['eta_seconds'],
                'file_size_gb': status['total_bytes'] / 1024 / 1024 / 1024 if status['total_bytes'] > 0 else 0,
                'downloaded_gb': status['bytes_downloaded'] / 1024 / 1024 / 1024,
                'cache_file_exists': get_cache_file_path(file_id).exists(),
                'last_access_seconds_ago': time.time() - status['last_access'] if status['last_access'] > 0 else 0,
                'will_cleanup_in_seconds': max(0, CACHE_CLEANUP_SECONDS - (time.time() - status['last_access'])) if status['last_access'] > 0 else 0
            }

    return {
        "downloads": status_list,
        "cache_directory": str(cache_dir)
    }

@app.get("/api/files/download-status/{file_id}")
async def get_download_status_single(file_id: str):
    """Get download status for a specific file"""
    refresh_file_cache()

    if file_id not in file_cache:
        raise HTTPException(status_code=404, detail="File not found")

    filename = file_cache[file_id]['filename']
    progress_info = get_download_progress(file_id)
    status = download_status[file_id]

    return {
        'file_id': file_id,
        'filename': filename,
        'status': progress_info['status'],
        'progress': progress_info['progress'],
        'speed_mbps': progress_info['speed_mbps'],
        'eta_seconds': progress_info['eta_seconds'],
        'file_size_gb': status['total_bytes'] / 1024 / 1024 / 1024 if status['total_bytes'] > 0 else 0,
        'downloaded_gb': status['bytes_downloaded'] / 1024 / 1024 / 1024,
        'cache_file_exists': get_cache_file_path(file_id).exists(),
        'download_start_time': status['download_start']
    }



@app.post("/api/files/clear-cache")
async def clear_cache(file_id: str = Query(None, description="File ID to clear (or all if not specified)")):
    """Clear video cache"""
    try:
        if file_id:
            # Clear specific file cache
            cache_file = get_cache_file_path(file_id)

            if cache_file.exists():
                file_size = cache_file.stat().st_size
                cache_file.unlink()

                # Reset download status
                with download_locks[file_id]:
                    download_status[file_id] = {
                        'downloading': False,
                        'completed': False,
                        'file_path': None,
                        'download_start': 0,
                        'bytes_downloaded': 0,
                        'total_bytes': 0,
                        'download_speed_mbps': 0,
                        'last_access': 0,
                        'active_users': 0,
                        'user_sessions': set()
                    }

                return {
                    "message": f"Cache cleared for file {file_id}",
                    "file_size_gb": file_size / 1024 / 1024 / 1024
                }
            else:
                return {
                    "message": f"No cache found for file {file_id}"
                }
        else:
            # Clear all cache
            total_files = 0
            total_size = 0

            for cache_file in cache_dir.glob("*.mp4"):
                try:
                    file_size = cache_file.stat().st_size
                    cache_file.unlink()
                    total_files += 1
                    total_size += file_size
                except Exception as e:
                    print(f"⚠️ Error removing {cache_file}: {e}")

            # Reset all download status
            download_status.clear()

            return {
                "message": "All cache cleared",
                "files_removed": total_files,
                "total_size_gb": total_size / 1024 / 1024 / 1024,
                "cache_directory": str(cache_dir)
            }

    except Exception as e:
        print(f"❌ Cache clear error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to clear cache: {str(e)}")

@app.post("/api/files/heartbeat")
async def video_heartbeat(id: str = Query(..., description="File ID being watched")):
    """Keep video session alive - call this every 10-15 seconds while watching"""
    if id not in file_cache:
        raise HTTPException(status_code=404, detail="File not found")

    # Update last access time
    download_status[id]['last_access'] = time.time()

    # Get current status
    progress_info = get_download_progress(id)
    cache_file = get_cache_file_path(id)
    cache_exists = cache_file.exists()
    cache_size = cache_file.stat().st_size if cache_exists else 0

    return {
        "status": "alive",
        "file_id": id,
        "last_access_updated": True,
        "download_status": progress_info['status'],
        "download_progress": progress_info['progress'],
        "cache_size_mb": cache_size / 1024 / 1024,
        "will_cleanup_in_seconds": max(0, CACHE_CLEANUP_SECONDS - (time.time() - download_status[id]['last_access']))
    }

@app.post("/api/files/extract-all-metadata")
async def extract_all_metadata(
    limit: int = Query(20, description="Maximum files to process (10, 20, 50, 100)"),
    skip_cached: bool = Query(True, description="Skip files that already have metadata cached")
):
    """Extract metadata for MP4 files (first 20MB only) - batch processing"""
    refresh_file_cache()

    # Validate limit
    if limit not in [10, 20, 50, 100]:
        raise HTTPException(status_code=400, detail="Limit must be 10, 20, 50, or 100")

    print(f"🎬 Starting metadata extraction: max {limit} files, skip_cached={skip_cached}")

    total_videos = 0
    processed = 0
    extracted = 0
    skipped_cached = 0
    skipped_errors = 0
    results = []

    try:
        client = get_google_photos_client()

        # Filter video files only and deduplicate by filename
        video_files = {}
        seen_filenames = set()

        for fid, info in file_cache.items():
            if info.get('type') == 'video':
                filename = info['filename']
                if filename not in seen_filenames:
                    video_files[fid] = info
                    seen_filenames.add(filename)
                else:
                    print(f"🔄 Skipping duplicate: {filename}")

        total_videos = len(video_files)
        print(f"📊 Found {total_videos} unique video files (after deduplication)")

        for file_id, file_info in video_files.items():
            # Stop if we've reached the limit
            if processed >= limit:
                print(f"⏹️ Reached limit of {limit} files")
                break

            processed += 1
            filename = file_info['filename']
            file_size = file_info.get('size_bytes', 0)

            print(f"🔍 Processing {processed}/{limit}: {filename}")

            # Skip if already cached
            if skip_cached and load_metadata_cache(file_id):
                skipped_cached += 1
                print(f"⏭️ Skipped (already cached): {filename}")
                results.append({
                    "file_id": file_id,
                    "filename": filename,
                    "status": "skipped_cached",
                    "message": "Metadata already exists"
                })
                continue

            try:
                # Get download URL
                download_data = client.api.get_download_urls(file_id)

                try:
                    download_url = download_data["1"]["5"]["3"]["5"]
                except KeyError:
                    try:
                        download_url = download_data["1"]["5"]["2"]["6"]
                    except KeyError:
                        try:
                            download_url = download_data["1"]["5"]["2"]["5"]
                        except KeyError:
                            raise Exception("No download URL found")

                # Download first 20MB only - SMART STREAMING APPROACH
                print(f"📥 Downloading first 20MB of {filename}...")
                max_download_size = 20 * 1024 * 1024  # 20MB limit

                # Start streaming download
                response = requests.get(download_url, stream=True, timeout=30)
                response.raise_for_status()

                chunk_data = b''
                bytes_downloaded = 0

                # Stream until we hit 20MB limit
                for chunk in response.iter_content(chunk_size=1024*1024):  # 1MB chunks
                    if chunk:
                        chunk_data += chunk
                        bytes_downloaded += len(chunk)

                        # Show progress every 5MB
                        if bytes_downloaded % (5 * 1024 * 1024) == 0:
                            progress = (bytes_downloaded / max_download_size) * 100
                            print(f"📊 Progress: {progress:.0f}% ({bytes_downloaded/1024/1024:.0f}MB/20MB)")

                        # Stop when we reach 20MB
                        if bytes_downloaded >= max_download_size:
                            print(f"🛑 Reached 20MB limit, stopping download")
                            break

                actual_size = len(chunk_data)
                print(f"✅ Downloaded {actual_size/1024/1024:.1f}MB for metadata extraction")

                # Get real file size from Content-Length if available
                if 'Content-Length' in response.headers:
                    real_file_size = int(response.headers['Content-Length'])
                else:
                    real_file_size = file_size if file_size > 0 else actual_size

                # Extract metadata
                print(f"🎬 Extracting MP4 metadata...")
                metadata = extract_mp4_metadata(chunk_data, real_file_size, filename)

                # Save metadata cache
                save_metadata_cache(file_id, metadata)
                extracted += 1

                results.append({
                    "file_id": file_id,
                    "filename": filename,
                    "status": "extracted",
                    "file_size_gb": real_file_size / 1024 / 1024 / 1024,
                    "metadata_size_kb": len(str(metadata)) / 1024,
                    "has_moov": metadata.get("has_moov", False),
                    "duration_seconds": metadata.get("duration_ms", 0) / 1000
                })

                print(f"✅ Metadata extracted for {filename}")

            except Exception as e:
                skipped_errors += 1
                error_msg = str(e)
                print(f"❌ Error processing {filename}: {error_msg}")
                results.append({
                    "file_id": file_id,
                    "filename": filename,
                    "status": "error",
                    "error": error_msg
                })

        # Summary
        summary = {
            "total_videos_available": total_videos,
            "processed": processed,
            "extracted": extracted,
            "skipped_cached": skipped_cached,
            "skipped_errors": skipped_errors,
            "limit_used": limit,
            "metadata_cache_dir": str(metadata_cache_dir),
            "results": results
        }

        print(f"🎉 Metadata extraction complete!")
        print(f"📊 Summary: {extracted} extracted, {skipped_cached} cached, {skipped_errors} errors")

        return summary

    except Exception as e:
        print(f"❌ Metadata extraction failed: {e}")
        raise HTTPException(status_code=500, detail=f"Metadata extraction failed: {str(e)}")

@app.get("/api/files/metadata-status")
async def get_metadata_status():
    """Get metadata cache status for all video files"""
    refresh_file_cache()

    video_files = {fid: info for fid, info in file_cache.items()
                  if info.get('type') == 'video'}

    total_videos = len(video_files)
    cached_count = 0
    missing_count = 0
    results = []

    for file_id, file_info in video_files.items():
        metadata = load_metadata_cache(file_id)

        if metadata:
            cached_count += 1
            status = "cached"
            metadata_info = {
                "has_moov": metadata.get("has_moov", False),
                "duration_seconds": metadata.get("duration_ms", 0) / 1000,
                "file_size_gb": metadata.get("file_size", 0) / 1024 / 1024 / 1024,
                "created": metadata.get("created", 0)
            }
        else:
            missing_count += 1
            status = "missing"
            metadata_info = None

        results.append({
            "file_id": file_id,
            "filename": file_info['filename'],
            "status": status,
            "metadata": metadata_info
        })

    return {
        "total_videos": total_videos,
        "cached_count": cached_count,
        "missing_count": missing_count,
        "cache_percentage": (cached_count / total_videos * 100) if total_videos > 0 else 0,
        "metadata_cache_dir": str(metadata_cache_dir),
        "files": results
    }

@app.post("/api/files/force-cleanup")
async def force_cleanup(force_all: bool = Query(False, description="Force cleanup all files regardless of access time")):
    """Force immediate cleanup of inactive cache"""
    try:
        if force_all:
            print("🔥 FORCE CLEANUP ALL - Ignoring access times")
            total_files = 0
            total_size = 0

            for cache_file in cache_dir.glob("*.mp4"):
                try:
                    file_size = cache_file.stat().st_size
                    cache_file.unlink()
                    total_files += 1
                    total_size += file_size
                    print(f"🗑️ Force deleted: {cache_file.name}")
                except Exception as e:
                    print(f"⚠️ Could not delete {cache_file.name}: {e}")

            # Clear all download status
            download_status.clear()

            return {
                "message": f"Force cleanup completed - {total_files} files removed",
                "files_removed": total_files,
                "total_size_gb": total_size / 1024 / 1024 / 1024
            }
        else:
            cleanup_inactive_cache()
            return {
                "message": "Normal cleanup completed",
                "cleanup_threshold_seconds": CACHE_CLEANUP_SECONDS
            }
    except Exception as e:
        print(f"❌ Force cleanup error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to force cleanup: {str(e)}")

@app.post("/api/cache/reset")
@app.get("/api/cache/reset")
async def reset_cache():
    """Reset the file cache - forces refresh from Google Photos"""
    global file_cache

    print("🔄 Resetting file cache...")

    try:
        # Clear the in-memory cache
        old_count = len(file_cache)
        file_cache.clear()
        print(f"🗑️ Cleared {old_count} files from memory cache")

        # Force refresh from Google Photos
        refresh_file_cache()
        new_count = len(file_cache)

        print(f"✅ Cache reset complete: {old_count} → {new_count} files")

        return {
            "success": True,
            "message": "Cache reset successfully",
            "old_count": old_count,
            "new_count": new_count,
            "files_removed": old_count - new_count,
            "files_added": max(0, new_count - old_count)
        }

    except Exception as e:
        print(f"❌ Cache reset failed: {e}")
        raise HTTPException(status_code=500, detail=f"Cache reset failed: {str(e)}")

@app.post("/api/movies/refresh")
async def refresh_movies():
    """Refresh movie cache to detect new uploads - optimized for UI"""
    global file_cache, cache_timestamp

    old_count = len(file_cache)
    old_mp4_count = sum(1 for f in file_cache.values() if f['type'] == 'video' and f['filename'].lower().endswith('.mp4'))

    print(f"🎬 Refreshing movie cache (current: {old_mp4_count} MP4 movies)...")

    # Clear cache and force refresh
    file_cache.clear()
    cache_timestamp = 0
    refresh_file_cache()

    new_count = len(file_cache)
    new_mp4_count = sum(1 for f in file_cache.values() if f['type'] == 'video' and f['filename'].lower().endswith('.mp4'))

    print(f"✅ Movies refreshed: {old_mp4_count} → {new_mp4_count} MP4 videos")

    return {
        "success": True,
        "message": "Movie cache refreshed successfully",
        "old_total_files": old_count,
        "new_total_files": new_count,
        "old_mp4_count": old_mp4_count,
        "new_mp4_count": new_mp4_count,
        "mp4_difference": new_mp4_count - old_mp4_count,
        "timestamp": cache_timestamp
    }

@app.post("/api/cache/clear")
@app.get("/api/cache/clear")
async def clear_cache():
    """Clear both file cache and download cache"""
    global file_cache, download_status

    print("🧹 Clearing all caches...")

    try:
        # Clear file cache
        old_file_count = len(file_cache)
        file_cache.clear()

        # Clear download status
        old_download_count = len(download_status)
        download_status.clear()

        # Clear physical cache files
        cache_files_removed = 0
        if cache_dir.exists():
            for cache_file in cache_dir.glob("*.mp4"):
                try:
                    cache_file.unlink()
                    cache_files_removed += 1
                    print(f"🗑️ Removed cache file: {cache_file.name}")
                except Exception as e:
                    print(f"⚠️ Could not remove {cache_file.name}: {e}")

        print(f"✅ Cache clear complete:")
        print(f"   📋 File cache: {old_file_count} files cleared")
        print(f"   📊 Download status: {old_download_count} entries cleared")
        print(f"   💾 Physical files: {cache_files_removed} files removed")

        return {
            "success": True,
            "message": "All caches cleared successfully",
            "file_cache_cleared": old_file_count,
            "download_status_cleared": old_download_count,
            "physical_files_removed": cache_files_removed
        }

    except Exception as e:
        print(f"❌ Cache clear failed: {e}")
        raise HTTPException(status_code=500, detail=f"Cache clear failed: {str(e)}")

@app.get("/api/cache/auto-refresh/status")
async def get_auto_refresh_status():
    """Get auto-refresh status and configuration"""
    global auto_refresh_task, last_auto_refresh

    task_status = "stopped"
    if auto_refresh_task:
        if auto_refresh_task.done():
            task_status = "completed"
        elif auto_refresh_task.cancelled():
            task_status = "cancelled"
        else:
            task_status = "running"

    next_refresh_in = 0
    if AUTO_REFRESH_ENABLED and last_auto_refresh > 0:
        elapsed = time.time() - last_auto_refresh
        next_refresh_in = max(0, (AUTO_REFRESH_INTERVAL_MINUTES * 60) - elapsed)

    return {
        "enabled": AUTO_REFRESH_ENABLED,
        "interval_minutes": AUTO_REFRESH_INTERVAL_MINUTES,
        "task_status": task_status,
        "last_refresh_ago_seconds": time.time() - last_auto_refresh if last_auto_refresh > 0 else 0,
        "next_refresh_in_seconds": next_refresh_in,
        "next_refresh_in_minutes": next_refresh_in / 60,
        "cached_files": len(file_cache)
    }

@app.post("/api/cache/auto-refresh/configure")
async def configure_auto_refresh(
    enabled: bool = Query(True, description="Enable/disable auto-refresh"),
    interval_minutes: int = Query(15, description="Refresh interval in minutes")
):
    """Configure auto-refresh settings"""
    global AUTO_REFRESH_ENABLED, AUTO_REFRESH_INTERVAL_MINUTES

    old_enabled = AUTO_REFRESH_ENABLED
    old_interval = AUTO_REFRESH_INTERVAL_MINUTES

    AUTO_REFRESH_ENABLED = enabled
    AUTO_REFRESH_INTERVAL_MINUTES = max(1, interval_minutes)  # Minimum 1 minute

    # Restart auto-refresh with new settings
    if enabled:
        await stop_auto_refresh()
        await start_auto_refresh()
        message = f"Auto-refresh restarted with {AUTO_REFRESH_INTERVAL_MINUTES}min interval"
    else:
        await stop_auto_refresh()
        message = "Auto-refresh disabled"

    return {
        "success": True,
        "message": message,
        "old_settings": {
            "enabled": old_enabled,
            "interval_minutes": old_interval
        },
        "new_settings": {
            "enabled": AUTO_REFRESH_ENABLED,
            "interval_minutes": AUTO_REFRESH_INTERVAL_MINUTES
        }
    }

@app.post("/api/cache/auto-refresh/trigger")
async def trigger_auto_refresh():
    """Manually trigger an auto-refresh now"""
    try:
        old_count = len(file_cache)
        print("🔄 Manual auto-refresh triggered")

        refresh_file_cache()
        new_count = len(file_cache)

        global last_auto_refresh
        last_auto_refresh = time.time()

        return {
            "success": True,
            "message": "Manual refresh completed",
            "old_count": old_count,
            "new_count": new_count,
            "files_changed": new_count - old_count
        }
    except Exception as e:
        print(f"❌ Manual refresh error: {e}")
        raise HTTPException(status_code=500, detail=f"Manual refresh failed: {str(e)}")

@app.get("/api/files/stream-direct")
async def stream_direct_redirect(id: str = Query(..., description="File ID")):
    """Direct streaming redirect - Client streams directly from Google Photos (0 bandwidth usage)"""
    print(f"🔗 Stream-direct request for ID: {id}")

    refresh_file_cache()
    print(f"📋 Cache contains {len(file_cache)} files")

    if id not in file_cache:
        print(f"❌ File ID {id} not found in cache")
        print(f"🔍 Available IDs (first 5): {list(file_cache.keys())[:5]}")
        raise HTTPException(status_code=404, detail=f"File not found. Cache has {len(file_cache)} files.")

    file_info = file_cache[id]
    filename = file_info['filename']

    if file_info['type'] != 'video':
        print(f"❌ File {filename} is not a video (type: {file_info['type']})")
        raise HTTPException(status_code=400, detail="File is not a video")

    print(f"🔗 Direct stream redirect for: {filename}")

    try:
        # Get the download URL from Google Photos API
        client = get_google_photos_client()
        download_data = client.api.get_download_urls(id)

        # Extract download URL (using the corrected path from our previous fix)
        try:
            streaming_url = download_data["1"]["5"]["3"]["5"]
        except KeyError:
            try:
                streaming_url = download_data["1"]["5"]["2"]["6"]
            except KeyError:
                try:
                    streaming_url = download_data["1"]["5"]["2"]["5"]
                except KeyError:
                    raise HTTPException(status_code=500, detail="No streaming URL found")

        print(f"🚀 Redirecting to Google Photos streaming URL (0 server bandwidth)")
        print(f"   📊 Client will stream directly from: googlevideo.com")

        # Return direct redirect - client streams from Google Photos
        from fastapi.responses import RedirectResponse
        return RedirectResponse(
            url=streaming_url,
            status_code=302,
            headers={
                'Cache-Control': 'no-cache',
                'X-Stream-Type': 'direct-google-redirect',
                'X-Bandwidth-Usage': '0-bytes-server-side'
            }
        )

    except Exception as e:
        print(f"❌ Direct stream redirect error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get streaming URL: {str(e)}")





if __name__ == "__main__":
    print("================================================================================")
    print("📋 API ENDPOINTS - Complete Reference")
    print("================================================================================")
    print("")
    print("🗂️  FILE LISTING:")
    print("   📋 List MP4s (deduplicated):  http://localhost:8000/api/files/mp4")
    print("   📋 List MP4s (with duplicates): http://localhost:8000/api/files/mp4-raw")
    print("   📋 List All Files:            http://localhost:8000/api/files/all")
    print("   ℹ️  File Info:                 http://localhost:8000/api/files/info?id=FILE_ID")
    print("")
    print("📥 DOWNLOAD & STREAMING:")
    print("   📥 Download (proxy):           http://localhost:8000/api/files/download?id=FILE_ID")
    print("   🔗 Download (direct redirect): http://localhost:8000/api/files/downloadDirect?id=FILE_ID")
    print("   🔗 Stream (direct, 0 bandwidth): http://localhost:8000/api/files/stream-direct?id=FILE_ID")
    print("   🚀 Smart Stream (cache + seek):  http://localhost:8000/api/files/smart-stream?id=FILE_ID")
    print("")
    print("🔗 URL EXTRACTION:")
    print("   🔗 Direct URL (JSON):          http://localhost:8000/api/files/direct-url?id=FILE_ID")
    print("   🌐 Google Streaming URL:       http://localhost:8000/api/files/google-url?id=FILE_ID")
    print("")
    print("🎬 METADATA SYSTEM:")
    print("   📊 Metadata Status:           http://localhost:8000/api/files/metadata-status")
    print("   🔄 Extract Metadata (batch):  POST http://localhost:8000/api/files/extract-all-metadata?limit=20")
    print("")
    print("📊 STATUS & MONITORING:")
    print("   📊 Download Status (all):      http://localhost:8000/api/files/download-status")
    print("   📊 Download Status (single):   http://localhost:8000/api/files/download-status/FILE_ID")
    print("   💓 Video Heartbeat:           POST http://localhost:8000/api/files/heartbeat?id=FILE_ID")
    print("   🔧 Debug Info:                http://localhost:8000/debug")
    print("")
    print("🧹 CACHE MANAGEMENT:")
    print("   🔄 Reset Cache (refresh):      http://localhost:8000/api/cache/reset")
    print("   🎬 Refresh Movies (UI):        POST http://localhost:8000/api/movies/refresh")
    print("   🗑️  Clear All Cache:           http://localhost:8000/api/cache/clear")
    print("   🗑️  Clear Specific Cache:      POST http://localhost:8000/api/files/clear-cache?file_id=FILE_ID")
    print("   🚨 Force Cleanup:             POST http://localhost:8000/api/files/force-cleanup")
    print("   ⏰ Auto-refresh Status:        http://localhost:8000/api/cache/auto-refresh/status")
    print("   ⚙️  Auto-refresh Configure:    POST http://localhost:8000/api/cache/auto-refresh/configure")
    print("   🚀 Auto-refresh Trigger:       POST http://localhost:8000/api/cache/auto-refresh/trigger")
    print("")
    print("📚 DOCUMENTATION & HELP:")
    print("   📚 FastAPI Auto Docs:         http://localhost:8000/docs")
    print("   📋 Interactive API Docs:      http://localhost:8000/docs-ui")
    print("   📖 API Root (info):           http://localhost:8000/")
    print("   🎬 Movie UI (TMDB + Streaming): http://localhost:8000/ui")
    print("")
    print("💡 QUICK EXAMPLES:")
    print("   🔗 Watch (direct, 0 bandwidth): http://localhost:8000/api/files/stream-direct?id=AF1QipMH86yETEN4dL0RbsUwlCsFunvuOB_SusWXfpJB")
    print("   🚀 Watch (smart cache):      http://localhost:8000/api/files/smart-stream?id=AF1QipMH86yETEN4dL0RbsUwlCsFunvuOB_SusWXfpJB")
    print("   📥 Download a file:           http://localhost:8000/api/files/download?id=AF1QipMH86yETEN4dL0RbsUwlCsFunvuOB_SusWXfpJB")
    print("")
    print("✨ FEATURES:")
    print("   🎯 Smart-stream: Download-ahead caching with instant seeking")
    print("   🔗 Direct streaming: 0 server bandwidth for all apps")
    print("   📦 Metadata cache: Extract MP4 metadata (20MB → 1.4KB files)")
    print("   🧹 Auto-cleanup: Smart cache management with configurable thresholds")
    print("")
    print("================================================================================")
    print("⚠️  Press Ctrl+C to stop the server")
    print("🌐 Server running on: http://localhost:8000")
    print("🎬 Movie UI available at: http://localhost:8000/ui")
    print("📋 Interactive API Docs: http://localhost:8000/docs-ui")
    print("================================================================================")

    uvicorn.run(
        "google_photos_api:app",
        host="0.0.0.0",
        port=7860,  # Default port for Hugging Face Spaces
        reload=False,  # Disable reload in production
        log_level="info"
    )
