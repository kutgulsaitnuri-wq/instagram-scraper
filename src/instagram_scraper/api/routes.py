"""FastAPI routes."""

from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List
import csv
import io
import json
import hashlib
import re
from urllib.parse import quote, urlparse
import os
import yt_dlp
from datetime import datetime
import shutil
from httpx import Timeout
import glob
from ..scrapers.profile import ProfileScraper
from ..scrapers.posts import PostsScraper
from ..scrapers.media import MediaScraper
from ..config.settings import settings
from ..models.profile import ProfileModel
from ..models.post import PostModel
from ..models.media import MediaModel

router = APIRouter()

@router.get("/profile/{username}", response_model=ProfileModel)
async def scrape_profile(username: str):
    async with ProfileScraper() as scraper:
        try:
            profile = await scraper.scrape(username)
            return profile
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@router.get("/posts/{username}", response_model=List[PostModel])
async def scrape_posts(
    username: str,
    max_posts: int = Query(default=50, le=200)
):
    async with ProfileScraper() as profile_scraper:
        profile = await profile_scraper.scrape(username)
        user_id = profile.dict().get('id', '1067259270')
    
    async with PostsScraper(user_id) as scraper:
        posts = await scraper.scrape(max_posts)
        return posts

@router.get("/media/{shortcode}", response_model=MediaModel)
async def scrape_media(shortcode: str):
    async with MediaScraper() as scraper:
        try:
            media = await scraper.scrape(shortcode)
            return media
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@router.get("/preview/{shortcode}")
async def preview_media(shortcode: str):
    async with MediaScraper() as scraper:
        scraper.client.timeout = Timeout(10.0)
        media = await scraper.scrape(shortcode)
    
    if not media.media_urls:
        raise HTTPException(status_code=404, detail="No media for preview")
    
    thumbnails = []
    thumb_base = media.thumbnail_url or media.media_urls[0]
    for i, url in enumerate(media.media_urls):
        ext = url.split('.')[-1].lower()
        is_video = media.is_video or 'mp4' in ext or 'mov' in ext
        thumbnails.append({
            "index": i+1,
            "url": thumb_base,
            "type": "video" if is_video else "image",
            "download_url": f"/downloads/{shortcode}_{i+1:03d}.{ext if is_video else 'jpg'}"
        })
    
    return {"shortcode": shortcode, "thumbnails": thumbnails, "is_multi": len(thumbnails) > 1}

def cleanup_old_downloads(max_dirs: int = 20, max_age_days: int = 7):
    """
    Clean up old download directories to prevent disk space issues.
    Keeps the most recent N directories and deletes anything older than max_age_days.
    
    Args:
        max_dirs: Maximum number of directories to keep (default: 20)
        max_age_days: Maximum age in days before deletion (default: 7)
    """
    downloads_root = "data/outputs/downloads"
    if not os.path.exists(downloads_root):
        return
    
    try:
        dirs_with_time = []
        for dir_name in os.listdir(downloads_root):
            dir_path = os.path.join(downloads_root, dir_name)
            if os.path.isdir(dir_path):
                try:
                    mtime = os.path.getmtime(dir_path)
                    dirs_with_time.append((dir_name, mtime, dir_path))
                except OSError:
                    continue
        
        if not dirs_with_time:
            return
        
        dirs_with_time.sort(key=lambda x: x[1], reverse=True)
        
        current_time = datetime.now().timestamp()
        max_age_seconds = max_age_days * 24 * 60 * 60
        
        deleted_count = 0
        for i, (dir_name, mtime, dir_path) in enumerate(dirs_with_time):
            age_seconds = current_time - mtime
            should_delete = False
            
            if i >= max_dirs:
                should_delete = True
            
            if age_seconds > max_age_seconds:
                should_delete = True
            
            if should_delete:
                try:
                    shutil.rmtree(dir_path)
                    deleted_count += 1
                except OSError as e:
                    print(f"Warning: Could not delete {dir_path}: {e}")
        
        if deleted_count > 0:
            print(f"Cleanup: Deleted {deleted_count} old download directory(ies)")
    
    except Exception as e:
        print(f"Warning: Error during cleanup: {e}")

