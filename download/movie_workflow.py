#!/usr/bin/env python3
"""
🎬 Movie Workflow Orchestrator

Orchestrates the complete movie workflow:
1. Download 4 movies with TMDB names
2. Upload to Google Photos with gpmc
3. Auto-delete from host after upload
4. Repeat cycle

Usage:
    python download/movie_workflow.py --input download/movies.jsonl --cycles 10
"""

import subprocess
import sys
import time
import argparse
import json
from pathlib import Path
import os

def run_command(command, description):
    """Run a command and return success status"""
    print(f"\n🔄 {description}")
    print(f"💻 Command: {' '.join(command)}")
    print("-" * 60)
    
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=False,  # Show output in real-time
            text=True
        )
        print(f"✅ {description} completed successfully")
        return True
        
    except subprocess.CalledProcessError as e:
        print(f"❌ {description} failed with exit code {e.returncode}")
        return False
    except FileNotFoundError:
        print(f"❌ Command not found: {command[0]}")
        return False

def count_movies_in_jsonl(filepath):
    """Count total movies in JSONL file"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            count = sum(1 for line in f if line.strip())
        return count
    except:
        return 0

def check_movies_folder():
    """Check movies folder status"""
    movies_dir = Path("movies")
    if not movies_dir.exists():
        return 0, []
    
    files = list(movies_dir.glob("*.mp4"))
    return len(files), [f.name for f in files]

def download_batch(batch_size, start_index, input_file):
    """Download a batch of movies"""
    # Get the directory where this script is located
    script_dir = Path(__file__).parent
    download_script = script_dir / "download_movies.py"

    command = [
        sys.executable, str(download_script),
        "--batch", str(batch_size),
        "--start", str(start_index),
        "--input", input_file
    ]

    success = run_command(command, f"Downloading batch starting at index {start_index}")

    # Parse the output to get the next index (this is a simple approach)
    # In a real implementation, you might want to capture the output and parse it
    return success

def upload_to_google_photos():
    """Upload movies to Google Photos using gpmc"""
    movies_dir = Path("movies")

    if not movies_dir.exists() or not any(movies_dir.glob("*.mp4")):
        print("⚠️ No movies to upload")
        return True

    # Count files before upload
    file_count, files = check_movies_folder()
    print(f"📁 Found {file_count} movies to upload:")
    for file in files[:5]:  # Show first 5
        print(f"   📄 {file}")
    if len(files) > 5:
        print(f"   ... and {len(files) - 5} more files")

    # Get absolute path to movies directory
    movies_abs_path = movies_dir.resolve()

    # Get auth data from environment
    auth_data = os.environ.get('GP_AUTH_DATA')

    command = [
        "gpmc", str(movies_abs_path),
        "--recursive",
        "--progress",
        "--delete-from-host",
        "--threads", "4"
    ]

    # Add auth_data if available
    if auth_data:
        command.extend(["--auth_data", auth_data])
    
    success = run_command(command, f"Uploading {file_count} movies to Google Photos")
    
    if success:
        # Verify files were deleted
        remaining_count, _ = check_movies_folder()
        if remaining_count == 0:
            print("✅ All files successfully uploaded and deleted from host")
        else:
            print(f"⚠️ {remaining_count} files remain in movies folder")
    
    return success

def main():
    parser = argparse.ArgumentParser(description='Movie workflow orchestrator')
    parser.add_argument('--input', default='download/movies.jsonl', help='Input JSONL file (default: download/movies.jsonl)')
    parser.add_argument('--batch-size', type=int, default=4, help='Movies per batch (default: 4)')
    parser.add_argument('--cycles', type=int, default=10, help='Number of cycles to run (default: 10)')
    parser.add_argument('--start-index', type=int, default=0, help='Starting movie index (default: 0)')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without executing')
    
    args = parser.parse_args()
    
    print("🎬 Movie Workflow Orchestrator")
    print("=" * 50)
    print(f"📁 Input file: {args.input}")
    print(f"📊 Batch size: {args.batch_size} movies")
    print(f"🔄 Cycles: {args.cycles}")
    print(f"🎯 Starting at index: {args.start_index}")
    
    # Check if input file exists
    if not Path(args.input).exists():
        print(f"❌ Input file not found: {args.input}")
        return 1
    
    # Count total movies
    total_movies = count_movies_in_jsonl(args.input)
    print(f"📋 Total movies available: {total_movies}")
    
    if args.start_index >= total_movies:
        print(f"❌ Start index {args.start_index} is beyond available movies")
        return 1
    
    # Check for existing movies in folder
    existing_count, existing_files = check_movies_folder()
    if existing_count > 0:
        print(f"\n⚠️ Found {existing_count} existing movies in movies/ folder")
        response = input("Do you want to upload these first? (y/n): ").lower()
        if response == 'y':
            if not args.dry_run:
                if not upload_to_google_photos():
                    print("❌ Failed to upload existing movies")
                    return 1
            else:
                print("🔍 DRY RUN: Would upload existing movies")
    
    # Main workflow loop
    current_index = args.start_index
    successful_cycles = 0
    
    for cycle in range(1, args.cycles + 1):
        if current_index >= total_movies:
            print(f"\n🎉 All movies processed! Reached end of file.")
            break
        
        print(f"\n" + "=" * 60)
        print(f"🔄 CYCLE {cycle}/{args.cycles}")
        print(f"📊 Processing movies {current_index + 1}-{min(current_index + args.batch_size, total_movies)}")
        print("=" * 60)
        
        if args.dry_run:
            print(f"🔍 DRY RUN: Would download batch starting at {current_index}")
            print(f"🔍 DRY RUN: Would upload to Google Photos")
            print(f"🔍 DRY RUN: Files would be auto-deleted after upload")
        else:
            # Step 1: Download batch
            files_before = check_movies_folder()[0]

            if not download_batch(args.batch_size, current_index, args.input):
                print(f"❌ Cycle {cycle} failed at download step")
                break

            # Check how many files we actually have
            files_after = check_movies_folder()[0]
            files_downloaded = files_after - files_before

            if files_downloaded == 0:
                print(f"❌ No files downloaded in cycle {cycle}, stopping")
                break

            print(f"📊 Downloaded {files_downloaded} files in this cycle")

            # Step 2: Upload to Google Photos
            if not upload_to_google_photos():
                print(f"❌ Cycle {cycle} failed at upload step")
                break

        successful_cycles += 1
        # Increment index by a reasonable amount (we'll adjust based on actual downloads)
        current_index += args.batch_size * 2  # Skip ahead more to account for failed downloads
        
        print(f"✅ Cycle {cycle} completed successfully")
        
        # Brief pause between cycles
        if cycle < args.cycles and current_index < total_movies:
            print(f"⏸️ Pausing 5 seconds before next cycle...")
            time.sleep(5)
    
    # Final summary
    print(f"\n" + "=" * 60)
    print(f"🎯 WORKFLOW SUMMARY")
    print("=" * 60)
    print(f"✅ Successful cycles: {successful_cycles}/{args.cycles}")
    print(f"📊 Movies processed: {min(current_index, total_movies) - args.start_index}")
    print(f"📋 Next start index: {current_index}")
    
    if current_index < total_movies:
        remaining = total_movies - current_index
        print(f"📝 Movies remaining: {remaining}")
        print(f"🔄 To continue: python movie_workflow.py --start-index {current_index}")
    else:
        print(f"🎉 All movies have been processed!")
    
    return 0 if successful_cycles > 0 else 1

if __name__ == "__main__":
    sys.exit(main())
