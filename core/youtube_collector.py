import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, List, Optional
import requests
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from googleapiclient.discovery import build

from core.logger import AgentLogger


class YouTubeCollector:
    DEFAULT_KEYWORD = "연애"
    ALLOWED_KEYWORDS = ["연애"]

    def __init__(self, logger: AgentLogger):
        self.logger = logger

    @staticmethod
    def extract_video_id(url_or_id: str) -> Optional[str]:
        text = url_or_id.strip()
        if re.match(r"^[A-Za-z0-9_-]{11}$", text):
            return text

        parsed = urlparse(text)
        if parsed.hostname in ("youtu.be", "www.youtu.be"):
            vid = parsed.path.lstrip("/").split("/")[0]
            return vid[:11] if len(vid) >= 11 else None

        if parsed.hostname and "youtube.com" in parsed.hostname:
            if parsed.path == "/watch":
                qs = parse_qs(parsed.query)
                v_list = qs.get("v", [])
                if v_list:
                    return v_list[0][:11]
            for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
                if parsed.path.startswith(prefix):
                    vid = parsed.path[len(prefix) :].split("/")[0].split("?")[0]
                    return vid[:11] if len(vid) >= 11 else None

        match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", text)
        return match.group(1) if match else None

    @staticmethod
    def format_upload_date(raw_date: Optional[str], timestamp: Optional[int] = None) -> str:
        """YYYYMMDD 또는 ISO 날짜 문자열, 혹은 unix timestamp를 'YYYY.MM.DD' 형식으로 변환합니다."""
        if raw_date:
            clean = str(raw_date).strip()
            if re.match(r"^\d{8}$", clean):
                return f"{clean[:4]}.{clean[4:6]}.{clean[6:8]}"
            if len(clean) >= 10 and clean[4] == "-" and clean[7] == "-":
                return clean[:10].replace("-", ".")
        if timestamp:
            try:
                return datetime.fromtimestamp(int(timestamp)).strftime("%Y.%m.%d")
            except Exception:
                pass
        return "날짜 정보 없음"

    @staticmethod
    def detect_input_mode(raw_input: str) -> str:
        text = raw_input.strip()
        if not text:
            return "keyword"
        if "youtube.com/" in text or "youtu.be/" in text:
            if any(token in text for token in ("/@", "/channel/", "/c/", "/user/")):
                return "channel"
            if YouTubeCollector.extract_video_id(text):
                return "url"
        return "keyword"

    @staticmethod
    def parse_view_count_text(raw_text: str) -> int:
        """'조회수 2,882회', '조회수 3.2천회', '2,882 views' 등의 문자열을 정수 조회수로 변환합니다."""
        if not raw_text:
            return 0
        cleaned = str(raw_text).replace(",", "").strip()
        if "없음" in cleaned or "no views" in cleaned.lower():
            return 0
        m = re.search(r"(\d+(?:\.\d+)?)\s*(천|만|억|k|m)?", cleaned, re.IGNORECASE)
        if not m:
            return 0
        val = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit in ("천", "k"):
            val *= 1_000
        elif unit == "만":
            val *= 10_000
        elif unit == "m":
            val *= 1_000_000
        elif unit == "억":
            val *= 100_000_000
        return int(val)

    @staticmethod
    def parse_published_age_hours(raw_text: str) -> Optional[float]:
        """'5시간 전', '45분 전', '1일 전', '3 hours ago' 등의 상대 시간 문자열을 경과 시간(hours)으로 변환합니다."""
        if not raw_text:
            return None
        cleaned = str(raw_text).strip()
        m = re.search(
            r"(\d+)\s*(초|분|시간|일|주|개월|달|년|second|minute|hour|day|week|month|year)",
            cleaned,
            re.IGNORECASE,
        )
        if not m:
            return None
        num = float(m.group(1))
        unit = m.group(2).lower()
        if unit in ("초", "second"):
            return max(num / 3600.0, 0.1)
        if unit in ("분", "minute"):
            return max(num / 60.0, 0.1)
        if unit in ("시간", "hour"):
            return max(num, 0.25)
        if unit in ("일", "day"):
            return num * 24.0
        if unit in ("주", "week"):
            return num * 24.0 * 7.0
        if unit in ("개월", "달", "month"):
            return num * 24.0 * 30.0
        if unit in ("년", "year"):
            return num * 24.0 * 365.0
        return None

    def _search_yt_initial_data_page(self, query: str, sp: str = "") -> List[Dict[str, Any]]:
        """YouTube 검색 결과 페이지의 ytInitialData JSON에서 videoRenderer 목록을 고속 추출합니다."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        params = {"search_query": query, "hl": "ko", "gl": "KR"}
        if sp:
            params["sp"] = sp

        try:
            resp = requests.get(
                "https://www.youtube.com/results",
                params=params,
                headers=headers,
                timeout=8,
            )
            if not resp.ok:
                return []
            m = re.search(r"var ytInitialData = (\{.*?\});</script>", resp.text)
            if not m:
                return []
            data = json.loads(m.group(1))
        except Exception:
            return []

        renderers: List[Dict[str, Any]] = []

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                if "videoRenderer" in node and isinstance(node["videoRenderer"], dict):
                    renderers.append(node["videoRenderer"])
                else:
                    for v in node.values():
                        _walk(v)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(data)
        return renderers

    def fetch_breakout_videos(
        self,
        keyword: str = "연애",
        max_results: int = 8,
        min_views: int = 1000,
        max_views: int = 5000,
        min_views_per_hour: float = 150.0,
    ) -> List[Dict[str, Any]]:
        """
        특정 허용 키워드(현재 '연애')로 최근 업로드 영상을 병렬 탐색하여
        - 조회수: min_views(1,000) ~ max_views(5,000) 사이
        - 시간당 조회수: min_views_per_hour(150) 이상
        조건을 만족하는 '뜨기 직전 영상'을 시간당 조회수 높은 순으로 반환합니다.
        """
        base_kw = (keyword or self.DEFAULT_KEYWORD).strip()
        self.logger.info(
            f"[뜨기 직전 영상 탐색] 키워드='{base_kw}' | 조건: 조회수 {min_views:,}~{max_views:,}회 & 시간당 {min_views_per_hour:g}회 이상...",
            "COLLECTOR",
        )

        # 허용 키워드('연애') 기반 최신/금주 필터 병렬 검색 태스크 구성
        # EgQIAhAB: 오늘 업로드 + 동영상, EgQIAxAB: 이번 주 업로드 + 동영상, CAISBAgCEAE=: 오늘 업로드 + 최신순
        search_tasks = [
            (base_kw, "EgQIAhAB"),
            (base_kw, "EgQIAxAB"),
            (base_kw, "CAISBAgCEAE="),
            (f"{base_kw} 고민", "EgQIAhAB"),
            (f"{base_kw} 고민", "EgQIAxAB"),
            (f"{base_kw} 상담", "EgQIAhAB"),
            (f"{base_kw} 상담", "EgQIAxAB"),
            (f"{base_kw} 사연", "EgQIAhAB"),
            (f"{base_kw} 타로", "EgQIAhAB"),
            (f"{base_kw} 심리", "EgQIAhAB"),
        ]

        with ThreadPoolExecutor(max_workers=8) as executor:
            batch_results = list(
                executor.map(lambda pair: self._search_yt_initial_data_page(pair[0], pair[1]), search_tasks)
            )

        candidates_map: Dict[str, Dict[str, Any]] = {}
        total_scanned = 0

        for renderer_list in batch_results:
            for vr in renderer_list:
                vid = vr.get("videoId")
                if not vid or len(vid) != 11 or vid in candidates_map:
                    continue
                total_scanned += 1

                vc_obj = vr.get("viewCountText") or {}
                vc_text = vc_obj.get("simpleText") or "".join(
                    r.get("text", "") for r in (vc_obj.get("runs") or [])
                )
                pub_obj = vr.get("publishedTimeText") or {}
                pub_text = pub_obj.get("simpleText") or "".join(
                    r.get("text", "") for r in (pub_obj.get("runs") or [])
                )

                view_count = self.parse_view_count_text(vc_text)
                age_hours = self.parse_published_age_hours(pub_text)
                if age_hours is None or age_hours <= 0:
                    continue

                views_per_hour = round(view_count / max(age_hours, 0.25), 1)

                # 핵심 필터: 조회수 1,000 ~ 5,000 사이 & 시간당 150 이상
                if not (min_views <= view_count <= max_views):
                    continue
                if views_per_hour < min_views_per_hour:
                    continue

                title_runs = (vr.get("title") or {}).get("runs") or []
                title = "".join(r.get("text", "") for r in title_runs).strip() or f"YouTube ({vid})"

                owner_runs = (
                    (vr.get("ownerText") or {}).get("runs")
                    or (vr.get("longBylineText") or {}).get("runs")
                    or []
                )
                channel = "".join(r.get("text", "") for r in owner_runs).strip() or "채널 미상"

                thumbs = ((vr.get("thumbnail") or {}).get("thumbnails")) or []
                thumb_url = (
                    thumbs[-1].get("url")
                    if thumbs
                    else f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
                )

                candidates_map[vid] = {
                    "video_id": vid,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "title": title,
                    "channel": channel,
                    "upload_date": pub_text or "최근 업로드",
                    "thumbnail": thumb_url,
                    "view_count": view_count,
                    "views_per_hour": views_per_hour,
                    "age_hours": round(age_hours, 1),
                    "is_breakout": True,
                }

        # 시간당 조회수(views_per_hour) 높은 순 정렬
        sorted_breakouts = sorted(
            candidates_map.values(),
            key=lambda x: (x.get("views_per_hour", 0.0), x.get("view_count", 0)),
            reverse=True,
        )

        selected = sorted_breakouts[:max_results]
        self.logger.success(
            f"[뜨기 직전 영상] 후보 {total_scanned}개 스캔 -> 조건 충족 {len(sorted_breakouts)}개 발견 (상위 {len(selected)}개 표시)",
            "COLLECTOR",
        )
        return selected

    def fetch_video_list(
        self,
        query_or_url: str = "",
        max_results: int = 8,
        youtube_api_key: str = "",
        breakout_only: bool = True,
        min_views: int = 1000,
        max_views: int = 5000,
        min_views_per_hour: float = 150.0,
    ) -> List[Dict[str, Any]]:
        """
        1번째 칸 [영상 불러오기 / 뜨기 직전 영상 찾기] 메서드.
        - 키워드 모드일 때 breakout_only=True이면 조회수 1,000~5,000 & 시간당 150+ 뜨기 직전 영상을 탐색합니다.
        """
        raw_text = query_or_url.strip() or self.DEFAULT_KEYWORD
        mode = self.detect_input_mode(raw_text)
        results: List[Dict[str, Any]] = []

        if mode == "url":
            lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
            for line in lines:
                vid = self.extract_video_id(line)
                if not vid:
                    continue
                item = self._fetch_single_video_card_info(vid, youtube_api_key)
                results.append(item)
            return results

        if mode == "channel":
            channel_url = raw_text
            if not channel_url.endswith("/videos") and "/@" in channel_url:
                channel_url = channel_url.rstrip("/") + "/videos"
            self.logger.info(f"채널 최신 영상 목록 불러오기: {channel_url}", "COLLECTOR")
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": False,
                "playlistend": max_results,
                "skip_download": True,
                "ignoreerrors": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(channel_url, download=False)
                entries = (info or {}).get("entries") or []
                for entry in entries:
                    if not entry:
                        continue
                    vid = entry.get("id")
                    if not vid or len(vid) != 11:
                        continue
                    results.append(self._entry_to_card_dict(entry))
            self.logger.success(f"채널 영상 {len(results)}개 로드 완료", "COLLECTOR")
            return results[:max_results]

        # 키워드 모드 + 뜨기 직전 영상 필터 활성화 시
        if breakout_only:
            return self.fetch_breakout_videos(
                keyword=raw_text,
                max_results=max_results,
                min_views=min_views,
                max_views=max_views,
                min_views_per_hour=min_views_per_hour,
            )

        self.logger.info(f"키워드 기반 최신 영상 불러오기: '{raw_text}' (최대 {max_results}개)", "COLLECTOR")
        if youtube_api_key:
            try:
                youtube = build("youtube", "v3", developerKey=youtube_api_key)
                req = youtube.search().list(
                    q=raw_text,
                    part="snippet",
                    type="video",
                    maxResults=max_results,
                    order="date",
                )
                res = req.execute()
                for item in res.get("items", []):
                    vid = item["id"]["videoId"]
                    snip = item.get("snippet", {})
                    thumbs = snip.get("thumbnails", {})
                    thumb_url = (
                        thumbs.get("medium", {}).get("url")
                        or thumbs.get("high", {}).get("url")
                        or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
                    )
                    results.append({
                        "video_id": vid,
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "title": snip.get("title", f"YouTube ({vid})"),
                        "channel": snip.get("channelTitle", "채널 미상"),
                        "upload_date": self.format_upload_date(snip.get("publishedAt", "")),
                        "thumbnail": thumb_url,
                    })
                if results:
                    self.logger.success(f"YouTube Data API로 영상 {len(results)}개 불러오기 완료", "COLLECTOR")
                    return results
            except Exception as e:
                self.logger.warn(f"YouTube API 검색 실패, yt-dlp로 폴백합니다: {e}", "COLLECTOR")

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "skip_download": True,
            "ignoreerrors": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max_results}:{raw_text}", download=False)
            for entry in (info or {}).get("entries") or []:
                if not entry:
                    continue
                vid = entry.get("id")
                if not vid:
                    continue
                results.append(self._entry_to_card_dict(entry))

        self.logger.success(f"yt-dlp로 영상 {len(results)}개 불러오기 완료", "COLLECTOR")
        return results

    def _entry_to_card_dict(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        vid = entry.get("id", "")
        title = entry.get("title") or f"YouTube Video ({vid})"
        channel = entry.get("channel") or entry.get("uploader") or "채널 미상"
        ts = entry.get("timestamp") or entry.get("release_timestamp")
        upload_date = self.format_upload_date(entry.get("upload_date"), ts)
        thumbnail = entry.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
        view_count = int(entry.get("view_count") or 0)
        views_per_hour = 0.0
        if ts and view_count > 0:
            age_h = max((time.time() - float(ts)) / 3600.0, 0.25)
            if age_h > 0:
                views_per_hour = round(view_count / age_h, 1)
        is_breakout = bool(1000 <= view_count <= 5000 and views_per_hour >= 150.0)
        return {
            "video_id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": title,
            "channel": channel,
            "upload_date": upload_date,
            "thumbnail": thumbnail,
            "view_count": view_count,
            "views_per_hour": views_per_hour,
            "is_breakout": is_breakout,
        }

    def _fetch_single_video_card_info(self, video_id: str, youtube_api_key: str = "") -> Dict[str, Any]:
        url = f"https://www.youtube.com/watch?v={video_id}"
        if youtube_api_key:
            try:
                youtube = build("youtube", "v3", developerKey=youtube_api_key)
                res = youtube.videos().list(part="snippet", id=video_id).execute()
                items = res.get("items", [])
                if items:
                    snip = items[0].get("snippet", {})
                    return {
                        "video_id": video_id,
                        "url": url,
                        "title": snip.get("title", video_id),
                        "channel": snip.get("channelTitle", "채널 미상"),
                        "upload_date": self.format_upload_date(snip.get("publishedAt", "")),
                        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
                    }
            except Exception:
                pass

        ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return self._entry_to_card_dict(info or {"id": video_id})

    def collect_video_package(self, url_or_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        2번째 칸 활성화 시 실행되는 데이터 수집 로직:
        1. 선택한 영상의 자막(스크립트)과 인기 댓글 상위 5~10개를 추출합니다.
        2. 만약 자막이 없는 영상이라면, 영상의 '제목'과 '설명란(더보기 텍스트)'을 대신 추출합니다.
        """
        video_id = self.extract_video_id(url_or_id)
        if not video_id:
            raise ValueError(f"유효하지 않은 유튜브 URL 또는 ID입니다: {url_or_id}")

        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        self.logger.info(f"선택 영상 데이터 수집 시작 ({video_id})", "COLLECTOR")

        api_keys = config.get("api_keys", {})
        youtube_api_key = api_keys.get("youtube_data_api_key", "").strip()
        collector_cfg = config.get("collector_settings", {})
        pref_langs = collector_cfg.get("preferred_subtitle_langs", ["ko", "en"])
        # 인기 댓글 상위 5~10개 기준 (기본 10개)
        raw_max_comments = int(collector_cfg.get("max_top_comments", 10))
        max_comments = max(5, min(raw_max_comments, 10))
        max_transcript_chars = int(collector_cfg.get("max_transcript_chars", 15000))

        need_ytdlp_comments = not bool(youtube_api_key)
        metadata, ytdlp_subs, ytdlp_comments = self._extract_via_ytdlp(
            canonical_url, max_comments=max_comments, fetch_comments=need_ytdlp_comments
        )

        api_comments: List[Dict[str, Any]] = []
        if youtube_api_key:
            try:
                api_meta, api_comments = self._extract_via_youtube_api(
                    video_id, youtube_api_key, max_comments=max_comments
                )
                if api_meta.get("title"):
                    metadata.update(api_meta)
                self.logger.success(
                    f"YouTube Data API로 인기 댓글 상위 {len(api_comments)}개 추출 완료", "COLLECTOR"
                )
            except Exception as e:
                self.logger.warn(
                    f"YouTube Data API 호출 실패 ({e}) -> yt-dlp 인기 댓글 스크래핑으로 전환합니다.", "COLLECTOR"
                )
                if not ytdlp_comments:
                    _, _, ytdlp_comments = self._extract_via_ytdlp(
                        canonical_url, max_comments=max_comments, fetch_comments=True
                    )

        final_comments = (api_comments if api_comments else ytdlp_comments)[:max_comments]

        # 1순위: 자막(스크립트) 추출
        transcript_text, transcript_source = self._extract_transcript(
            video_id=video_id,
            preferred_langs=pref_langs,
            ytdlp_sub_info=ytdlp_subs,
            max_chars=max_transcript_chars,
        )

        has_transcript = bool(transcript_text.strip())
        if has_transcript:
            content_mode = "transcript"
            self.logger.success(
                f"자막({transcript_source}, {len(transcript_text)}자) 및 인기 댓글 {len(final_comments)}개 추출 완료",
                "COLLECTOR",
            )
        else:
            # 2순위 폴백: 자막이 없는 영상이면 '제목'과 '설명란(더보기 텍스트)'을 대신 사용
            content_mode = "title_and_description"
            transcript_source = "자막 없음 → 제목 + 설명란(더보기) 대체 추출"
            self.logger.warn(
                f"자막이 없는 영상입니다. 영상 제목과 설명란({len(metadata.get('description', ''))}자) 및 인기 댓글 {len(final_comments)}개를 대신 추출했습니다.",
                "COLLECTOR",
            )

        return {
            "video_id": video_id,
            "url": canonical_url,
            "title": metadata.get("title", ""),
            "channel": metadata.get("channel", ""),
            "view_count": metadata.get("view_count", 0),
            "like_count": metadata.get("like_count", 0),
            "upload_date": metadata.get("upload_date", ""),
            "thumbnail": metadata.get("thumbnail", f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"),
            "description": metadata.get("description", ""),
            "transcript": transcript_text,
            "has_transcript": has_transcript,
            "content_mode": content_mode,
            "transcript_source": transcript_source,
            "top_comments": final_comments,
        }

    def _extract_via_ytdlp(
        self, url: str, max_comments: int = 10, fetch_comments: bool = False
    ):
        ydl_opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
        }
        if fetch_comments:
            ydl_opts["getcomments"] = True
            ydl_opts["extractor_args"] = {
                "youtube": {
                    "max_comments": [str(max_comments), str(max_comments), "0", "0"],
                    "comment_sort": ["top"],
                }
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        metadata = {
            "title": info.get("title") or "",
            "channel": info.get("channel") or info.get("uploader") or "",
            "view_count": info.get("view_count") or 0,
            "like_count": info.get("like_count") or 0,
            "upload_date": self.format_upload_date(info.get("upload_date"), info.get("timestamp")),
            "thumbnail": info.get("thumbnail") or "",
            "description": info.get("description") or "",
        }

        subs_info = {
            "subtitles": info.get("subtitles") or {},
            "automatic_captions": info.get("automatic_captions") or {},
        }

        comments_list: List[Dict[str, Any]] = []
        raw_comments = info.get("comments") or []
        for c in raw_comments[:max_comments]:
            text = (c.get("text") or "").strip()
            if not text:
                continue
            comments_list.append({
                "author": c.get("author") or "익명",
                "text": text,
                "like_count": c.get("like_count") or 0,
                "is_favorited": bool(c.get("is_favorited", False)),
            })

        return metadata, subs_info, comments_list

    def _extract_via_youtube_api(
        self, video_id: str, api_key: str, max_comments: int = 10
    ):
        youtube = build("youtube", "v3", developerKey=api_key)

        v_res = youtube.videos().list(part="snippet,statistics", id=video_id).execute()
        items = v_res.get("items", [])
        meta: Dict[str, Any] = {}
        if items:
            snip = items[0].get("snippet", {})
            stats = items[0].get("statistics", {})
            thumbs = snip.get("thumbnails", {})
            thumb_url = (
                thumbs.get("maxres", {}).get("url")
                or thumbs.get("high", {}).get("url")
                or thumbs.get("medium", {}).get("url")
                or ""
            )
            meta = {
                "title": snip.get("title", ""),
                "channel": snip.get("channelTitle", ""),
                "description": snip.get("description", ""),
                "upload_date": self.format_upload_date(snip.get("publishedAt", "")),
                "view_count": int(stats.get("viewCount", 0)),
                "like_count": int(stats.get("likeCount", 0)),
                "thumbnail": thumb_url,
            }

        comments: List[Dict[str, Any]] = []
        try:
            c_res = (
                youtube.commentThreads()
                .list(
                    part="snippet",
                    videoId=video_id,
                    order="relevance",
                    maxResults=min(max_comments, 20),
                    textFormat="plainText",
                )
                .execute()
            )
            for item in c_res.get("items", [])[:max_comments]:
                top_c = item["snippet"]["topLevelComment"]["snippet"]
                comments.append({
                    "author": top_c.get("authorDisplayName", "익명"),
                    "text": (top_c.get("textDisplay") or "").strip(),
                    "like_count": int(top_c.get("likeCount", 0)),
                    "is_favorited": False,
                })
        except Exception as e:
            self.logger.warn(f"댓글 API 조회 중 예외 발생 (댓글 사용 중지 등): {e}", "COLLECTOR")

        return meta, comments

    def _extract_transcript(
        self,
        video_id: str,
        preferred_langs: List[str],
        ytdlp_sub_info: Dict[str, Any],
        max_chars: int = 15000,
    ):
        try:
            ytt_api = YouTubeTranscriptApi()
            transcript_list = ytt_api.list(video_id)
            transcript_obj = None

            try:
                transcript_obj = transcript_list.find_manually_created_transcript(preferred_langs)
                source_label = f"수동 자막 ({transcript_obj.language_code})"
            except Exception:
                try:
                    transcript_obj = transcript_list.find_generated_transcript(preferred_langs)
                    source_label = f"자동 생성 자막 ({transcript_obj.language_code})"
                except Exception:
                    for tr in transcript_list:
                        transcript_obj = tr
                        source_label = f"대체 자막 ({tr.language_code})"
                        break

            if transcript_obj is not None:
                fetched = transcript_obj.fetch()
                lines = []
                for snippet in fetched:
                    txt = getattr(snippet, "text", None)
                    if txt is None and isinstance(snippet, dict):
                        txt = snippet.get("text", "")
                    if txt:
                        clean = re.sub(r"\s+", " ", str(txt)).strip()
                        if clean:
                            lines.append(clean)
                joined = " ".join(lines)
                if joined:
                    return joined[:max_chars], source_label
        except Exception as e:
            self.logger.info(f"youtube-transcript-api 폴백 -> yt-dlp 자막 파서 시도 ({e})", "COLLECTOR")

        for sub_type_key, label_prefix in [
            ("subtitles", "수동 자막"),
            ("automatic_captions", "자동 자막"),
        ]:
            track_dict = ytdlp_sub_info.get(sub_type_key) or {}
            lang_candidates = list(preferred_langs) + [
                k for k in track_dict.keys() if k not in preferred_langs
            ]
            for lang in lang_candidates:
                formats = track_dict.get(lang)
                if not formats:
                    continue
                json3_url = next((f.get("url") for f in formats if f.get("ext") == "json3"), None)
                if json3_url:
                    try:
                        resp = requests.get(json3_url, timeout=10)
                        if resp.ok:
                            data = resp.json()
                            events = data.get("events", [])
                            words = []
                            for ev in events:
                                for seg in ev.get("segs", []) or []:
                                    utf8_txt = seg.get("utf8", "").strip()
                                    if utf8_txt and utf8_txt != "\n":
                                        words.append(utf8_txt)
                            text_out = re.sub(r"\s+", " ", " ".join(words)).strip()
                            if text_out:
                                return text_out[:max_chars], f"{label_prefix} ({lang}/yt-dlp)"
                    except Exception:
                        pass

        return "", "자막 없음"