def find_existing_files(file_key: str, expected_count: int = 0) -> Optional[List[dict]]:
    """
    Search for existing downloaded files for a given file_key (shortcode or hash).
    If expected_count is 0, returns any files matching the key.
    Returns list of file info dicts if files found, None otherwise.
    """
    downloads_root = "data/outputs/downloads"
    if not os.path.exists(downloads_root):
        return None

    found_files = []

    for timestamp_dir in os.listdir(downloads_root):
        timestamp_path = os.path.join(downloads_root, timestamp_dir)
        if not os.path.isdir(timestamp_path):
            continue

        if expected_count > 0:
            all_found = True
            temp_files = []
            for i in range(1, expected_count + 1):
                pattern = os.path.join(timestamp_path, f"{file_key}_{i:03d}.*")
                matched = glob.glob(pattern)
                if matched:
                    full_path = matched[0]
                    filename = os.path.basename(full_path)
                    ext = os.path.splitext(filename)[1].lstrip('.').lower()
                    temp_files.append({
                        "name": filename,
                        "path": f"/downloads/{timestamp_dir}/{filename}",
                        "type": ext
                    })
                else:
                    all_found = False
                    break
            if all_found and len(temp_files) == expected_count:
                return temp_files
        else:
            pattern = os.path.join(timestamp_path, f"{file_key}_*.*")
            matched = sorted(glob.glob(pattern))
            if matched:
                for full_path in matched:
                    filename = os.path.basename(full_path)
                    ext = os.path.splitext(filename)[1].lstrip('.').lower()
                    found_files.append({
                        "name": filename,
                        "path": f"/downloads/{timestamp_dir}/{filename}",
                        "type": ext
                    })
                return found_files

    return None


# YouTube URL patterns to block
YOUTUBE_PATTERNS = re.compile(
    r'(youtube\.com|youtu\.be|youtube\.com/shorts|m\.youtube\.com|yt\.be)',
    re.IGNORECASE
)

# Instagram URL patterns
INSTAGRAM_PATTERNS = re.compile(
    r'(instagram\.com|instagr\.am|ig\.me)',
    re.IGNORECASE
)

def _url_hash(url: str) -> str:
    """Generate a short hash from URL for use as cache/file key."""
    return hashlib.md5(url.strip().encode()).hexdigest()[:12]

