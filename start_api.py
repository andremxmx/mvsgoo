#!/usr/bin/env python3
"""
🚀 Google Photos API Launcher

Quick launcher for the Google Photos API server
"""

import subprocess
import sys
import os
from pathlib import Path

def load_env_file():
    """Load environment variables from .env file"""
    env_file = Path(".env")

    if env_file.exists():
        print("📄 Loading .env file...")

        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key] = value
                    print(f"   ✅ Loaded: {key}")

        print("✅ Environment variables loaded from .env")
        return True
    else:
        print("❌ .env file not found")
        return False

def install_dependencies():
    """Install required dependencies"""
    print("📦 Installing API dependencies...")

    try:
        # Install dependencies silently (suppress output)
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "fastapi>=0.104.0",
            "uvicorn[standard]>=0.24.0",
            "python-multipart>=0.0.6",
            "--quiet"  # Suppress pip output
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("✅ Dependencies installed successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to install dependencies: {e}")
        return False

def check_google_photos_auth():
    """Check if Google Photos authentication is working"""
    print("🔐 Checking Google Photos authentication...")

    try:
        # Add the gpm directory to the path
        sys.path.insert(0, str(Path(__file__).parent / "gpm"))
        from gpmc import Client

        # Use auth data from environment variable
        auth_data = os.environ.get('GP_AUTH_DATA')
        if auth_data:
            client = Client(auth_data=auth_data)
            print("✅ Google Photos authentication working")
            return True
        else:
            print("❌ GP_AUTH_DATA not found in environment")
            return False

    except Exception as e:
        print(f"❌ Google Photos authentication failed: {e}")
        print("💡 Make sure your authentication token is configured")
        return False

def start_api_server():
    """Start the API server"""
    print("🚀 Starting Google Photos API Server...")
    print("=" * 80)
    print("📋 API ENDPOINTS - Complete Reference")
    print("=" * 80)

    print("\n🗂️  FILE LISTING:")
    print("   📋 List MP4s (deduplicated):  http://localhost:8000/api/files/mp4")
    print("   📋 List MP4s (with duplicates): http://localhost:8000/api/files/mp4-raw")
    print("   📋 List All Files:            http://localhost:8000/api/files/all")
    print("   ℹ️  File Info:                 http://localhost:8000/api/files/info?id=FILE_ID")

    print("\n📥 DOWNLOAD & STREAMING:")
    print("   📥 Download (proxy):           http://localhost:8000/api/files/download?id=FILE_ID")
    print("   🔗 Download (direct redirect): http://localhost:8000/api/files/downloadDirect?id=FILE_ID")
    print("   🎬 Stream (proxy, uses bandwidth): http://localhost:8000/api/files/stream?id=FILE_ID")
    print("   🔗 Stream (direct, 0 bandwidth): http://localhost:8000/api/files/stream-direct?id=FILE_ID")
    print("   🚀 Smart Stream (cache local):  http://localhost:8000/api/files/smart-stream?id=FILE_ID")
    print("   ⚡ Fast Seek:                 http://localhost:8000/api/files/fast-seek?id=FILE_ID&t=60")

    print("\n🔗 URL EXTRACTION:")
    print("   🔗 Direct URL (JSON):          http://localhost:8000/api/files/direct-url?id=FILE_ID")
    print("   🌐 Google Streaming URL:       http://localhost:8000/api/files/google-url?id=FILE_ID")

    print("\n📊 STATUS & MONITORING:")
    print("   📊 Download Status (all):      http://localhost:8000/api/files/download-status")
    print("   📊 Download Status (single):   http://localhost:8000/api/files/download-status/FILE_ID")
    print("   🔧 Debug Info:                http://localhost:8000/debug")

    print("\n🧹 CACHE MANAGEMENT:")
    print("   🔄 Reset Cache (refresh):      http://localhost:8000/api/cache/reset")
    print("   🗑️  Clear All Cache:           http://localhost:8000/api/cache/clear")
    print("   ⏰ Auto-refresh Status:        http://localhost:8000/api/cache/auto-refresh/status")
    print("   ⚙️  Auto-refresh Configure:    http://localhost:8000/api/cache/auto-refresh/configure")
    print("   🚀 Auto-refresh Trigger:       http://localhost:8000/api/cache/auto-refresh/trigger")

    print("\n📚 DOCUMENTATION & HELP:")
    print("   📚 Interactive API Docs:      http://localhost:8000/docs")
    print("   📖 API Root (info):           http://localhost:8000/")
    print("   🎬 Movie UI (TMDB + Streaming): http://localhost:8000/ui")

    print("\n💡 QUICK EXAMPLES:")
    print("   🎬 Watch (proxy stream):      http://localhost:8000/api/files/stream?id=AF1QipMH86yETEN4dL0RbsUwlCsFunvuOB_SusWXfpJB")
    print("   🔗 Watch (direct, 0 bandwidth): http://localhost:8000/api/files/stream-direct?id=AF1QipMH86yETEN4dL0RbsUwlCsFunvuOB_SusWXfpJB")
    print("   📥 Download a file:           http://localhost:8000/api/files/download?id=AF1QipMH86yETEN4dL0RbsUwlCsFunvuOB_SusWXfpJB")
    print("   ⚡ Seek to 2 minutes:         http://localhost:8000/api/files/fast-seek?id=AF1QipMH86yETEN4dL0RbsUwlCsFunvuOB_SusWXfpJB&t=120")

    print("\n" + "=" * 80)
    print("⚠️  Press Ctrl+C to stop the server")
    print("🌐 Server running on: http://localhost:8000")
    print("🎬 Movie UI available at: http://localhost:8000/ui")
    print("=" * 80)
    print()
    
    try:
        # Import and run the API
        import uvicorn
        from google_photos_api import app
        
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=8000,
            log_level="info"
        )
        
    except KeyboardInterrupt:
        print("\n🛑 Server stopped by user")
        return True
    except Exception as e:
        print(f"\n❌ Server failed to start: {e}")
        return False

def main():
    """Main launcher function"""
    print("🎬 Google Photos API Launcher")
    print("=" * 40)

    # Step 0: Load environment variables
    if not load_env_file():
        print("⚠️ Warning: No .env file found, using system environment variables")

    # Step 1: Install dependencies
    if not install_dependencies():
        print("❌ Cannot continue without dependencies")
        return False

    # Step 2: Check authentication
    if not check_google_photos_auth():
        print("❌ Cannot continue without Google Photos authentication")
        return False

    # Step 3: Start server
    print("\n🚀 All checks passed! Starting API server...")
    return start_api_server()

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n🛑 Launcher interrupted")
        sys.exit(0)
