#!/usr/bin/env python3
"""
🎬 Movie Downloader with TMDB Integration

Downloads movies from JSONL file with TMDB names:
- Reads JSONL with tmdb_id and download URL
- Gets movie name from TMDB API
- Downloads with format: MovieName_tmdbid.mp4
- Saves to movies/ folder
- Batch processing (4 movies at a time)

Usage:
    python download/download_movies.py --batch 4 --input download/movies.jsonl
"""

import json
import os
import sys
import requests
import argparse
from pathlib import Path
import time
from urllib.parse import urlparse
import re

# TMDB Configuration
TMDB_API_KEY = '04a646a3d3b703752123ed76e1ecc62f'
TMDB_BASE_URL = 'https://api.themoviedb.org/3'

def clean_filename(filename):
    """Clean filename for filesystem compatibility"""
    # Remove invalid characters for filenames
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)
    # Replace spaces with underscores
    filename = filename.replace(' ', '_')
    # Remove multiple underscores
    filename = re.sub(r'_+', '_', filename)
    # Remove leading/trailing underscores
    filename = filename.strip('_')
    return filename

def get_movie_name_from_tmdb(tmdb_id):
    """Get movie name from TMDB API"""
    try:
        url = f"{TMDB_BASE_URL}/movie/{tmdb_id}?api_key={TMDB_API_KEY}"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            title = data.get('title', f'Movie_{tmdb_id}')
            return clean_filename(title)
        else:
            print(f"❌ TMDB API error for ID {tmdb_id}: {response.status_code}")
            return f"Movie_{tmdb_id}"
            
    except Exception as e:
        print(f"❌ Error fetching TMDB data for ID {tmdb_id}: {e}")
        return f"Movie_{tmdb_id}"

def download_file(url, filepath, movie_info):
    """Download file with progress"""
    try:
        print(f"📥 Downloading: {filepath.name}")
        print(f"   📊 Size: {movie_info.get('size', 'Unknown')}")
        print(f"   🎬 Quality: {movie_info.get('quality', 'Unknown')}")

        # Add headers to avoid 403 errors
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://hakunaymatata.com/',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive'
        }

        response = requests.get(url, stream=True, timeout=30, headers=headers)

        if response.status_code == 403:
            print(f"❌ 403 Forbidden - URL may be expired or restricted")
            print(f"   🔗 URL: {url[:100]}...")
            return False

        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    # Progress indicator every 10MB
                    if downloaded % (10 * 1024 * 1024) == 0:
                        if total_size > 0:
                            progress = (downloaded / total_size) * 100
                            print(f"   📈 Progress: {progress:.1f}% ({downloaded/1024/1024:.1f}MB)")
                        else:
                            print(f"   📈 Downloaded: {downloaded/1024/1024:.1f}MB")
        
        print(f"✅ Download complete: {filepath.name}")
        return True
        
    except Exception as e:
        print(f"❌ Download failed: {e}")
        if filepath.exists():
            filepath.unlink()  # Remove partial file
        return False

def read_jsonl_file(filepath):
    """Read JSONL file and return list of movie entries"""
    movies = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line:
                    try:
                        movie = json.loads(line)
                        movies.append(movie)
                    except json.JSONDecodeError as e:
                        print(f"⚠️ Invalid JSON on line {line_num}: {e}")
                        continue
        
        print(f"📋 Loaded {len(movies)} movies from {filepath}")
        return movies
        
    except FileNotFoundError:
        print(f"❌ File not found: {filepath}")
        return []
    except Exception as e:
        print(f"❌ Error reading file: {e}")
        return []

def process_batch(movies, batch_size, start_index=0):
    """Process a batch of movies - continues until batch_size successful downloads"""
    movies_dir = Path("movies")
    movies_dir.mkdir(exist_ok=True)

    print(f"\n🎬 Processing batch {start_index//batch_size + 1}")
    print(f"🎯 Target: {batch_size} successful downloads")
    print("=" * 60)

    successful_downloads = 0
    current_index = start_index
    processed_count = 0

    while successful_downloads < batch_size and current_index < len(movies):
        movie = movies[current_index]
        processed_count += 1

        tmdb_id = movie.get('tmdb')
        download_url = movie.get('url')

        if not tmdb_id or not download_url:
            print(f"⚠️ Movie {processed_count}: Missing tmdb_id or url (Index: {current_index})")
            current_index += 1
            continue

        print(f"\n🎭 Movie {processed_count} (Index: {current_index}) - Success: {successful_downloads}/{batch_size}")
        print(f"   🆔 TMDB ID: {tmdb_id}")

        # Get movie name from TMDB
        movie_name = get_movie_name_from_tmdb(tmdb_id)
        print(f"   📝 Movie Name: {movie_name}")

        # Get file extension from URL
        parsed_url = urlparse(download_url)
        path = parsed_url.path
        extension = Path(path).suffix or '.mp4'

        # Create filename: MovieName_tmdbid.extension
        filename = f"{movie_name}_{tmdb_id}{extension}"
        filepath = movies_dir / filename

        # Check if file already exists
        if filepath.exists():
            print(f"⏭️ File already exists: {filename}")
            successful_downloads += 1
            current_index += 1
            continue

        # Download the movie
        if download_file(download_url, filepath, movie):
            successful_downloads += 1
            print(f"✅ Success! {successful_downloads}/{batch_size} completed")
        else:
            print(f"❌ Failed download, continuing to next movie...")

        current_index += 1

        # Small delay between downloads
        time.sleep(1)

    if successful_downloads == batch_size:
        print(f"\n🎉 Batch complete: {successful_downloads}/{batch_size} successful downloads")
        print(f"📊 Processed {processed_count} movies (indices {start_index}-{current_index-1})")
    else:
        print(f"\n⚠️ Batch incomplete: {successful_downloads}/{batch_size} successful downloads")
        print(f"📊 Reached end of movie list at index {current_index}")

    return successful_downloads, current_index

def main():
    parser = argparse.ArgumentParser(description='Download movies with TMDB names')
    parser.add_argument('--batch', type=int, default=4, help='Number of movies to download (default: 4)')
    parser.add_argument('--input', default='download/movies.jsonl', help='Input JSONL file (default: download/movies.jsonl)')
    parser.add_argument('--start', type=int, default=0, help='Start index (default: 0)')
    parser.add_argument('--list', action='store_true', help='List available movies without downloading')
    
    args = parser.parse_args()
    
    print("🎬 Movie Downloader with TMDB Integration")
    print("=" * 50)
    
    # Read movies from JSONL
    movies = read_jsonl_file(args.input)
    if not movies:
        print("❌ No movies to process")
        return 1
    
    # List mode
    if args.list:
        print(f"\n📋 Available movies ({len(movies)} total):")
        for i, movie in enumerate(movies[:20], 1):  # Show first 20
            tmdb_id = movie.get('tmdb', 'Unknown')
            size = movie.get('size', 'Unknown')
            quality = movie.get('quality', 'Unknown')
            print(f"   {i:3d}. TMDB:{tmdb_id} | {quality} | {size}")
        
        if len(movies) > 20:
            print(f"   ... and {len(movies) - 20} more movies")
        return 0
    
    # Download mode
    if args.start >= len(movies):
        print(f"❌ Start index {args.start} is beyond available movies ({len(movies)})")
        return 1
    
    # Process batch
    successful, next_index = process_batch(movies, args.batch, args.start)

    print(f"\n🎯 Summary:")
    print(f"   📥 Downloaded: {successful} movies")
    print(f"   📁 Location: movies/ folder")
    print(f"   🔄 Next batch: --start {next_index}")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
