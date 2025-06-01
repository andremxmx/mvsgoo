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

# Hybrid RAM + Disk Cache System for Video Streaming
# 5 minutes backward, 15 minutes forward, 1GB max per movie
import hashlib

# Download-Ahead Cache System - Simple and Fast
cache_dir = Path(__file__).parent / "video_cache"
cache_dir.mkdir(exist_ok=True)

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
            print(f"🚀 Starting full download: {file_size/1024/1024/1024:.1f}GB")

            response = requests.get(download_url, stream=True)
            if response.status_code == 200:
                with open(cache_file, 'wb') as f:
                    bytes_downloaded = 0
                    start_time = time.time()

                    for chunk in response.iter_content(chunk_size=1024*1024):  # 1MB chunks
                        if chunk:
                            f.write(chunk)
                            bytes_downloaded += len(chunk)

                            # Update progress
                            download_status[file_id]['bytes_downloaded'] = bytes_downloaded

                            # Calculate speed every 1MB
                            if bytes_downloaded % (10 * 1024 * 1024) == 0:  # Every 10MB
                                elapsed = time.time() - start_time
                                speed_mbps = (bytes_downloaded / 1024 / 1024) / max(elapsed, 0.1)
                                download_status[file_id]['download_speed_mbps'] = speed_mbps

                                progress = (bytes_downloaded / file_size) * 100
                                print(f"📥 Download progress: {progress:.1f}% ({speed_mbps:.0f} MB/s)")

                # Mark as completed
                download_status[file_id]['downloading'] = False
                download_status[file_id]['completed'] = True

                elapsed = time.time() - start_time
                final_speed = (file_size / 1024 / 1024) / max(elapsed, 0.1)
                print(f"✅ Download completed: {file_size/1024/1024/1024:.1f}GB in {elapsed:.1f}s ({final_speed:.0f} MB/s)")

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
            progress = (status['bytes_downloaded'] / status['total_bytes']) * 100
            remaining_bytes = status['total_bytes'] - status['bytes_downloaded']
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

        # Don't cleanup files that are currently downloading, recently accessed, or completed
        is_downloading = status['downloading']
        is_recently_accessed = time_since_access <= CACHE_CLEANUP_SECONDS
        has_no_access_time = status['last_access'] == 0
        is_completed_and_recent = status['completed'] and time_since_access <= (CACHE_CLEANUP_SECONDS * 2)  # Give completed files more time

        print(f"📊 {file_id[:8]}... - downloading: {is_downloading}, completed: {status['completed']}, inactive: {time_since_access:.1f}s, threshold: {CACHE_CLEANUP_SECONDS}s")

        if not is_downloading and not is_recently_accessed and not has_no_access_time and not is_completed_and_recent:
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

    # Clear any residual cache from previous sessions
    clear_residual_cache()

    # Initialize components
    get_google_photos_client()
    refresh_file_cache()
    start_cleanup_task()

    print("✅ API ready!")

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
                "full_video": "/api/files/stream?id=xxx",
                "fast_seek": "/api/files/fast-seek?id=xxx&t=1800&duration=30",
                "smart_download": "/api/files/smart-stream?id=xxx"
            },
            "info": "/api/files/info?id=xxx",
            "download_status": "/api/files/download-status",
            "download_progress": "/api/files/download-status/{file_id}",
            "heartbeat": "/api/files/heartbeat?id=xxx",
            "docs": "/docs"
        },
        "examples": {
            "smart_download": "/api/files/smart-stream?id=FILE_ID",
            "download_progress": "/api/files/download-status/FILE_ID",
            "fast_seek_30min": "/api/files/fast-seek?id=FILE_ID&t=1800&duration=30",
            "download_direct": "/api/files/downloadDirect?id=FILE_ID"
        }
    }

