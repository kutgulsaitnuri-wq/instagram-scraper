"""Media data model."""

from typing import List
from pydantic import BaseModel

class MediaModel(BaseModel):
    shortcode: str
    media_urls: List[str]
    thumbnail_url: str = ""
    is_video: bool = False
    caption: str = ""