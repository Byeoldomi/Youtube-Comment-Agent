import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
import webview

from core.logger import AgentLogger
from core.config_manager import ConfigManager
from core.youtube_collector import YouTubeCollector
from core.gemini_agent import GeminiAgent
from core.browser_controller import BrowserController


class DashboardBridgeAPI:
    """pywebview 프론트엔드(JS)에 노출되는 Python API 클래스."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.logger = AgentLogger()
        self.config_mgr = ConfigManager(base_dir)
        self.collector = YouTubeCollector(self.logger)
        self.gemini = GeminiAgent(self.logger)
        self.browser = BrowserController(self.logger, base_dir)
        self.queue: List[Dict[str, Any]] = []

        self.logger.info("유튜브 댓글 에이전트 대시보드가 초기화되었습니다.", "SYSTEM")

    def _video_to_posted_account_map(self) -> Dict[str, str]:
        """video_id -> 댓글 작성 성공(SUCCESS) 완료된 계정명(유튜브1/2/3) 매핑 딕셔너리를 반환합니다."""
        mapping: Dict[str, str] = {}
        for h in self.config_mgr.load_history():
            if h.get("status", "SUCCESS") != "SUCCESS":
                continue
            vid = h.get("video_id")
            if vid and vid not in mapping:
                mapping[vid] = h.get("account_id") or "유튜브1"
        return mapping

    def _sync_completion_flags(self) -> None:
        acc_map = self._video_to_posted_account_map()
        for item in self.queue:
            vid = item.get("video_id")
            posted_acc = acc_map.get(vid, "")
            is_done = bool(posted_acc)
            item["is_completed"] = is_done
            item["posted_account"] = posted_acc
            if is_done:
                item["status"] = "POSTED"
            elif item.get("status") == "POSTED":
                item["status"] = (
                    "GENERATED"
                    if item.get("ai_result")
                    else ("COLLECTED" if item.get("video_data") else "WAITING")
                )

    # ---------------- 초기 상태 및 실시간 폴링 (로그 + 스텝 인디케이터) ----------------
    def get_initial_state(self) -> Dict[str, Any]:
        cfg = self.config_mgr.load_config()
        profiles = self.config_mgr.scan_local_chrome_profiles()
        history = self.config_mgr.load_history()
        self._sync_completion_flags()
        return {
            "config": cfg,
            "chrome_profiles": profiles,
            "queue": self.queue,
            "history": history,
            "daily_quota": self.config_mgr.get_daily_quota_status(),
            "logs": self.logger.get_logs_since(0),
            "automation_state": self.browser.get_automation_state(),
        }

    def poll_logs(self, last_log_id: int = 0) -> List[Dict[str, Any]]:
        return self.logger.get_logs_since(last_log_id)

    def poll_status(self, last_log_id: int = 0) -> Dict[str, Any]:
        """프론트엔드가 실시간 로그와 Playwright 스텝 인디케이터 상태를 함께 폴링합니다."""
        return {
            "logs": self.logger.get_logs_since(last_log_id),
            "automation_state": self.browser.get_automation_state(),
        }

    def reset_automation_step(self) -> Dict[str, Any]:
        return self.browser.reset_state()

    def open_url_in_system_browser(self, url: str) -> bool:
        if url:
            webbrowser.open(url)
            return True
        return False

    def clear_logs(self) -> bool:
        self.logger.clear()
        self.logger.info("로그 콘솔이 초기화되었습니다.", "SYSTEM")
        return True

    # ---------------- 설정 & 크롬 프로필(유튜브1/2/3) 관리 ----------------
    def save_settings(self, updated_config: Dict[str, Any]) -> Dict[str, Any]:
        saved = self.config_mgr.save_config(updated_config)
        self.logger.success("환경 설정 및 프로필 경로 매핑이 저장되었습니다. (data/config.json)", "CONFIG")
        return {
            "ok": True,
            "config": saved,
            "daily_quota": self.config_mgr.get_daily_quota_status(),
        }

    def refresh_chrome_profiles(self) -> List[Dict[str, str]]:
        profiles = self.config_mgr.scan_local_chrome_profiles()
        self.logger.info(f"전용 격리 크롬 프로필 폴더(./chrome_profiles) {len(profiles)}개를 확인했습니다.", "CONFIG")
        return profiles

    def open_chrome_login_session(self, account_id: Optional[str] = None) -> Dict[str, Any]:
        cfg = self.config_mgr.load_config()
        if account_id:
            cfg["chrome_browser"]["selected_account_id"] = account_id
            self.config_mgr.save_config(cfg)
        res = self.browser.open_login_session_browser(cfg, account_id=account_id)
        res["chrome_profiles"] = self.config_mgr.scan_local_chrome_profiles()
        return res

    # ---------------- 1번째 칸: 영상 불러오기 및 뜨기 직전 영상 탐색 ----------------
    def fetch_videos(
        self,
        query_or_url: str = "",
        max_results: int = 8,
        breakout_only: bool = True,
    ) -> Dict[str, Any]:
        try:
            cfg = self.config_mgr.load_config()
            yt_key = cfg.get("api_keys", {}).get("youtube_data_api_key", "")
            policy = cfg.get("workflow_policy", {})
            allowed_keywords = policy.get("allowed_keywords") or ["연애"]
            bf = policy.get("breakout_filter") or {}
            min_views = int(bf.get("min_views", 1000) or 1000)
            max_views = int(bf.get("max_views", 5000) or 5000)
            min_vph = float(bf.get("min_views_per_hour", 150) or 150.0)

            effective_query = (query_or_url or "").strip() or YouTubeCollector.DEFAULT_KEYWORD
            input_mode = YouTubeCollector.detect_input_mode(effective_query)

            # 키워드 검색 시 허용된 키워드('연애') 검증
            if input_mode == "keyword" and effective_query not in allowed_keywords:
                allowed_str = ", ".join(f"'{k}'" for k in allowed_keywords)
                msg = f"허용되지 않은 키워드입니다 ('{effective_query}'). 현재 허용된 키워드는 {allowed_str}입니다."
                self.logger.warn(msg, "POLICY")
                return {
                    "ok": False,
                    "keyword_blocked": True,
                    "allowed_keywords": allowed_keywords,
                    "message": msg,
                    "queue": self.queue,
                }

            found = self.collector.fetch_video_list(
                query_or_url=effective_query,
                max_results=int(max_results),
                youtube_api_key=yt_key,
                breakout_only=bool(breakout_only),
                min_views=min_views,
                max_views=max_views,
                min_views_per_hour=min_vph,
            )
            if not found:
                no_res_msg = (
                    f"'{effective_query}' 키워드로 조건(조회수 {min_views:,}~{max_views:,}회, 시간당 {min_vph:g}회 이상)을 만족하는 영상을 찾지 못했습니다."
                    if breakout_only and input_mode == "keyword"
                    else "검색 결과가 없거나 유효하지 않은 입력입니다."
                )
                self.logger.warn(no_res_msg, "COLLECTOR")
                return {
                    "ok": False,
                    "message": no_res_msg,
                    "queue": self.queue,
                }

            acc_map = self._video_to_posted_account_map()
            existing_map = {item["video_id"]: item for item in self.queue}
            new_list: List[Dict[str, Any]] = []

            for v in found:
                vid = v["video_id"]
                posted_acc = acc_map.get(vid, "")
                is_done = bool(posted_acc)
                if vid in existing_map:
                    existing_item = existing_map[vid]
                    existing_item["title"] = v.get("title") or existing_item["title"]
                    existing_item["channel"] = v.get("channel") or existing_item.get("channel", "")
                    existing_item["upload_date"] = v.get("upload_date") or existing_item.get("upload_date", "")
                    existing_item["thumbnail"] = v.get("thumbnail") or existing_item.get("thumbnail", "")
                    existing_item["view_count"] = v.get("view_count", existing_item.get("view_count", 0))
                    existing_item["views_per_hour"] = v.get("views_per_hour", existing_item.get("views_per_hour", 0.0))
                    existing_item["is_breakout"] = v.get("is_breakout", existing_item.get("is_breakout", False))
                    existing_item["is_completed"] = is_done
                    existing_item["posted_account"] = posted_acc
                    if is_done:
                        existing_item["status"] = "POSTED"
                    new_list.append(existing_item)
                else:
                    q_item = {
                        "queue_id": str(uuid.uuid4())[:8],
                        "video_id": vid,
                        "url": v["url"],
                        "title": v.get("title", f"YouTube ({vid})"),
                        "channel": v.get("channel", "채널 미상"),
                        "upload_date": v.get("upload_date", "날짜 정보 없음"),
                        "thumbnail": v.get("thumbnail", f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"),
                        "view_count": v.get("view_count", 0),
                        "views_per_hour": v.get("views_per_hour", 0.0),
                        "is_breakout": bool(v.get("is_breakout", False)),
                        "is_completed": is_done,
                        "posted_account": posted_acc,
                        "status": "POSTED" if is_done else "WAITING",
                        "video_data": None,
                        "ai_result": None,
                        "draft_comment": "",
                        "created_at": datetime.now().strftime("%H:%M:%S"),
                    }
                    new_list.append(q_item)

            new_ids = {item["video_id"] for item in new_list}
            for old_item in self.queue:
                if old_item["video_id"] not in new_ids:
                    new_list.append(old_item)

            self.queue = new_list
            self._sync_completion_flags()
            return {
                "ok": True,
                "applied_query": effective_query,
                "fetched_count": len(found),
                "queue": self.queue,
            }
        except Exception as e:
            self.logger.error(f"영상 목록 불러오기 오류: {e}", "COLLECTOR")
            return {"ok": False, "message": str(e), "queue": self.queue}

    def mark_video_completed_toggle(self, queue_id: str) -> Dict[str, Any]:
        target = next((item for item in self.queue if item["queue_id"] == queue_id), None)
        if not target:
            return {"ok": False, "queue": self.queue}
        if target.get("is_completed"):
            target["is_completed"] = False
            target["posted_account"] = ""
            target["status"] = (
                "GENERATED"
                if target.get("ai_result")
                else ("COLLECTED" if target.get("video_data") else "WAITING")
            )
            self.config_mgr.remove_history_by_video_id(target["video_id"])
            self.logger.info(
                f"작업 완료 해제 및 히스토리 이력 삭제 완료: {target.get('title', target['video_id'])}",
                "HISTORY",
            )
        else:
            cfg = self.config_mgr.load_config()
            acc_id = cfg.get("chrome_browser", {}).get("selected_account_id", "유튜브1")
            target["is_completed"] = True
            target["posted_account"] = acc_id
            target["status"] = "POSTED"
            self.config_mgr.append_history({
                "history_id": str(uuid.uuid4())[:8],
                "posted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "video_id": target["video_id"],
                "account_id": acc_id,
                "title": target["title"],
                "url": target["url"],
                "comment": target.get("draft_comment") or "(수동 완료 처리)",
                "status": "SUCCESS",
                "result_label": "성공",
                "error_reason": "",
            })
            self.logger.info(
                f"수동 작업 완료 처리 및 히스토리 등록: {target.get('title', target['video_id'])}",
                "HISTORY",
            )
        self._sync_completion_flags()
        return {
            "ok": True,
            "item": target,
            "queue": self.queue,
            "history": self.config_mgr.load_history(),
            "daily_quota": self.config_mgr.get_daily_quota_status(),
        }

    def delete_history_item(self, history_id_or_vid: str) -> Dict[str, Any]:
        """히스토리 목록에서 특정 이력(history_id 또는 video_id)을 삭제하고 상태를 동기화합니다."""
        updated_history = self.config_mgr.remove_history_by_id(history_id_or_vid)
        self._sync_completion_flags()
        target = next(
            (item for item in self.queue if item.get("video_id") == history_id_or_vid),
            None,
        )
        self.logger.info(f"히스토리 이력이 삭제되었습니다. (id={history_id_or_vid})", "HISTORY")
        return {
            "ok": True,
            "item": target,
            "queue": self.queue,
            "history": updated_history,
            "daily_quota": self.config_mgr.get_daily_quota_status(),
        }

    def clear_all_history(self) -> Dict[str, Any]:
        """히스토리 전체 비우기 및 모든 영상 완료 상태 초기화."""
        updated_history = self.config_mgr.clear_history()
        self._sync_completion_flags()
        self.logger.info("댓글 작업 히스토리가 모두 초기화되었습니다.", "HISTORY")
        return {
            "ok": True,
            "queue": self.queue,
            "history": updated_history,
            "daily_quota": self.config_mgr.get_daily_quota_status(),
        }

    # ---------------- 2번째 칸: 영상 선택 및 AI 댓글 초안 생성 ----------------
    def collect_and_analyze_item(
        self, queue_id: str, extra_prompt: str = "", run_ai: bool = True
    ) -> Dict[str, Any]:
        target = next((item for item in self.queue if item["queue_id"] == queue_id), None)
        if not target:
            return {"ok": False, "message": "목록에서 해당 영상을 찾을 수 없습니다."}

        cfg = self.config_mgr.load_config()
        try:
            video_data = self.collector.collect_video_package(target["url"], cfg)
            target["video_data"] = video_data
            target["title"] = video_data.get("title") or target["title"]
            target["channel"] = video_data.get("channel") or target.get("channel", "")
            target["upload_date"] = video_data.get("upload_date") or target.get("upload_date", "")
            target["thumbnail"] = video_data.get("thumbnail") or target.get("thumbnail", "")
            if not target.get("is_completed"):
                target["status"] = "COLLECTED"

            if run_ai and cfg.get("api_keys", {}).get("gemini_api_key", "").strip():
                ai_res = self.gemini.analyze_and_generate_comments(
                    video_data=video_data, config=cfg, extra_user_prompt=extra_prompt
                )
                target["ai_result"] = ai_res
                draft = ai_res.get("draft_comment", "")
                if not draft:
                    suggestions = ai_res.get("suggested_comments", [])
                    if suggestions:
                        draft = suggestions[0].get("comment_text", "")
                target["draft_comment"] = draft
                if not target.get("is_completed"):
                    target["status"] = "GENERATED"
            elif run_ai:
                self.logger.warn(
                    "영상 수집은 완료되었으나 Gemini API 키가 없어 AI 댓글 생성은 건너뛰었습니다.",
                    "GEMINI",
                )

            return {"ok": True, "item": target, "queue": self.queue}
        except Exception as e:
            if not target.get("is_completed"):
                target["status"] = "ERROR"
            self.logger.error(f"수집/분석 처리 실패: {e}", "SYSTEM")
            return {"ok": False, "message": str(e), "item": target, "queue": self.queue}

    def regenerate_ai_comments(
        self, queue_id: str, active_persona_id: str = "", custom_instruction: str = "", extra_prompt: str = ""
    ) -> Dict[str, Any]:
        target = next((item for item in self.queue if item["queue_id"] == queue_id), None)
        if not target or not target.get("video_data"):
            return {"ok": False, "message": "먼저 영상을 선택해 데이터를 수집해 주세요."}

        cfg = self.config_mgr.load_config()
        try:
            ai_res = self.gemini.analyze_and_generate_comments(
                video_data=target["video_data"], config=cfg, extra_user_prompt=extra_prompt
            )
            target["ai_result"] = ai_res
            draft = ai_res.get("draft_comment", "")
            if not draft:
                suggestions = ai_res.get("suggested_comments", [])
                if suggestions:
                    draft = suggestions[0].get("comment_text", "")
            target["draft_comment"] = draft
            if not target.get("is_completed"):
                target["status"] = "GENERATED"
            return {"ok": True, "item": target, "queue": self.queue}
        except Exception as e:
            self.logger.error(f"AI 댓글 재생성 실패: {e}", "GEMINI")
            return {"ok": False, "message": str(e)}

    # ---------------- 3번째 칸: 크롬 프로필 선택 및 댓글 달기 실행 (1영상 1댓글 & 일일 상한 10개) ----------------
    def post_comment_with_account(
        self, queue_id: str, final_comment_text: str, account_id: str = "유튜브1"
    ) -> Dict[str, Any]:
        target = next((item for item in self.queue if item["queue_id"] == queue_id), None)
        if not target:
            return {"ok": False, "message": "대상 영상을 찾을 수 없습니다."}

        # 정책 1: 영상당 댓글 1개만 허용 (1개 영상에는 1개 계정만 댓글 작성 가능)
        acc_map = self._video_to_posted_account_map()
        already_posted_acc = acc_map.get(target["video_id"])
        if already_posted_acc:
            dup_msg = (
                f"중복 방지 차단: 이 영상은 이미 [{already_posted_acc}] 프로필로 댓글을 작성했습니다. "
                "(영상당 댓글 1개만 허용됩니다.)"
            )
            self.logger.warn(dup_msg, "POLICY")
            return {
                "ok": False,
                "duplicate_blocked": True,
                "posted_account": already_posted_acc,
                "message": dup_msg,
                "item": target,
                "queue": self.queue,
                "history": self.config_mgr.load_history(),
                "daily_quota": self.config_mgr.get_daily_quota_status(),
            }

        # 정책 2: 하루 게시 상한 (기본 10개) 검증
        quota = self.config_mgr.get_daily_quota_status()
        if quota["is_limit_reached"]:
            limit_msg = (
                f"하루 게시 상한 도달: 오늘 이미 {quota['today_success_count']}개(일일 상한 {quota['daily_post_limit']}개)의 "
                "댓글을 게시하여 추가 게시가 차단되었습니다."
            )
            self.logger.warn(limit_msg, "POLICY")
            return {
                "ok": False,
                "daily_limit_blocked": True,
                "daily_quota": quota,
                "message": limit_msg,
                "item": target,
                "queue": self.queue,
                "history": self.config_mgr.load_history(),
            }

        cfg = self.config_mgr.load_config()
        cfg["chrome_browser"]["selected_account_id"] = account_id
        self.config_mgr.save_config(cfg)

        res = self.browser.post_comment_visually(
            video_url=target["url"],
            comment_text=final_comment_text,
            config=cfg,
            account_id=account_id,
        )

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if res.get("ok"):
            target["status"] = "POSTED"
            target["is_completed"] = True
            target["posted_account"] = account_id
            target["draft_comment"] = final_comment_text
            history_entry = {
                "history_id": str(uuid.uuid4())[:8],
                "posted_at": now_str,
                "video_id": target["video_id"],
                "account_id": account_id,
                "title": target["title"],
                "url": target["url"],
                "comment": final_comment_text,
                "status": "SUCCESS",
                "result_label": "성공",
                "error_reason": "",
            }
            updated_history = self.config_mgr.append_history(history_entry)
            self._sync_completion_flags()
            return {
                "ok": True,
                "message": res.get("message"),
                "item": target,
                "queue": self.queue,
                "history": updated_history,
                "daily_quota": self.config_mgr.get_daily_quota_status(),
                "automation_state": self.browser.get_automation_state(),
            }
        else:
            if not res.get("profile_locked"):
                target["status"] = "ERROR"
            history_entry = {
                "history_id": str(uuid.uuid4())[:8],
                "posted_at": now_str,
                "video_id": target["video_id"],
                "account_id": account_id,
                "title": target["title"],
                "url": target["url"],
                "comment": final_comment_text,
                "status": "FAILED",
                "result_label": "실패",
                "error_reason": res.get("message") or "자동화 실행 실패",
            }
            updated_history = self.config_mgr.append_history(history_entry)
            self._sync_completion_flags()
            return {
                "ok": False,
                "profile_locked": bool(res.get("profile_locked", False)),
                "message": res.get("message"),
                "item": target,
                "queue": self.queue,
                "history": updated_history,
                "daily_quota": self.config_mgr.get_daily_quota_status(),
                "screenshot_path": res.get("screenshot_path", ""),
                "screenshot_filename": res.get("screenshot_filename", ""),
                "screenshot_data_url": res.get("screenshot_data_url", ""),
                "automation_state": self.browser.get_automation_state(),
            }

    # 하위 호환 래퍼
    def confirm_and_post_comment(
        self, queue_id: str, final_comment_text: str, user_confirmed: bool = True
    ) -> Dict[str, Any]:
        cfg = self.config_mgr.load_config()
        acc_id = cfg.get("chrome_browser", {}).get("selected_account_id", "유튜브1")
        return self.post_comment_with_account(queue_id, final_comment_text, acc_id)


def main():
    base_dir = Path(__file__).resolve().parent
    ui_index = base_dir / "ui" / "index.html"

    bridge_api = DashboardBridgeAPI(base_dir)
    webview.create_window(
        title="YouTube Comment Agent Dashboard",
        url=str(ui_index),
        js_api=bridge_api,
        width=1580,
        height=950,
        min_size=(1260, 780),
        background_color="#F8F9FA",
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
