"""Media scraper/downloader."""

import asyncio
import random
from urllib.parse import quote
import json
from typing import Dict, Any, List
import yt_dlp
from ..models.media import MediaModel
from .base import BaseScraper, ua
from ..utils.headers import get_headers
import httpx

DOC_ID_POST = "8845758582119845"

class MediaScraper(BaseScraper):
    async def scrape(self, shortcode: str) -> MediaModel:
        """
        Try GraphQL first (with proxies if available) for private accounts.
        Fall back to yt-dlp if GraphQL fails (e.g., on cloud IPs).
        """
        try:
            return await self._scrape_with_graphql(shortcode)
        except (httpx.HTTPStatusError, ValueError, Exception) as graphql_error:
            error_msg = str(graphql_error)
            if "302" in error_msg or "Redirect" in error_msg or "login" in error_msg.lower():
                print(f"DEBUG: GraphQL blocked (likely cloud IP): {error_msg}, falling back to yt-dlp")
            else:
                print(f"DEBUG: GraphQL failed: {error_msg}, falling back to yt-dlp")

            try:
                return await self._scrape_with_ytdlp(shortcode)
            except Exception as ytdlp_error:
               
                raise ValueError(
                    f"Both methods failed. GraphQL: {error_msg}, yt-dlp: {str(ytdlp_error)}"
                )
    
    async def _scrape_with_graphql(self, shortcode: str) -> MediaModel:
        """
        Original GraphQL method - works with private accounts.
        Uses proxies automatically via BaseScraper if configured.
        """
        temp_username = "nasa"
        profile_resp = await self._make_request("GET", f"https://www.instagram.com/{temp_username}/")
        csrf_token = profile_resp.cookies.get("csrftoken", "dummy_csrf")
        
        self.client.headers.update(get_headers(ua, csrf_token))
        
        url = "https://www.instagram.com/graphql/query"
        variables = {"shortcode": shortcode}
        body = f"variables={quote(json.dumps(variables))}&doc_id={DOC_ID_POST}"
        
        resp = await self._make_request(
            "POST",
            url,
            data=body,
            headers={"content-type": "application/x-www-form-urlencoded"}
        )
        
        print(f"DEBUG: IG Media JSON = {resp.json()}")
        
        try:
            response_data = resp.json()
            if "errors" in response_data:
                raise ValueError(f"IG GraphQL Error: {response_data['errors']}")
            data = response_data["data"]["xdt_shortcode_media"]
        except (json.JSONDecodeError, KeyError) as e:
            raise ValueError(f"Parse Error: {e}. Full Response: {resp.text[:200]}")
        
        media_urls = []
        
        if data.get("video_url"):
            media_urls.append(data["video_url"])
        elif data.get("video_versions"):
            for version in data["video_versions"]:
                media_urls.append(version["url"])
        elif data.get("edge_sidecar_to_children"):
            for child in data["edge_sidecar_to_children"]["edges"]:
                child_node = child["node"]
                if child_node.get("video_url"):
                    media_urls.append(child_node["video_url"])
                elif child_node.get("video_versions"):
                    media_urls.append(child_node["video_versions"][0]["url"])
                else:
                    media_urls.append(child_node["display_url"])
        elif data.get("display_url"): 
            media_urls.append(data["display_url"])
        else:
            raise ValueError("No media URLs found in response")
        
        # Caption çıkar
        caption = ""
        try:
            edges = data.get("edge_media_to_caption", {}).get("edges", [])
            if edges:
                caption = edges[0].get("node", {}).get("text", "")
        except Exception:
            pass

        return MediaModel(
            shortcode=shortcode,
            media_urls=media_urls,
            thumbnail_url=data.get("thumbnail_src", ""),
            is_video=bool(data.get("video_versions") or data.get("video_url")),
            caption=caption,
        )
    
    async def _scrape_with_ytdlp(self, shortcode: str) -> MediaModel:
        """
        Fallback method using yt-dlp - works on cloud IPs for public content.
        Note: yt-dlp only works with public Instagram content.
        """
        post_url = f"https://www.instagram.com/p/{shortcode}/"
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
        }
        
        
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(None, ydl.extract_info, post_url, False)
            
            media_urls = []
            thumbnail_url = ""
            is_video = False
            
          
            if 'url' in info:
                media_urls.append(info['url'])
                is_video = info.get('vcodec') != 'none'
                thumbnail_url = info.get('thumbnail', '')
            
            
            elif 'entries' in info and info['entries']:
                for entry in info['entries']:
                    if 'url' in entry:
                        media_urls.append(entry['url'])
                        if entry.get('vcodec') != 'none':
                            is_video = True
                        if not thumbnail_url:
                            thumbnail_url = entry.get('thumbnail', '')
            

            elif 'formats' in info:
                best_format = None
                for fmt in info['formats']:
                    if fmt.get('vcodec') != 'none':
                        is_video = True
                    if not best_format or fmt.get('quality', 0) > best_format.get('quality', 0):
                        best_format = fmt
                if best_format and 'url' in best_format:
                    media_urls.append(best_format['url'])
                thumbnail_url = info.get('thumbnail', '')
            
            if not media_urls:
                raise ValueError("No media URLs found in yt-dlp response")

            caption = info.get('description', '') or ''

            return MediaModel(
                shortcode=shortcode,
                media_urls=media_urls,
                thumbnail_url=thumbnail_url,
                is_video=is_video,
                caption=caption,
            )