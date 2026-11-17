import base64
import os
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page

from core.logger import AgentLogger


class BrowserController:
    """
    Playwright `launch_persistent_context` 기반 정식 Chrome(`channel="chrome"`) 격리 프로필 제어 컨트롤러.
    - 전용 격리 경로: ./chrome_profiles/youtube_1, youtube_2, youtube_3 (기본 User Data 절대 미사용)
    - 구글 자동화 탐지 우회:
      * channel="chrome" (내 PC에 설치된 정식 Chrome 사용)
      * ignore_default_args=["--enable-automation"]
      * args=["--disable-blink-features=AutomationControlled", ...]
    - 파일 잠금(SingletonLock / lockfile / exitCode=21) 감지 시 대시보드 안내 메시지 반환
    """

    ACCOUNT_SUBDIR_MAP: Dict[str, str] = {
        "유튜브1": "youtube_1",
        "유튜브2": "youtube_2",
        "유튜브3": "youtube_3",
    }

    PROFILE_LOCK_MESSAGE: str = (
        "이미 해당 프로필의 크롬 창이 열려 있습니다. 창을 닫고 다시 시도해주세요."
    )

    IGNORE_DEFAULT_ARGS: List[str] = [
        "--enable-automation",
        "--password-store=basic",
        "--use-mock-keychain",
        "--disable-sync",
    ]

    def __init__(self, logger: AgentLogger, base_dir: Path):
        self.logger = logger
        self.base_dir = base_dir.resolve()
        self.screenshots_dir = self.base_dir / "screenshots"
        self.profiles_root = self.base_dir / "chrome_profiles"

        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_root.mkdir(parents=True, exist_ok=True)
        for sub_name in self.ACCOUNT_SUBDIR_MAP.values():
            (self.profiles_root / sub_name).mkdir(parents=True, exist_ok=True)

        self._state_lock = threading.Lock()
        self._automation_state: Dict[str, Any] = {
            "step": "IDLE",  # IDLE | OPENING_PROFILE | TYPING | COMPLETED | ERROR
            "step_label": "대기 중",
            "account_id": "유튜브1",
            "video_url": "",
            "comment_text": "",
            "error_reason": "",
            "profile_locked": False,
            "screenshot_path": "",
            "screenshot_data_url": "",
            "updated_at": "",
        }

    def get_automation_state(self) -> Dict[str, Any]:
        with self._state_lock:
            return dict(self._automation_state)

    def _update_step(
        self,
        step: str,
        step_label: str,
        account_id: Optional[str] = None,
        video_url: Optional[str] = None,
        comment_text: Optional[str] = None,
        error_reason: str = "",
        profile_locked: bool = False,
        screenshot_path: str = "",
        screenshot_data_url: str = "",
    ) -> None:
        with self._state_lock:
            self._automation_state["step"] = step
            self._automation_state["step_label"] = step_label
            if account_id is not None:
                self._automation_state["account_id"] = account_id
            if video_url is not None:
                self._automation_state["video_url"] = video_url
            if comment_text is not None:
                self._automation_state["comment_text"] = comment_text
            self._automation_state["error_reason"] = error_reason
            self._automation_state["profile_locked"] = profile_locked
            self._automation_state["screenshot_path"] = screenshot_path
            self._automation_state["screenshot_data_url"] = screenshot_data_url
            self._automation_state["updated_at"] = datetime.now().strftime("%H:%M:%S")

    def reset_state(self) -> Dict[str, Any]:
        self._update_step("IDLE", "대기 중")
        return self.get_automation_state()

    @staticmethod
    def _build_korean_youtube_url(video_url: str) -> str:
        """한국어 유튜브 UI 기준(hl=ko, persist_hl=1) URL로 변환합니다."""
        parsed = urlparse(video_url.strip())
        qs = parse_qs(parsed.query)
        qs["hl"] = ["ko"]
        qs["persist_hl"] = ["1"]
        new_query = urlencode(qs, doseq=True)
        return urlunparse(
            (
                parsed.scheme or "https",
                parsed.netloc or "www.youtube.com",
                parsed.path,
                parsed.params,
                new_query,
                parsed.fragment,
            )
        )

    @staticmethod
    def _is_forbidden_default_chrome_dir(path_str: str) -> bool:
        """윈도우 기본 크롬 User Data 경로(AppData/Local/Google/Chrome/User Data) 사용 여부를 차단합니다."""
        if not path_str:
            return False
        norm = os.path.expandvars(path_str).replace("\\", "/").strip().lower()
        return "google/chrome/user data" in norm

    @staticmethod
    def _is_profile_dir_locked(abs_profile_dir: Path) -> bool:
        """
        프로필 폴더가 현재 실행 중인 크롬 창에 의해 잠겨 있는지(SingletonLock / Windows lockfile) 검사합니다.
        """
        singleton_lock = abs_profile_dir / "SingletonLock"
        if singleton_lock.exists() or singleton_lock.is_symlink():
            return True

        win_lockfile = abs_profile_dir / "lockfile"
        if win_lockfile.exists():
            try:
                # Windows Chrome은 실행 중일 때 lockfile을 배타적 잠금(ShareMode=0)으로 열어 둡니다.
                with open(win_lockfile, "rb"):
                    pass
            except PermissionError:
                return True
            except OSError:
                return True
        return False

    @staticmethod
    def _is_profile_lock_exception(err_msg: str, failed_during_launch: bool = False) -> bool:
        """Playwright 실행 예외 문자열에서 프로필 파일 잠금(SingletonLock / exitCode=21) 여부를 판별합니다."""
        lower = (err_msg or "").lower()
        lock_keywords = (
            "singletonlock",
            "user data directory is already in use",
            "profile in use",
            "processsingleton",
            "exitcode=21",
            "lockfile",
        )
        if any(k in lower for k in lock_keywords):
            return True
        if failed_during_launch and "launch_persistent_context" in lower and "closed" in lower:
            return True
        return False

    def _resolve_account_launch_args(
        self, config: Dict[str, Any], account_id: str
    ) -> Dict[str, Any]:
        chrome_cfg = config.get("chrome_browser", {})
        slow_mo = int(chrome_cfg.get("slow_mo_ms", 40))

        default_sub = self.ACCOUNT_SUBDIR_MAP.get(account_id, "youtube_1")
        default_rel_dir = f"./chrome_profiles/{default_sub}"

        accounts = chrome_cfg.get("accounts", {})
        acc_info = accounts.get(account_id) or {}
        raw_user_data_dir = (acc_info.get("user_data_dir") or default_rel_dir).strip()

        # 윈도우 기본 User Data 경로이거나 레거시 경로이면 강제로 ./chrome_profiles/youtube_X 로 격리
        if (
            not raw_user_data_dir
            or self._is_forbidden_default_chrome_dir(raw_user_data_dir)
            or "data/chrome_profiles" in raw_user_data_dir.replace("\\", "/").lower()
        ):
            rel_display = default_rel_dir
            abs_user_data_dir = (self.profiles_root / default_sub).resolve()
        else:
            candidate = Path(raw_user_data_dir)
            if candidate.is_absolute():
                abs_candidate = candidate.resolve()
            else:
                clean_rel = raw_user_data_dir.replace("\\", "/")
                if clean_rel.startswith("./"):
                    clean_rel = clean_rel[2:]
                abs_candidate = (self.base_dir / clean_rel).resolve()

            # 반드시 프로젝트 내부 ./chrome_profiles 하위 경로만 허용 (기본 User Data 원천 차단)
            try:
                rel_to_profiles = abs_candidate.relative_to(self.profiles_root.resolve())
                if str(rel_to_profiles) in ("", "."):
                    abs_user_data_dir = (self.profiles_root / default_sub).resolve()
                    rel_display = default_rel_dir
                else:
                    abs_user_data_dir = abs_candidate
                    rel_display = f"./chrome_profiles/{rel_to_profiles.as_posix()}"
            except ValueError:
                abs_user_data_dir = (self.profiles_root / default_sub).resolve()
                rel_display = default_rel_dir

        if self._is_forbidden_default_chrome_dir(str(abs_user_data_dir)):
            raise RuntimeError(
                "보안 정책 위반 차단: 윈도우 기본 Chrome User Data 경로는 절대 사용할 수 없습니다."
            )

        abs_user_data_dir.mkdir(parents=True, exist_ok=True)

        # 구글 자동화 제어 표시줄 및 봇 탐지 우회 필수 인자
        extra_args = [
            "--disable-blink-features=AutomationControlled",
            "--lang=ko-KR",
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized",
        ]

        return {
            "account_id": account_id,
            "user_data_dir": str(abs_user_data_dir),
            "abs_path": abs_user_data_dir,
            "rel_user_data_dir": rel_display,
            "profile_folder": abs_user_data_dir.name,
            "channel": "chrome",
            "ignore_default_args": list(self.IGNORE_DEFAULT_ARGS),
            "extra_args": extra_args,
            "slow_mo": slow_mo,
            "profile_label": f"{account_id} ({rel_display})",
        }

    def _build_persistent_context_kwargs(
        self, launch_info: Dict[str, Any], include_slow_mo: bool = True
    ) -> Dict[str, Any]:
        """
        Playwright `launch_persistent_context` 공통 실행 옵션을 생성합니다.
        - channel="chrome": 기본 Chromium이 아닌 PC에 설치된 정식 Chrome 사용
        - user_data_dir: 계정별 독립 프로필 폴더 (./chrome_profiles/youtube_1..3)
        - ignore_default_args=["--enable-automation"]: 자동화 제어 표시줄 제거
        - args=["--disable-blink-features=AutomationControlled", ...]: 봇 탐지 숨김
        """
        kwargs: Dict[str, Any] = {
            "user_data_dir": launch_info["user_data_dir"],
            "channel": "chrome",
            "headless": False,
            "chromium_sandbox": True,
            "ignore_default_args": launch_info["ignore_default_args"],
            "args": launch_info["extra_args"],
            "locale": "ko-KR",
            "no_viewport": True,
        }
        if include_slow_mo and launch_info["slow_mo"] > 0:
            kwargs["slow_mo"] = launch_info["slow_mo"]
        return kwargs

    @staticmethod
    def _first_visible_locator(page: Page, selector: str):
        """
        YouTube Polymer DOM에는 숨겨진(display:none) 중복 노드(하단 ytd-comments vs 우측 패널)가 공존하므로,
        매칭되는 요소 중 현재 화면에 실제로 보이는(is_visible) 첫 번째 Locator를 반환합니다.
        """
        loc = page.locator(selector)
        try:
            cnt = loc.count()
            for i in range(cnt):
                candidate = loc.nth(i)
                try:
                    if candidate.is_visible():
                        return candidate
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _capture_failure_screenshot(self, page: Optional[Page], reason: str) -> Dict[str, str]:
        """실패 발생 시 즉시 현재 브라우저 화면을 screenshots/ 폴더에 타임스탬프 파일명으로 저장하고 Base64 미리보기를 생성합니다."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = f"error_{ts}.png"
        save_path = self.screenshots_dir / file_name
        data_url = ""

        if page is not None:
            try:
                png_bytes = page.screenshot(path=str(save_path), full_page=False)
                b64_str = base64.b64encode(png_bytes).decode("ascii")
                data_url = f"data:image/png;base64,{b64_str}"
                self.logger.warn(f"에러 화면 캡처 저장 완료: screenshots/{file_name}", "PLAYWRIGHT")
            except Exception as cap_err:
                self.logger.warn(f"화면 캡처 중 예외 발생: {cap_err}", "PLAYWRIGHT")

        return {
            "screenshot_path": str(save_path) if save_path.exists() else "",
            "screenshot_filename": file_name if save_path.exists() else "",
            "screenshot_data_url": data_url,
            "reason": reason,
        }

    def open_login_session_browser(
        self, config: Dict[str, Any], account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        [프로필 열기 (로그인 세팅용)] 실행 메서드:
        - 정식 Chrome(`channel="chrome"`) + `launch_persistent_context` + 자동화 탐지 우회 옵션으로
          선택된 프로필 폴더(./chrome_profiles/youtube_X)를 연결하여 유튜브 메인 화면을 엽니다.
        - 자동 타이핑 없이 사용자가 직접 구글 로그인을 마치고 창을 닫을 때까지 대기하여 쿠키·세션을 영구 저장합니다.
        """
        selected_acc = account_id or config.get("chrome_browser", {}).get("selected_account_id", "유튜브1")
        launch_info = self._resolve_account_launch_args(config, selected_acc)
        youtube_home_url = "https://www.youtube.com/?hl=ko&persist_hl=1"

        # 사전 파일 잠금(SingletonLock / lockfile) 체크
        if self._is_profile_dir_locked(launch_info["abs_path"]):
            self.logger.error(self.PROFILE_LOCK_MESSAGE, "PLAYWRIGHT")
            self._update_step(
                step="ERROR",
                step_label="프로필 열림 감지",
                account_id=selected_acc,
                error_reason=self.PROFILE_LOCK_MESSAGE,
                profile_locked=True,
            )
            return {
                "ok": False,
                "profile_locked": True,
                "account_id": selected_acc,
                "message": self.PROFILE_LOCK_MESSAGE,
                "automation_state": self.get_automation_state(),
            }

        self._update_step(
            step="IDLE",
            step_label="로그인 세팅 중",
            account_id=selected_acc,
            error_reason="",
            profile_locked=False,
        )
        self.logger.info(
            f"[{launch_info['profile_label']}] 정식 Chrome(channel='chrome') 초기 로그인 세팅 창을 엽니다...",
            "LOGIN-SETUP",
        )

        context = None
        launched = False
        try:
            with sync_playwright() as p:
                kwargs = self._build_persistent_context_kwargs(launch_info, include_slow_mo=False)
                context = p.chromium.launch_persistent_context(**kwargs)
                launched = True

                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(youtube_home_url, wait_until="domcontentloaded")

                self.logger.success(
                    f"[{launch_info['profile_label']}] 유튜브 메인 창이 열렸습니다. 직접 구글 로그인을 마치고 창을 닫아주세요 (창을 닫을 때까지 대기)...",
                    "LOGIN-SETUP",
                )

                # 사용자가 로그인 완료 후 모든 탭/창을 닫을 때까지 무제한 대기
                try:
                    while len(context.pages) > 0:
                        active_page = context.pages[0]
                        if active_page.is_closed():
                            time.sleep(0.2)
                            continue
                        active_page.wait_for_event("close", timeout=0)
                except Exception:
                    pass

                try:
                    context.close()
                except Exception:
                    pass

            self._update_step(step="IDLE", step_label="대기 중", account_id=selected_acc)
            save_msg = (
                f"[{launch_info['profile_label']}] 크롬 창 종료 확인 · 로그인 세션과 쿠키가 '{launch_info['rel_user_data_dir']}'에 영구 저장되었습니다."
            )
            self.logger.success(save_msg, "LOGIN-SETUP")
            return {
                "ok": True,
                "cookies_saved": True,
                "account_id": selected_acc,
                "message": save_msg,
                "automation_state": self.get_automation_state(),
            }

        except Exception as e:
            err_msg = str(e)
            if self._is_profile_lock_exception(err_msg, failed_during_launch=not launched):
                self.logger.error(self.PROFILE_LOCK_MESSAGE, "PLAYWRIGHT")
                self._update_step(
                    step="ERROR",
                    step_label="프로필 열림 감지",
                    account_id=selected_acc,
                    error_reason=self.PROFILE_LOCK_MESSAGE,
                    profile_locked=True,
                )
                return {
                    "ok": False,
                    "profile_locked": True,
                    "account_id": selected_acc,
                    "message": self.PROFILE_LOCK_MESSAGE,
                    "automation_state": self.get_automation_state(),
                }

            if launched and "Target page, context or browser has been closed" in err_msg:
                self._update_step(step="IDLE", step_label="대기 중", account_id=selected_acc)
                save_msg = (
                    f"[{launch_info['profile_label']}] 로그인 세팅 창이 닫혀 세션과 쿠키가 저장되었습니다."
                )
                self.logger.success(save_msg, "LOGIN-SETUP")
                return {
                    "ok": True,
                    "cookies_saved": True,
                    "account_id": selected_acc,
                    "message": save_msg,
                    "automation_state": self.get_automation_state(),
                }

            self._update_step(
                step="ERROR",
                step_label="실행 오류",
                account_id=selected_acc,
                error_reason=err_msg,
            )
            self.logger.error(f"로그인 세팅 브라우저 실행 오류: {err_msg}", "LOGIN-SETUP")
            return {
                "ok": False,
                "account_id": selected_acc,
                "message": err_msg,
                "automation_state": self.get_automation_state(),
            }

    def post_comment_visually(
        self,
        video_url: str,
        comment_text: str,
        config: Dict[str, Any],
        account_id: str = "유튜브1",
    ) -> Dict[str, Any]:
        """
        3번째 칸 댓글 달기 브라우저 자동화 6단계 시퀀스:
        1. 정식 Chrome(`channel="chrome"`) + `launch_persistent_context` + 자동화 탐지 우회 옵션으로
           독립 프로필(./chrome_profiles/youtube_1..3) 실행 [프로필 여는 중]
        2. 해당 유튜브 영상 URL로 이동 (한국어 UI 기준: hl=ko)
        3. 댓글 창이 로드되도록 화면을 아래로 자연스럽게 스크롤 (#placeholder-area 또는 '댓글 추가...' 대기)
        4. 댓글 입력창 클릭 활성화 후 한 글자씩 랜덤 딜레이(50~150ms) 타이핑 [입력 중]
        5. 한국어 UI 기준 '댓글'(게시) 버튼 클릭
        6. 게시 후 잠시 대기하여 방금 작성한 내 댓글이 댓글 목록 최상단에 노출되는지 DOM에서 확인 [게시 완료 · 댓글 보기]
        * 중간 실패 시 즉시 화면 캡처(screenshots/) 및 에러 사유+이미지 반환
        """
        clean_comment = comment_text.strip()
        if not clean_comment:
            return {"ok": False, "message": "게시할 댓글 초안이 비어 있습니다."}

        launch_info = self._resolve_account_launch_args(config, account_id)
        chrome_cfg = config.get("chrome_browser", {})
        delay_min = int(chrome_cfg.get("typing_delay_min_ms", 50))
        delay_max = int(chrome_cfg.get("typing_delay_max_ms", 150))
        ko_video_url = self._build_korean_youtube_url(video_url)

        # 사전 파일 잠금(SingletonLock / lockfile) 체크
        if self._is_profile_dir_locked(launch_info["abs_path"]):
            self.logger.error(self.PROFILE_LOCK_MESSAGE, "PLAYWRIGHT")
            self._update_step(
                step="ERROR",
                step_label="프로필 열림 감지",
                account_id=account_id,
                video_url=ko_video_url,
                comment_text=clean_comment,
                error_reason=self.PROFILE_LOCK_MESSAGE,
                profile_locked=True,
            )
            return {
                "ok": False,
                "profile_locked": True,
                "step": "ERROR",
                "step_label": "프로필 열림 감지",
                "account_id": account_id,
                "message": self.PROFILE_LOCK_MESSAGE,
                "screenshot_path": "",
                "screenshot_filename": "",
                "screenshot_data_url": "",
            }

        # STEP 1: 프로필 여는 중
        self._update_step(
            step="OPENING_PROFILE",
            step_label="프로필 여는 중",
            account_id=account_id,
            video_url=ko_video_url,
            comment_text=clean_comment,
        )
        self.logger.info(
            f"[1/6] '{launch_info['profile_label']}' 정식 Chrome(channel='chrome') 프로필 여는 중 (headless=False)...",
            "PLAYWRIGHT",
        )

        page: Optional[Page] = None
        context = None
        launched = False

        with sync_playwright() as p:
            try:
                kwargs = self._build_persistent_context_kwargs(launch_info, include_slow_mo=True)
                kwargs["extra_http_headers"] = {"Accept-Language": "ko-KR,ko;q=0.9"}

                context = p.chromium.launch_persistent_context(**kwargs)
                launched = True

                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
                )
                page = context.pages[0] if context.pages else context.new_page()

                # STEP 2: 한국어 유튜브 영상 URL로 이동
                self.logger.info(f"[2/6] 유튜브 영상 페이지로 이동 중 (hl=ko): {ko_video_url}", "PLAYWRIGHT")
                page.goto(ko_video_url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(2.5)

                # 영상 일시정지 시도 (소리/재생 방해 방지)
                try:
                    page.evaluate(
                        "() => { const v = document.querySelector('video.html5-main-video'); if (v && !v.paused) v.pause(); }"
                    )
                except Exception:
                    pass

                # 로그인 상태 정밀 확인 (우측 상단 아바타 버튼 vs 로그인 버튼)
                avatar_btn = page.locator("ytd-masthead button#avatar-btn, ytd-masthead ytd-topbar-menu-button-renderer #avatar-btn")
                login_btn = page.locator(
                    "ytd-masthead a[href*='ServiceLogin'], "
                    "ytd-masthead a[href*='accounts.google.com'], "
                    "ytd-masthead ytd-button-renderer a[aria-label*='로그인'], "
                    "ytd-masthead yt-button-shape a:has-text('로그인')"
                )
                if avatar_btn.count() == 0 and login_btn.count() > 0 and login_btn.first.is_visible():
                    self.logger.info(
                        f"[{launch_info['profile_label']}] 유튜브 상단 '로그인' 버튼 감지 -> 저장된 구글 계정 세션 동기화를 시도합니다...",
                        "PLAYWRIGHT",
                    )
                    try:
                        login_btn.first.click()
                        page.wait_for_load_state("domcontentloaded", timeout=15000)
                        time.sleep(2.5)
                    except Exception:
                        pass

                    if "accounts.google.com" in page.url or (
                        avatar_btn.count() == 0 and login_btn.count() > 0 and login_btn.first.is_visible()
                    ):
                        raise RuntimeError(
                            f"[{launch_info['profile_label']}] 유튜브 로그인이 되어 있지 않습니다. "
                            "3번째 칸 상단의 [프로필 열기 (로그인 세팅용)] 버튼을 눌러 유튜브 우측 상단 '로그인'까지 완료한 뒤 창을 닫아주세요."
                        )

                # STEP 3: 댓글 창이 로드되도록 탐색 (2026 우측 댓글 패널 및 기존 하단 댓글창 모두 지원)
                self.logger.info(
                    "[3/6] 댓글 입력 영역('#placeholder-area' / '댓글 추가...') 탐색 중...",
                    "PLAYWRIGHT",
                )
                placeholder_selector = (
                    "ytd-comment-simplebox-renderer #placeholder-area, "
                    "ytd-engagement-panel-section-list-renderer #placeholder-area, "
                    "ytd-comments#comments #placeholder-area, "
                    "#simplebox-placeholder, "
                    "#placeholder-area"
                )

                visible_placeholder = self._first_visible_locator(page, placeholder_selector)

                # 만약 우측 댓글 탭/티저만 있고 아직 입력창이 안 열려 있다면 우측 '댓글' 버튼이나 티저 클릭 시도
                if visible_placeholder is None:
                    comment_panel_trigger = self._first_visible_locator(
                        page,
                        "ytd-watch-metadata #comment-teaser, "
                        "button[aria-label*='댓글'], "
                        "yt-button-shape button:has-text('댓글')",
                    )
                    if comment_panel_trigger is not None:
                        try:
                            comment_panel_trigger.click()
                            time.sleep(1.2)
                            visible_placeholder = self._first_visible_locator(page, placeholder_selector)
                        except Exception:
                            pass

                # 여전히 안 보이면 화면을 아래로 스크롤하며 탐색
                for scroll_step in range(1, 12):
                    if visible_placeholder is not None:
                        break
                    scroll_amount = random.randint(480, 720)
                    page.mouse.wheel(0, scroll_amount)
                    time.sleep(random.uniform(0.7, 1.1))
                    visible_placeholder = self._first_visible_locator(page, placeholder_selector)

                if visible_placeholder is None:
                    raise RuntimeError(
                        "댓글 입력 영역('#placeholder-area' / '댓글 추가...')을 찾지 못했습니다. (댓글이 사용 중지된 영상이거나 레이아웃 로드 지연)"
                    )

                try:
                    visible_placeholder.evaluate(
                        "el => el.scrollIntoView({ behavior: 'instant', block: 'center' })"
                    )
                except Exception:
                    try:
                        visible_placeholder.scroll_into_view_if_needed()
                    except Exception:
                        pass
                time.sleep(0.8)

                # STEP 4: 댓글 입력창 클릭 활성화 및 한 글자씩 랜덤 타이핑 (delay=50~150ms)
                self._update_step(
                    step="TYPING",
                    step_label="입력 중",
                    account_id=account_id,
                    video_url=ko_video_url,
                    comment_text=clean_comment,
                )
                self.logger.info(
                    f"[4/6] 댓글 입력창 활성화 및 타이핑 시작 (글자당 {delay_min}~{delay_max}ms 랜덤 딜레이)...",
                    "PLAYWRIGHT",
                )

                input_selector = (
                    "ytd-commentbox #contenteditable-root, "
                    "ytd-engagement-panel-section-list-renderer #contenteditable-root, "
                    "ytd-comments#comments #contenteditable-root, "
                    "div#contenteditable-root[contenteditable='true']"
                )

                # 댓글 입력창(#contenteditable-root) 활성화 재시도 루프 (채널 생성 모달 및 우측 패널 대응)
                visible_input_box = None
                for attempt in range(1, 6):
                    visible_placeholder = self._first_visible_locator(page, placeholder_selector)
                    if visible_placeholder is not None:
                        try:
                            visible_placeholder.click(force=True, timeout=3000)
                        except Exception:
                            try:
                                visible_placeholder.evaluate("el => el.click()")
                            except Exception:
                                pass

                    time.sleep(1.0)

                    if "accounts.google.com" in page.url:
                        raise RuntimeError(
                            f"[{launch_info['profile_label']}] 댓글창 클릭 후 구글 로그인 페이지로 이동되었습니다. "
                            "[프로필 열기 (로그인 세팅용)] 버튼으로 유튜브 로그인을 완료해 주세요."
                        )

                    # 로그인 유도 모달 감지
                    signin_modal_btn = self._first_visible_locator(
                        page,
                        "ytd-modal-with-title-and-button-renderer a[href*='ServiceLogin'], "
                        "ytd-modal-with-title-and-button-renderer a[href*='accounts.google.com'], "
                        "tp-yt-paper-dialog a[href*='ServiceLogin']",
                    )
                    if signin_modal_btn is not None:
                        raise RuntimeError(
                            f"[{launch_info['profile_label']}] 유튜브 로그인이 풀려 있어 댓글을 작성할 수 없습니다. "
                            "[프로필 열기 (로그인 세팅용)] 버튼을 눌러 유튜브에 로그인해 주세요."
                        )

                    # 신규 구글 계정의 '채널 만들기 / 핸들 확인' 팝업 자동 수락 처리
                    create_channel_btn = self._first_visible_locator(
                        page,
                        "ytd-channel-creation-dialog-renderer #create-channel-button button, "
                        "ytd-channel-creation-dialog-renderer button:has-text('채널 만들기'), "
                        "tp-yt-paper-dialog button:has-text('채널 만들기'), "
                        "tp-yt-paper-dialog button:has-text('계속')",
                    )
                    if create_channel_btn is not None:
                        self.logger.info(
                            "[4/6] 유튜브 최초 댓글 작성용 '채널 만들기' 팝업 감지 -> 자동으로 채널 생성을 완료합니다...",
                            "PLAYWRIGHT",
                        )
                        try:
                            create_channel_btn.click()
                            time.sleep(2.5)
                        except Exception:
                            pass
                        continue

                    visible_input_box = self._first_visible_locator(page, input_selector)
                    if visible_input_box is not None:
                        break

                if visible_input_box is None:
                    raise RuntimeError(
                        "댓글 입력창('#contenteditable-root')이 활성화되지 않았습니다. (유튜브 레이아웃 로드 지연 또는 로그인/채널 생성 필요)"
                    )

                visible_input_box.click()
                time.sleep(0.4)

                # 한 글자씩 50~150ms 랜덤 딜레이로 사람이 치는 것처럼 타이핑
                for ch in clean_comment:
                    char_delay_ms = random.randint(delay_min, delay_max)
                    page.keyboard.type(ch, delay=char_delay_ms)

                time.sleep(0.9)

                # STEP 5: 한국어 UI 기준 '댓글'(게시) 버튼 클릭 (하단 댓글창 & 우측 댓글 패널 모두 호환)
                self.logger.info("[5/6] 한국어 UI '댓글'(게시) 버튼 클릭 중...", "PLAYWRIGHT")
                submit_selector = (
                    "ytd-commentbox #submit-button button, "
                    "ytd-commentbox ytd-button-renderer#submit-button button, "
                    "ytd-engagement-panel-section-list-renderer #submit-button button, "
                    "ytd-comments#comments #submit-button button, "
                    "#submit-button button[aria-label*='댓글'], "
                    "#submit-button button:has-text('댓글')"
                )

                visible_submit_btn = None
                for _ in range(10):
                    visible_submit_btn = self._first_visible_locator(page, submit_selector)
                    if visible_submit_btn is not None:
                        break
                    time.sleep(0.5)

                if visible_submit_btn is None:
                    raise RuntimeError("'댓글' 게시 버튼을 화면에서 찾지 못했습니다.")

                if visible_submit_btn.is_disabled():
                    raise RuntimeError("'댓글' 게시 버튼이 비활성화(disabled) 상태입니다.")

                try:
                    visible_submit_btn.click(timeout=5000)
                except Exception:
                    visible_submit_btn.evaluate("el => el.click()")

                # STEP 6: 게시 후 잠시 대기하여 방금 작성한 내 댓글이 댓글 목록 최상단에 노출되는지 DOM 확인
                self.logger.info(
                    "[6/6] 게시 후 댓글 목록 최상단 DOM 노출 여부 확인 대기 중...", "PLAYWRIGHT"
                )
                probe_snippet = clean_comment[:18].strip()
                dom_verified = False
                comment_text_selector = (
                    "ytd-comment-thread-renderer #content-text, "
                    "ytd-comment-view-model #content-text, "
                    "ytd-engagement-panel-section-list-renderer #content-text, "
                    "ytd-comments#comments #content-text"
                )

                for _ in range(14):
                    time.sleep(1.0)
                    all_threads = page.locator(comment_text_selector)
                    total_cnt = min(all_threads.count(), 15)
                    for idx in range(total_cnt):
                        node = all_threads.nth(idx)
                        try:
                            if not node.is_visible():
                                continue
                            txt = (node.inner_text() or "").strip()
                            if probe_snippet and probe_snippet in txt:
                                dom_verified = True
                                break
                        except Exception:
                            continue
                    if dom_verified:
                        break

                if not dom_verified:
                    raise RuntimeError(
                        "댓글 게시 버튼을 클릭했으나, 댓글 목록 최상단 DOM에서 방금 작성한 댓글이 확인되지 않았습니다. (스팸 필터링 또는 게시 지연)"
                    )

                self.logger.success(
                    f"댓글 목록 최상단 DOM 노출 확인 완료! [{launch_info['profile_label']}]",
                    "PLAYWRIGHT",
                )
                time.sleep(1.5)

                self._update_step(
                    step="COMPLETED",
                    step_label="게시 완료 · 댓글 보기",
                    account_id=account_id,
                    video_url=ko_video_url,
                    comment_text=clean_comment,
                )
                context.close()
                return {
                    "ok": True,
                    "step": "COMPLETED",
                    "step_label": "게시 완료 · 댓글 보기",
                    "account_id": account_id,
                    "video_url": ko_video_url,
                    "message": f"[{account_id}] 댓글 게시 및 최상단 DOM 노출 확인이 완료되었습니다!",
                }

            except Exception as e:
                raw_err = str(e)
                is_lock_err = self._is_profile_lock_exception(raw_err, failed_during_launch=not launched)

                if is_lock_err:
                    friendly_reason = self.PROFILE_LOCK_MESSAGE
                elif isinstance(e, PlaywrightTimeoutError):
                    friendly_reason = f"요소 대기 시간 초과 (요소를 찾지 못했거나 로그인이 풀려 있음): {raw_err}"
                else:
                    friendly_reason = raw_err

                # sync_playwright() 블록 내부에서 캡처하므로 이벤트 루프가 살아 있는 상태에서 정상 캡처됨
                cap_info = (
                    {"screenshot_path": "", "screenshot_filename": "", "screenshot_data_url": ""}
                    if is_lock_err
                    else self._capture_failure_screenshot(page, friendly_reason)
                )
                try:
                    if context is not None:
                        context.close()
                except Exception:
                    pass

                step_lbl = "프로필 열림 감지" if is_lock_err else "실패 (캡처 확인)"
                self._update_step(
                    step="ERROR",
                    step_label=step_lbl,
                    account_id=account_id,
                    video_url=ko_video_url,
                    comment_text=clean_comment,
                    error_reason=friendly_reason,
                    profile_locked=is_lock_err,
                    screenshot_path=cap_info["screenshot_path"],
                    screenshot_data_url=cap_info["screenshot_data_url"],
                )
                self.logger.error(f"댓글 달기 실패: {friendly_reason}", "PLAYWRIGHT")
                return {
                    "ok": False,
                    "profile_locked": is_lock_err,
                    "step": "ERROR",
                    "step_label": step_lbl,
                    "account_id": account_id,
                    "message": friendly_reason,
                    "screenshot_path": cap_info["screenshot_path"],
                    "screenshot_filename": cap_info["screenshot_filename"],
                    "screenshot_data_url": cap_info["screenshot_data_url"],
                }
