import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional


class ConfigManager:
    ACCOUNT_SUBDIR_MAP: Dict[str, str] = {
        "유튜브1": "youtube_1",
        "유튜브2": "youtube_2",
        "유튜브3": "youtube_3",
    }

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir.resolve()
        self.data_dir = self.base_dir / "data"
        self.screenshots_dir = self.base_dir / "screenshots"
        self.config_path = self.data_dir / "config.json"
        self.history_path = self.data_dir / "history.json"

        # 프로젝트 루트 내부 독립 전용 크롬 프로필 디렉터리 (./chrome_profiles/)
        self.profiles_root = self.base_dir / "chrome_profiles"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_isolated_profile_dirs()
        self._ensure_default_files()

    def _ensure_isolated_profile_dirs(self) -> None:
        """./chrome_profiles/ 및 계정별 독립 하위 폴더(youtube_1, youtube_2, youtube_3)를 생성합니다."""
        self.profiles_root.mkdir(parents=True, exist_ok=True)
        for sub_name in self.ACCOUNT_SUBDIR_MAP.values():
            (self.profiles_root / sub_name).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def is_forbidden_default_chrome_dir(path_str: str) -> bool:
        """윈도우 기본 크롬 User Data 경로(AppData/Local/Google/Chrome/User Data)인지 검사합니다."""
        if not path_str:
            return False
        norm = os.path.expandvars(path_str).replace("\\", "/").strip().lower()
        if "google/chrome/user data" in norm:
            return True
        return False

    def get_default_account_rel_dir(self, account_id: str) -> str:
        sub_name = self.ACCOUNT_SUBDIR_MAP.get(account_id, "youtube_1")
        return f"./chrome_profiles/{sub_name}"

    def sanitize_account_user_data_dir(self, account_id: str, raw_dir: Optional[str]) -> str:
        """
        계정별 user_data_dir 값을 검증하고, 윈도우 기본 User Data 경로이거나 레거시 경로이면
        즉시 ./chrome_profiles/youtube_X 형태의 격리 경로로 강제 보정합니다.
        """
        default_rel = self.get_default_account_rel_dir(account_id)
        if not raw_dir or not str(raw_dir).strip():
            return default_rel

        cleaned = str(raw_dir).strip()
        if self.is_forbidden_default_chrome_dir(cleaned):
            return default_rel

        norm_slash = cleaned.replace("\\", "/")
        # 이전 레거시 data/chrome_profiles 경로인 경우 프로젝트 루트 ./chrome_profiles 로 마이그레이션
        if "data/chrome_profiles" in norm_slash.lower():
            return default_rel

        # 절대 경로로 들어온 경우 프로젝트 내부 chrome_profiles 하위인지 확인
        candidate_path = Path(cleaned)
        if candidate_path.is_absolute():
            try:
                rel_to_base = candidate_path.resolve().relative_to(self.base_dir)
                rel_posix = "./" + rel_to_base.as_posix()
                if rel_posix.startswith("./chrome_profiles/"):
                    return rel_posix
            except ValueError:
                pass
            return default_rel

        # 상대 경로 정규화
        if not norm_slash.startswith("./"):
            norm_slash = "./" + norm_slash.lstrip("/")
        if not norm_slash.startswith("./chrome_profiles/"):
            return default_rel
        return norm_slash

    def resolve_account_abs_dir(self, account_id: str, raw_dir: Optional[str] = None) -> Path:
        """실제 브라우저 실행에 사용할 절대 경로를 반환하고 폴더 존재를 보장합니다."""
        rel_dir = self.sanitize_account_user_data_dir(account_id, raw_dir)
        rel_clean = rel_dir[2:] if rel_dir.startswith("./") else rel_dir
        abs_path = (self.base_dir / rel_clean).resolve()

        if self.is_forbidden_default_chrome_dir(str(abs_path)):
            sub_name = self.ACCOUNT_SUBDIR_MAP.get(account_id, "youtube_1")
            abs_path = (self.profiles_root / sub_name).resolve()

        abs_path.mkdir(parents=True, exist_ok=True)
        return abs_path

    def _default_config(self) -> Dict[str, Any]:
        default_chrome_exe = self._detect_chrome_executable()

        return {
            "api_keys": {
                "gemini_api_key": "",
                "youtube_data_api_key": ""
            },
            "ai_settings": {
                "gemini_model": "gemini-3.8-flash",
                "temperature": 0.85
            },
            "chrome_browser": {
                "channel": "chrome",
                "profiles_root_dir": "./chrome_profiles",
                "selected_account_id": "유튜브1",
                "accounts": {
                    "유튜브1": {
                        "account_id": "유튜브1",
                        "label": "유튜브1",
                        "profile_folder": "youtube_1",
                        "user_data_dir": "./chrome_profiles/youtube_1",
                        "description": "유튜브1 전용 격리 프로필 (./chrome_profiles/youtube_1)"
                    },
                    "유튜브2": {
                        "account_id": "유튜브2",
                        "label": "유튜브2",
                        "profile_folder": "youtube_2",
                        "user_data_dir": "./chrome_profiles/youtube_2",
                        "description": "유튜브2 전용 격리 프로필 (./chrome_profiles/youtube_2)"
                    },
                    "유튜브3": {
                        "account_id": "유튜브3",
                        "label": "유튜브3",
                        "profile_folder": "youtube_3",
                        "user_data_dir": "./chrome_profiles/youtube_3",
                        "description": "유튜브3 전용 격리 프로필 (./chrome_profiles/youtube_3)"
                    }
                },
                "headless": False,
                "slow_mo_ms": 40,
                "typing_delay_min_ms": 50,
                "typing_delay_max_ms": 150
            },
            "collector_settings": {
                "preferred_subtitle_langs": ["ko", "en"],
                "include_auto_generated_subs": True,
                "max_top_comments": 10,
                "max_transcript_chars": 15000
            },
            "workflow_policy": {
                "one_account_per_video": True,
                "daily_post_limit": 10,
                "allowed_keywords": ["연애"],
                "breakout_filter": {
                    "min_views": 1000,
                    "max_views": 5000,
                    "min_views_per_hour": 150
                }
            }
        }

    def _detect_chrome_executable(self) -> str:
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return r"C:\Program Files\Google\Chrome\Application\chrome.exe"

    def _ensure_default_files(self) -> None:
        if not self.config_path.exists():
            default_cfg = self._default_config()
            self.save_config(default_cfg)
        else:
            # 기존 config.json에 남아 있을 수 있는 기본 User Data 경로를 즉시 격리 구조로 정규화
            migrated = self.load_config()
            self.save_config(migrated)

        if not self.history_path.exists():
            self._write_json(self.history_path, [])

    def _read_json(self, path: Path, fallback: Any) -> Any:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return fallback

    def _write_json(self, path: Path, data: Any) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _normalize_chrome_browser_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """윈도우 기본 Chrome User Data 참조 및 레거시 필드를 제거하고 ./chrome_profiles/ 격리 매핑을 보장합니다."""
        default_cfg = self._default_config()
        chrome_cfg = cfg.get("chrome_browser", {})

        # 레거시 필드 제거 (기본 User Data 및 구형 옵션 차단)
        for legacy_key in ("user_data_dir", "use_isolated_profile_root", "isolated_user_data_root", "executable_path"):
            chrome_cfg.pop(legacy_key, None)

        chrome_cfg["channel"] = "chrome"
        chrome_cfg["profiles_root_dir"] = "./chrome_profiles"

        default_accounts = default_cfg["chrome_browser"]["accounts"]
        loaded_accounts = chrome_cfg.get("accounts", {})
        normalized_accounts: Dict[str, Any] = {}

        for acc_id, def_acc in default_accounts.items():
            raw_acc = loaded_accounts.get(acc_id, {}) if isinstance(loaded_accounts, dict) else {}
            safe_rel_dir = self.sanitize_account_user_data_dir(
                acc_id, raw_acc.get("user_data_dir")
            )
            folder_name = Path(safe_rel_dir).name
            # 실제 디렉터리 생성 보장
            self.resolve_account_abs_dir(acc_id, safe_rel_dir)

            normalized_accounts[acc_id] = {
                "account_id": acc_id,
                "label": raw_acc.get("label") or acc_id,
                "profile_folder": folder_name,
                "user_data_dir": safe_rel_dir,
                "description": f"{acc_id} 전용 격리 프로필 ({safe_rel_dir})",
            }

        chrome_cfg["accounts"] = normalized_accounts
        if chrome_cfg.get("selected_account_id") not in normalized_accounts:
            chrome_cfg["selected_account_id"] = "유튜브1"

        cfg["chrome_browser"] = chrome_cfg

        # 워크플로우 정책: 영상당 댓글 1개만 허용, 하루 게시 상한 기본 10개, 허용 키워드('연애') 및 뜨기 직전 영상 조건 보장
        policy = cfg.get("workflow_policy", {})
        if not isinstance(policy, dict):
            policy = {}
        policy["one_account_per_video"] = True
        try:
            limit_val = int(policy.get("daily_post_limit", 10))
            policy["daily_post_limit"] = max(1, limit_val)
        except Exception:
            policy["daily_post_limit"] = 10

        allowed_kws = policy.get("allowed_keywords")
        if not isinstance(allowed_kws, list) or not allowed_kws:
            policy["allowed_keywords"] = ["연애"]

        bf = policy.get("breakout_filter")
        if not isinstance(bf, dict):
            bf = {}
        policy["breakout_filter"] = {
            "min_views": int(bf.get("min_views", 1000) or 1000),
            "max_views": int(bf.get("max_views", 5000) or 5000),
            "min_views_per_hour": float(bf.get("min_views_per_hour", 150) or 150),
        }
        cfg["workflow_policy"] = policy
        return cfg

    def load_config(self) -> Dict[str, Any]:
        default_cfg = self._default_config()
        loaded = self._read_json(self.config_path, {})
        for section, values in default_cfg.items():
            if section not in loaded:
                loaded[section] = values
            elif isinstance(values, dict) and isinstance(loaded[section], dict):
                for k, v in values.items():
                    if k not in loaded[section]:
                        loaded[section][k] = v

        return self._normalize_chrome_browser_config(loaded)

    def save_config(self, new_config: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_chrome_browser_config(new_config)
        self._ensure_isolated_profile_dirs()
        self._write_json(self.config_path, normalized)
        return normalized

    def scan_local_chrome_profiles(self) -> List[Dict[str, str]]:
        """
        프로젝트 내부 격리 프로필 루트(./chrome_profiles/)의 계정별 전용 폴더 상태를 반환합니다.
        (윈도우 기본 AppData/Local/Google/Chrome/User Data 경로는 절대 조회하지 않음)
        """
        self._ensure_isolated_profile_dirs()
        cfg = self.load_config()
        accounts = cfg.get("chrome_browser", {}).get("accounts", {})
        profiles: List[Dict[str, str]] = []

        for acc_id, default_sub in self.ACCOUNT_SUBDIR_MAP.items():
            acc_info = accounts.get(acc_id, {})
            rel_dir = acc_info.get("user_data_dir") or f"./chrome_profiles/{default_sub}"
            abs_dir = self.resolve_account_abs_dir(acc_id, rel_dir)
            has_cookies = (
                (abs_dir / "Default" / "Network" / "Cookies").exists()
                or (abs_dir / "Default" / "Cookies").exists()
            )
            has_session = has_cookies or (abs_dir / "Local State").exists()
            status_tag = "세션 저장됨" if has_cookies else ("초기화됨" if has_session else "초기 로그인 필요")

            profiles.append({
                "account_id": acc_id,
                "profile_folder": abs_dir.name,
                "user_data_dir": rel_dir,
                "abs_path": str(abs_dir),
                "initialized": "true" if has_session else "false",
                "has_cookies": "true" if has_cookies else "false",
                "status_tag": status_tag,
                "label": f"{acc_id} -> {rel_dir} ({status_tag})",
            })

        return profiles

    def load_history(self) -> List[Dict[str, Any]]:
        raw_list = self._read_json(self.history_path, [])
        if not isinstance(raw_list, list):
            return []
        normalized: List[Dict[str, Any]] = []
        changed = False
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            if not item.get("history_id"):
                item["history_id"] = str(uuid.uuid4())[:8]
                changed = True
            raw_status = (item.get("status") or "SUCCESS").upper()
            if raw_status in ("POSTED", "SUCCESS"):
                item["status"] = "SUCCESS"
                item["result_label"] = "성공"
            else:
                item["status"] = "FAILED"
                item["result_label"] = "실패"
            item.setdefault("error_reason", "")
            normalized.append(item)

        # 시간순 정렬 (posted_at 기준 오름차순/내림차순 일관성을 위해 최신 시각이 위로 오되 정확한 시간순 정렬 보장)
        normalized.sort(key=lambda x: x.get("posted_at") or "", reverse=True)
        if changed:
            self._write_json(self.history_path, normalized)
        return normalized

    def append_history(self, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        history = self.load_history()
        entry = dict(item)
        entry["history_id"] = entry.get("history_id") or str(uuid.uuid4())[:8]
        raw_status = (entry.get("status") or "SUCCESS").upper()
        entry["status"] = "SUCCESS" if raw_status in ("POSTED", "SUCCESS") else "FAILED"
        entry["result_label"] = "성공" if entry["status"] == "SUCCESS" else "실패"
        entry["error_reason"] = entry.get("error_reason") or ""

        # 영상당 성공 댓글은 1개만 유지 (실패 이력은 시간순 로그로 보존)
        vid = entry.get("video_id")
        if vid and entry["status"] == "SUCCESS":
            history = [
                h for h in history
                if not (h.get("video_id") == vid and h.get("status") == "SUCCESS")
            ]

        history.insert(0, entry)
        history.sort(key=lambda x: x.get("posted_at") or "", reverse=True)
        history = history[:500]
        self._write_json(self.history_path, history)
        return history

    def remove_history_by_id(self, history_id: str) -> List[Dict[str, Any]]:
        """특정 히스토리 항목(history_id 또는 video_id)을 삭제합니다."""
        history = [
            h for h in self.load_history()
            if h.get("history_id") != history_id and h.get("video_id") != history_id
        ]
        self._write_json(self.history_path, history)
        return history

    def remove_history_by_video_id(self, video_id: str) -> List[Dict[str, Any]]:
        """특정 영상(video_id)의 성공 완료 히스토리 이력을 삭제합니다."""
        history = [h for h in self.load_history() if h.get("video_id") != video_id]
        self._write_json(self.history_path, history)
        return history

    def clear_history(self) -> List[Dict[str, Any]]:
        """모든 댓글 작업 히스토리를 초기화합니다."""
        self._write_json(self.history_path, [])
        return []

    def get_posted_account_for_video(self, video_id: str) -> Optional[str]:
        """특정 영상(video_id)에 이미 성공적으로 댓글을 작성한 계정(유튜브1/2/3)이 있는지 조회합니다."""
        for h in self.load_history():
            if h.get("video_id") == video_id and h.get("status", "SUCCESS") == "SUCCESS":
                return h.get("account_id") or "유튜브1"
        return None

    def get_today_success_count(self) -> int:
        """오늘 날짜(YYYY-MM-DD) 기준으로 댓글 게시에 성공(SUCCESS)한 건수를 반환합니다."""
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        count = 0
        for h in self.load_history():
            if (
                h.get("status", "SUCCESS") == "SUCCESS"
                and (h.get("posted_at") or "").startswith(today_prefix)
            ):
                count += 1
        return count

    def get_daily_quota_status(self) -> Dict[str, Any]:
        """하루 게시 상한(기본 10개) 대비 오늘 성공 건수 및 잔여 가능 횟수를 반환합니다."""
        cfg = self.load_config()
        limit = int(cfg.get("workflow_policy", {}).get("daily_post_limit", 10) or 10)
        today_count = self.get_today_success_count()
        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "today_success_count": today_count,
            "daily_post_limit": limit,
            "remaining": max(0, limit - today_count),
            "is_limit_reached": today_count >= limit,
        }