@app.get("/api/files/mp4")
async def list_mp4_files():
    """List all MP4 video files"""
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
    
    return {
        "count": len(mp4_files),
        "files": mp4_files
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

@app.get("/api/files/fast-seek")
async def fast_seek_stream(
    id: str = Query(..., description="File ID"),
    t: float = Query(0, description="Time in seconds to seek to"),
    duration: int = Query(10, description="Duration of segment in seconds")
):
    """Fast seeking stream - pre-calculate byte ranges for instant seeking"""
    refresh_file_cache()

    if id not in file_cache:
        raise HTTPException(status_code=404, detail="File not found")

    file_info = file_cache[id]
    filename = file_info['filename']

    print(f"⚡ Fast seek request for: {filename} at {t}s")

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

        # Calculate byte range based on time
        total_duration = file_info['duration_ms'] / 1000 if file_info['duration_ms'] else 7200
        file_size = file_info['size_bytes'] if file_info['size_bytes'] > 0 else 3500000000

        # Calculate bytes per second
        bytes_per_second = file_size / total_duration

        # Calculate start byte for the requested time
        start_byte = int(t * bytes_per_second)

        # Calculate end byte for the duration
        end_byte = int((t + duration) * bytes_per_second) - 1

        # Ensure we don't exceed file size
        end_byte = min(end_byte, file_size - 1)

        print(f"📊 Seek calculation:")
        print(f"   ⏱️ Time: {t}s - {t + duration}s")
        print(f"   📏 Bytes: {start_byte:,} - {end_byte:,}")
        print(f"   💾 Size: {(end_byte - start_byte + 1) / 1024 / 1024:.1f} MB")

        # Make range request
        headers = {'Range': f'bytes={start_byte}-{end_byte}'}
        response = requests.get(download_url, headers=headers, stream=True)

        if response.status_code in [200, 206]:
            # Prepare headers without fixed Content-Length (let it be dynamic)
            response_headers = {
                "Accept-Ranges": "bytes",
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "max-age=3600"
            }

            # Only add Content-Length if Google Photos provides it
            if 'Content-Length' in response.headers:
                response_headers["Content-Length"] = response.headers['Content-Length']

            # Add Content-Range if available
            if 'Content-Range' in response.headers:
                response_headers["Content-Range"] = response.headers['Content-Range']
            else:
                response_headers["Content-Range"] = f"bytes {start_byte}-{end_byte}/{file_size}"

            return StreamingResponse(
                response.iter_content(chunk_size=8192),
                media_type="video/mp4",
                headers=response_headers
            )
        else:
            raise HTTPException(status_code=500, detail=f"Range request failed: {response.status_code}")

    except Exception as e:
        print(f"❌ Fast seek error: {e}")
        raise HTTPException(status_code=500, detail=f"Fast seek failed: {str(e)}")

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
    file_size = file_info['size_bytes'] if file_info['size_bytes'] > 0 else 3500000000

    print(f"🚀 Smart stream request for: {filename} ({file_size/1024/1024/1024:.1f}GB) [reset={reset}]")

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

                    # SMART SEEKING: Limit to cache boundaries
                    if requested_start_byte > max_available_byte:
                        print(f"🛡️ Seeking beyond cache! Requested: {requested_start_byte/1024/1024:.1f}MB, Available: {max_available_byte/1024/1024:.1f}MB")
                        current_start_byte = max_available_byte  # Limit to cache boundary
                        print(f"🔄 Auto-corrected to maximum available position")
                    else:
                        current_start_byte = requested_start_byte
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
            # If we have a range request but no cache, redirect to beginning
            if range_header and current_start_byte > 0:
                print(f"🔄 No cache available for seeking. Redirecting to beginning to start download.")
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

@app.get("/api/files/stream")
async def stream_file(id: str = Query(..., description="File ID"), request: Request = None):
    """Stream a video file by ID with range support"""
    refresh_file_cache()
    
    if id not in file_cache:
        raise HTTPException(status_code=404, detail="File not found")
    
    file_info = file_cache[id]
    filename = file_info['filename']
    
    if file_info['type'] != 'video':
        raise HTTPException(status_code=400, detail="File is not a video")
    
    print(f"🎬 Stream request: {filename}")

    try:
        client = get_google_photos_client()
        download_data = client.api.get_download_urls(id)

        # Extract download URL (using the corrected path from our previous fix)
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
        
        # Handle range requests for video streaming
        range_header = None
        if request and hasattr(request, 'headers'):
            range_header = request.headers.get('range')
        
        if range_header:
            try:
                # Parse range header
                range_match = range_header.replace('bytes=', '').split('-')
                start = int(range_match[0]) if range_match[0] else 0
                end = int(range_match[1]) if range_match[1] else None

                # Make range request to Google Photos
                headers = {'Range': f'bytes={start}-{end if end else ""}'}
                response = requests.get(download_url, headers=headers, stream=True, timeout=30)

                if response.status_code == 206:  # Partial Content
                    return StreamingResponse(
                        response.iter_content(chunk_size=8192),
                        status_code=206,
                        headers={
                            'Content-Range': response.headers.get('Content-Range', ''),
                            'Accept-Ranges': 'bytes',
                            'Content-Length': response.headers.get('Content-Length', ''),
                            'Content-Type': 'video/mp4'
                        }
                    )
            except Exception as e:
                print(f"⚠️ Range request failed, falling back to regular streaming: {e}")
        
        # Regular streaming without range
        response = requests.get(download_url, stream=True)
        response.raise_for_status()
        
        # Prepare headers for streaming
        stream_headers = {
            'Accept-Ranges': 'bytes',
            'Content-Type': 'video/mp4'
        }

        # Add Content-Length if available
        content_length = response.headers.get('Content-Length')
        if content_length:
            stream_headers['Content-Length'] = content_length
        elif file_info['size_bytes'] and file_info['size_bytes'] > 0:
            stream_headers['Content-Length'] = str(file_info['size_bytes'])

        return StreamingResponse(
            response.iter_content(chunk_size=8192),
            media_type='video/mp4',
            headers=stream_headers
        )
        
    except Exception as e:
        print(f"❌ Stream error: {e}")
        raise HTTPException(status_code=500, detail=f"Stream failed: {str(e)}")



if __name__ == "__main__":
    print("🚀 Starting Google Photos API Server...")
    print("📋 Available endpoints:")
    print("   http://localhost:7860/api/files/mp4")
    print("   http://localhost:7860/api/files/smart-stream?id=xxx")
    print("   http://localhost:7860/api/files/download?id=xxx")
    print("   http://localhost:7860/docs (API documentation)")

    uvicorn.run(
        "google_photos_api:app",
        host="0.0.0.0",
        port=7860,  # Default port for Hugging Face Spaces
        reload=False,  # Disable reload in production
        log_level="info"
    )
