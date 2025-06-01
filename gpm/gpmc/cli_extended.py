#!/usr/bin/env python3
"""
Extended CLI for Google Photos Mobile Client with list and download capabilities.
"""
import argparse
import sys
from pathlib import Path
from pprint import pp

from .client import Client
from .api import DEFAULT_TIMEOUT


def cmd_upload(args, client):
    """Handle upload command"""
    output = client.upload(
        target=args.path,
        album_name=args.album,
        use_quota=args.use_quota,
        saver=args.saver,
        show_progress=args.progress,
        recursive=args.recursive,
        threads=args.threads,
        force_upload=args.force_upload,
        delete_from_host=args.delete_from_host,
        filter_exp=args.filter,
        filter_exclude=args.exclude,
        filter_regex=args.regex,
        filter_ignore_case=args.ignore_case,
        filter_path=args.match_path,
    )
    pp(output)


def cmd_list(args, client):
    """Handle list command"""
    try:
        # Choose between direct (from Google) or cache-based listing
        if args.direct:
            # List directly from Google Photos (like rclone)
            media_items = client.list_remote_media_direct(
                media_type=args.type,
                limit=args.limit,
                show_progress=args.progress,
                force_cache_update=True,
            )
            # Apply filters manually for direct listing
            if args.filter:
                import re
                filtered_items = []
                for item in media_items:
                    filename = item.get('file_name', '')
                    if args.regex:
                        flags = re.IGNORECASE if args.ignore_case else 0
                        matches = bool(re.search(args.filter, filename, flags))
                    else:
                        if args.ignore_case:
                            matches = args.filter.lower() in filename.lower()
                        else:
                            matches = args.filter in filename

                    if (matches and not args.exclude) or (not matches and args.exclude):
                        filtered_items.append(item)
                media_items = filtered_items
        else:
            # Update cache if requested
            if args.update_cache:
                print("🔄 Updating cache first...")
                client.update_cache(show_progress=args.progress)

            # List media files from cache
            media_items = client.list_remote_media(
                media_type=args.type,
                include_trashed=args.include_trashed,
                limit=args.limit,
                filter_exp=args.filter,
                filter_exclude=args.exclude,
                filter_regex=args.regex,
                filter_ignore_case=args.ignore_case,
                show_progress=args.progress,
            )
        
        if args.json:
            # Output as JSON
            import json
            print(json.dumps(media_items, indent=2, ensure_ascii=False))
        else:
            # Pretty print summary
            print(f"\n📊 FOUND {len(media_items)} MEDIA FILES:")
            print("=" * 60)
            
            for i, item in enumerate(media_items[:args.show_count], 1):
                print(f"\n🎬 {i}. {item['file_name']}")
                print(f"   🔑 Media Key: {item['media_key'][:20]}...")
                print(f"   📅 Created: {item['creation_date']}")
                if item['size_mb'] > 0:
                    print(f"   💾 Size: {item['size_mb']} MB")
                if item['width'] and item['height']:
                    print(f"   📐 Resolution: {item['width']}x{item['height']}")
                if item['duration']:
                    print(f"   ⏱️  Duration: {item['duration']} seconds")
            
            if len(media_items) > args.show_count:
                print(f"\n... and {len(media_items) - args.show_count} more files")
                print(f"💡 Use --show-count {len(media_items)} to see all files")
                print(f"💡 Use --json for machine-readable output")
                
    except ValueError as e:
        print(f"❌ Error: {e}")
        print("💡 Try running with --update-cache to refresh the local cache")
        sys.exit(1)


def cmd_download(args, client):
    """Handle download command"""
    try:
        if args.media_key:
            # Download single file by media key
            success = client.download_media(
                media_key=args.media_key,
                output_path=args.output,
                quality=args.quality,
                show_progress=args.progress,
                overwrite=args.overwrite,
            )
            if success:
                print(f"✅ Download completed: {args.output}")
            else:
                print(f"❌ Download failed")
                sys.exit(1)
        else:
            # Download multiple files based on list criteria
            media_items = client.list_remote_media(
                media_type=args.type,
                include_trashed=args.include_trashed,
                limit=args.limit,
                filter_exp=args.filter,
                filter_exclude=args.exclude,
                filter_regex=args.regex,
                filter_ignore_case=args.ignore_case,
                show_progress=False,
            )
            
            if not media_items:
                print("❌ No media files found matching criteria")
                sys.exit(1)
            
            print(f"📥 Found {len(media_items)} files to download")
            
            # Confirm download if many files
            if len(media_items) > 10 and not args.yes:
                response = input(f"Download {len(media_items)} files? [y/N]: ")
                if response.lower() != 'y':
                    print("❌ Download cancelled")
                    sys.exit(0)
            
            # Download files
            results = client.download_multiple_media(
                media_items=media_items,
                output_dir=args.output,
                quality=args.quality,
                threads=args.threads,
                show_progress=args.progress,
                overwrite=args.overwrite,
                preserve_structure=args.preserve_structure,
            )
            
            successful = sum(results.values())
            failed = len(results) - successful
            print(f"✅ Download summary: {successful} successful, {failed} failed")
            
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