def _is_youtube(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().replace('www.', '')
    return bool(YOUTUBE_PATTERNS.search(host))

def _is_instagram(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().replace('www.', '')
    return bool(INSTAGRAM_PATTERNS.search(host))

@router.get("/download")
async def download_media(url: Optional[str] = Query(None, description="Video URL (Instagram, TikTok, Twitter, etc.)"), shortcode: Optional[str] = Query(None, description="Direct shortcode (Instagram only)")):
    if not url and not shortcode:
        raise HTTPException(status_code=400, detail="Provide 'url' or 'shortcode' param")

    # YouTube bloklama
    if url and _is_youtube(url):
        raise HTTPException(status_code=400, detail="YouTube is not supported")

    # Instagram akışı
    if shortcode or (url and _is_instagram(url)):
        return await _download_instagram(url, shortcode)

    # Diğer platformlar (TikTok, Twitter, Facebook, Pinterest, Reddit, Threads)
    if url:
        return await _download_generic(url)

    raise HTTPException(status_code=400, detail="Invalid request")


async def _download_instagram(url: Optional[str], shortcode: Optional[str]):
    """Instagram-specific download flow using MediaScraper."""
    if url and not shortcode:
        parsed = urlparse(url)
        path = parsed.path
        if '/p/' in path:
            shortcode = path.split('/p/')[1].split('/')[0]
        elif '/reel/' in path:
            shortcode = path.split('/reel/')[1].split('/')[0]
        else:
            raise HTTPException(status_code=400, detail="Invalid IG URL; must contain /p/ or /reel/ for shortcode")

    if not shortcode:
        raise HTTPException(status_code=400, detail="No shortcode extracted")

    try:
        async with MediaScraper() as scraper:
            media = await scraper.scrape(shortcode)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scraping failed: {str(e)}")

    if not media.media_urls:
        raise HTTPException(status_code=404, detail="No media URLs found for shortcode")

    existing_files = find_existing_files(shortcode, len(media.media_urls))
    if existing_files:
        return {
            "shortcode": shortcode,
            "files": existing_files,
            "dir": None,
            "preview_thumbnail": media.thumbnail_url,
            "caption": media.caption,
            "cached": True
        }

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_dir = f"data/outputs/downloads/{timestamp}"
    os.makedirs(output_dir, exist_ok=True)

    files = []
    ydl_opts = {
        'outtmpl': f'{output_dir}/{shortcode}_%(autonumber)03d.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'format': 'best',
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for i, media_url in enumerate(media.media_urls):
            try:
                ydl.download([media_url])
                info = ydl.extract_info(media_url, download=False)
                pattern = os.path.join(output_dir, f"{shortcode}_{i+1:03d}.*")
                matched = glob.glob(pattern)
                if matched:
                    full_path = matched[0]
                    filename = os.path.basename(full_path)
                    ext = os.path.splitext(filename)[1].lstrip('.').lower()
                else:
                    ext = info.get('ext') or 'file'
                    if ext in ('jpeg',):
                        ext = 'jpg'
                    filename = f"{shortcode}_{i+1:03d}.{ext}"
                    full_path = os.path.join(output_dir, filename)

                files.append({
                    "name": filename,
                    "path": f"/downloads/{timestamp}/{filename}",
                    "type": ext
                })
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Download failed for URL {i+1}: {str(e)}")

    cleanup_old_downloads()

    return {
        "shortcode": shortcode,
        "files": files,
        "dir": output_dir,
        "preview_thumbnail": media.thumbnail_url,
        "caption": media.caption,
        "cached": False
    }


async def _download_generic(url: str):
    """Generic download flow for non-Instagram platforms using yt-dlp directly."""
    file_key = _url_hash(url)

    # Cache kontrolü — caption cache'de yok, boş dön
    existing_files = find_existing_files(file_key)
    if existing_files:
        return {
            "shortcode": file_key,
            "files": existing_files,
            "dir": None,
            "preview_thumbnail": None,
            "caption": "",
            "cached": True
        }

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_dir = f"data/outputs/downloads/{timestamp}"
    os.makedirs(output_dir, exist_ok=True)

    ydl_opts = {
        'outtmpl': f'{output_dir}/{file_key}_001.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'format': 'best[ext=mp4]/best',
        'noplaylist': True,
        'merge_output_format': 'mp4',
    }

    files = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            # yt-dlp indirilen dosyaları bul
            pattern = os.path.join(output_dir, f"{file_key}_001.*")
            matched = sorted(glob.glob(pattern))

            if not matched:
                raise HTTPException(status_code=404, detail="No media could be downloaded")

            # Sadece final dosyayı al (ara dosyaları atla)
            final_file = matched[0]
            filename = os.path.basename(final_file)
            ext = os.path.splitext(filename)[1].lstrip('.').lower()
            files.append({
                "name": filename,
                "path": f"/downloads/{timestamp}/{filename}",
                "type": ext
            })

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

    cleanup_old_downloads()

    thumbnail = None
    caption = ""
    if info and isinstance(info, dict):
        thumbnail = info.get('thumbnail')
        caption = info.get('description', '') or ''

    return {
        "shortcode": file_key,
        "files": files,
        "dir": output_dir,
        "preview_thumbnail": thumbnail,
        "caption": caption,
        "cached": False
    }

@router.get("/posts/{username}/export")
async def export_posts_csv(username: str, max_posts: int = 50):
    posts = await scrape_posts(username, max_posts)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=PostModel.model_fields.keys())
    writer.writeheader()
    writer.writerows([post.dict() for post in posts])
    return {"csv": output.getvalue(), "download": f"data:text/csv;charset=utf-8,{output.getvalue()}"}