def cmd_sync(args, client):
    """Handle sync command (like rclone sync)"""
    try:
        print(f"🌐 Starting Google Photos sync (like rclone)...")
        print(f"📁 Target directory: {args.output}")

        # Confirm sync if many files expected
        if not args.yes:
            response = input(f"Sync all {args.type} files from Google Photos to {args.output}? [y/N]: ")
            if response.lower() != 'y':
                print("❌ Sync cancelled")
                sys.exit(0)

        # Sync files directly from Google
        results = client.sync_from_google(
            output_dir=args.output,
            media_type=args.type,
            limit=args.limit,
            quality=args.quality,
            threads=args.threads,
            show_progress=args.progress,
            overwrite=args.overwrite,
            preserve_structure=args.preserve_structure,
        )

        successful = sum(results.values())
        failed = len(results) - successful
        print(f"🎉 Sync completed: {successful} successful, {failed} failed")

    except Exception as e:
        print(f"❌ Error during sync: {e}")
        sys.exit(1)


def cmd_update_cache(args, client):
    """Handle update-cache command"""
    try:
        client.update_cache(show_progress=args.progress)
        print("✅ Cache updated successfully")
    except Exception as e:
        print(f"❌ Error updating cache: {e}")
        sys.exit(1)


def main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(
        description="Extended Google Photos Mobile Client with list and download capabilities",
        formatter_class=argparse.RawTextHelpFormatter
    )
    
    # Global arguments
    parser.add_argument("--auth_data", type=str, help="Google auth data for authentication. If not provided, `GP_AUTH_DATA` env variable will be used.")
    parser.add_argument("--proxy", type=str, help="Proxy to use. Format: `protocol://username:password@host:port`")
    parser.add_argument("--progress", action="store_true", help="Display progress.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"Requests timeout, seconds. Defaults to {DEFAULT_TIMEOUT}.")
    parser.add_argument("--log-level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Set the logging level (default: INFO)")
    
    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Upload command (existing functionality)
    upload_parser = subparsers.add_parser("upload", help="Upload files to Google Photos")
    upload_parser.add_argument("path", type=str, help="Path to the file or directory to upload.")
    upload_parser.add_argument("--album", type=str, help="Album name. Use 'AUTO' for directory-based albums.")
    upload_parser.add_argument("--recursive", action="store_true", help="Scan the directory recursively.")
    upload_parser.add_argument("--threads", type=int, default=1, help="Number of threads to run uploads with. Defaults to 1.")
    upload_parser.add_argument("--force-upload", action="store_true", help="Upload files regardless of their presence in Google Photos.")
    upload_parser.add_argument("--delete-from-host", action="store_true", help="Delete uploaded files from source path.")
    upload_parser.add_argument("--use-quota", action="store_true", help="Uploaded files will count against your Google Photos storage quota.")
    upload_parser.add_argument("--saver", action="store_true", help="Upload files in storage saver quality.")
    
    # Filter options for upload
    upload_filter = upload_parser.add_argument_group("File Filter Options")
    upload_filter.add_argument("--filter", type=str, help="Filter expression.")
    upload_filter.add_argument("--exclude", action="store_true", help="Exclude files matching the filter.")
    upload_filter.add_argument("--regex", action="store_true", help="Use regex for filtering.")
    upload_filter.add_argument("--ignore-case", action="store_true", help="Perform case-insensitive matching.")
    upload_filter.add_argument("--match-path", action="store_true", help="Check for matches in the path, not just the filename.")
    
    # List command (new)
    list_parser = subparsers.add_parser("list", help="List remote media files")
    list_parser.add_argument("--type", choices=["all", "images", "videos"], default="all", help="Type of media to list (default: all)")
    list_parser.add_argument("--include-trashed", action="store_true", help="Include files in trash")
    list_parser.add_argument("--limit", type=int, help="Maximum number of files to list")
    list_parser.add_argument("--update-cache", action="store_true", help="Update cache before listing")
    list_parser.add_argument("--direct", action="store_true", help="List directly from Google Photos (like rclone) - works on any PC")
    list_parser.add_argument("--json", action="store_true", help="Output as JSON")
    list_parser.add_argument("--show-count", type=int, default=20, help="Number of files to show in summary (default: 20)")
    
    # Filter options for list
    list_filter = list_parser.add_argument_group("Filter Options")
    list_filter.add_argument("--filter", type=str, help="Filter expression for filenames.")
    list_filter.add_argument("--exclude", action="store_true", help="Exclude files matching the filter.")
    list_filter.add_argument("--regex", action="store_true", help="Use regex for filtering.")
    list_filter.add_argument("--ignore-case", action="store_true", help="Perform case-insensitive matching.")
    
    # Download command (new)
    download_parser = subparsers.add_parser("download", help="Download media files")
    download_parser.add_argument("output", type=str, help="Output path (file for single download, directory for multiple)")
    download_parser.add_argument("--media-key", type=str, help="Specific media key to download")
    download_parser.add_argument("--quality", choices=["original", "edited"], default="original", help="Download quality (default: original)")
    download_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    download_parser.add_argument("--threads", type=int, default=3, help="Number of concurrent download threads (default: 3)")
    download_parser.add_argument("--preserve-structure", action="store_true", default=True, help="Organize files by date (YYYY/MM/)")
    download_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation for bulk downloads")
    
    # Filter options for download (when not using --media-key)
    download_filter = download_parser.add_argument_group("Filter Options (for bulk download)")
    download_filter.add_argument("--type", choices=["all", "images", "videos"], default="all", help="Type of media to download")
    download_filter.add_argument("--include-trashed", action="store_true", help="Include files in trash")
    download_filter.add_argument("--limit", type=int, help="Maximum number of files to download")
    download_filter.add_argument("--filter", type=str, help="Filter expression for filenames.")
    download_filter.add_argument("--exclude", action="store_true", help="Exclude files matching the filter.")
    download_filter.add_argument("--regex", action="store_true", help="Use regex for filtering.")
    download_filter.add_argument("--ignore-case", action="store_true", help="Perform case-insensitive matching.")
    
    # Sync command (new - like rclone sync)
    sync_parser = subparsers.add_parser("sync", help="Sync/download all files from Google Photos (like rclone sync)")
    sync_parser.add_argument("output", type=str, help="Output directory to sync files to")
    sync_parser.add_argument("--type", choices=["all", "images", "videos"], default="all", help="Type of media to sync (default: all)")
    sync_parser.add_argument("--limit", type=int, help="Maximum number of files to sync")
    sync_parser.add_argument("--quality", choices=["original", "edited"], default="original", help="Download quality (default: original)")
    sync_parser.add_argument("--threads", type=int, default=3, help="Number of concurrent download threads (default: 3)")
    sync_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    sync_parser.add_argument("--preserve-structure", action="store_true", default=True, help="Organize files by date (YYYY/MM/)")
    sync_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")

    # Update cache command (new)
    cache_parser = subparsers.add_parser("update-cache", help="Update local cache from Google Photos")
    
    args = parser.parse_args()
    
    # Show help if no command specified
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    # Validate filter arguments
    if hasattr(args, 'filter') and (args.exclude or args.regex or args.ignore_case) and not args.filter:
        parser.error("--filter is required when using any of --exclude, --regex, or --ignore-case")
    
    # Create client
    try:
        client = Client(
            auth_data=args.auth_data,
            timeout=args.timeout,
            log_level=args.log_level,
            proxy=args.proxy,
        )
    except Exception as e:
        print(f"❌ Error creating client: {e}")
        sys.exit(1)
    
    # Execute command
    if args.command == "upload":
        cmd_upload(args, client)
    elif args.command == "list":
        cmd_list(args, client)
    elif args.command == "download":
        cmd_download(args, client)
    elif args.command == "sync":
        cmd_sync(args, client)
    elif args.command == "update-cache":
        cmd_update_cache(args, client)


if __name__ == "__main__":
    main()
