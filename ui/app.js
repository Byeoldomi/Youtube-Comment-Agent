const state = {
  config: null,
  chromeProfiles: [],
  queue: [],
  history: [],
  dailyQuota: {
    today_success_count: 0,
    daily_post_limit: 10,
    remaining: 10,
    is_limit_reached: false,
  },
  historySortAsc: false,
  activeQueueId: null,
  selectedAccountId: "유튜브1",
  listFilter: "all",
  lastLogId: 0,
  isBusy: false,
  lastPostedVideoUrl: "",
};

const STEP_ORDER = ["IDLE", "OPENING_PROFILE", "TYPING", "COMPLETED"];

function setBusy(busy, statusText = "대기 중") {
  state.isBusy = busy;
  const pill = document.getElementById("globalStatusText");
  pill.textContent = statusText;
  document.getElementById("btnFetchVideos").disabled = busy;
  document.getElementById("btnRegenerateAI").disabled = busy;
  const loginSetupBtn = document.getElementById("btnOpenLoginSession");
  if (loginSetupBtn) {
    loginSetupBtn.disabled = busy;
  }
  updatePostButtonState();
}

function getActiveItem() {
  if (!state.activeQueueId) return null;
  return state.queue.find((q) => q.queue_id === state.activeQueueId) || null;
}

function updatePostButtonState() {
  const textarea = document.getElementById("finalCommentTextarea");
  const btn = document.getElementById("btnPostComment");
  const dupBanner = document.getElementById("duplicateGuardBanner");
  const dupAccName = document.getElementById("dupAccountName");
  const limitBanner = document.getElementById("dailyLimitBanner");
  const limitBannerText = document.getElementById("dailyLimitBannerText");
  const quotaBadge = document.getElementById("dailyQuotaBadge");

  const quota = state.dailyQuota || {
    today_success_count: 0,
    daily_post_limit: 10,
    is_limit_reached: false,
  };
  const todayCnt = quota.today_success_count ?? 0;
  const maxLimit = quota.daily_post_limit ?? 10;
  const limitReached = Boolean(quota.is_limit_reached || todayCnt >= maxLimit);

  if (quotaBadge) {
    quotaBadge.textContent = `오늘 게시: ${todayCnt} / ${maxLimit}개`;
    quotaBadge.classList.toggle("limit-reached", limitReached);
  }

  const activeItem = getActiveItem();
  const hasComment = textarea.value.trim().length > 0;
  const alreadyDone = Boolean(activeItem && (activeItem.is_completed || activeItem.status === "POSTED"));

  if (limitReached) {
    if (limitBanner && limitBannerText) {
      limitBannerText.textContent = `${todayCnt}/${maxLimit}개`;
      limitBanner.classList.remove("hidden");
    }
  } else if (limitBanner) {
    limitBanner.classList.add("hidden");
  }

  if (alreadyDone) {
    dupBanner.classList.remove("hidden");
    dupAccName.textContent = activeItem.posted_account || "유튜브1";
    btn.disabled = true;
    btn.textContent = `이미 [${activeItem.posted_account || "유튜브1"}] 프로필로 댓글 완료된 영상 (영상당 1개 제한)`;
  } else if (limitReached) {
    dupBanner.classList.add("hidden");
    btn.disabled = true;
    btn.textContent = `오늘 게시 상한 도달 (${todayCnt}/${maxLimit}개 완료)`;
  } else {
    dupBanner.classList.add("hidden");
    btn.disabled = state.isBusy || !hasComment || !activeItem;
    btn.textContent = `[${state.selectedAccountId}] 크롬 프로필로 댓글 달기 실행 (오늘 ${todayCnt}/${maxLimit})`;
  }
}

// ---------------- 4단계 실시간 스텝 인디케이터 & 에러 캡처 UI 업데이트 ----------------
function renderAutomationStepper(autoState) {
  if (!autoState) return;
  const currentStep = autoState.step || "IDLE";
  const stepLabel = autoState.step_label || "대기 중";

  document.getElementById("currentStepTextBadge").textContent = stepLabel;
  if (autoState.video_url) {
    state.lastPostedVideoUrl = autoState.video_url;
  }

  const currentIndex = STEP_ORDER.indexOf(currentStep);

  STEP_ORDER.forEach((stepKey, idx) => {
    const node = document.getElementById(`stepNode_${stepKey}`);
    if (!node) return;
    node.classList.remove("active", "done");

    if (currentStep === "ERROR") {
      if (stepKey === "IDLE") node.classList.add("done");
    } else if (idx < currentIndex) {
      node.classList.add("done");
    } else if (idx === currentIndex) {
      node.classList.add("active");
    }
  });

  // 프로필 잠금(SingletonLock) 배너 및 에러 캡처 카드 업데이트
  const lockBanner = document.getElementById("profileLockBanner");
  const lockBannerText = document.getElementById("profileLockBannerText");
  const errCard = document.getElementById("errorCaptureCard");
  const errReason = document.getElementById("errorReasonText");
  const errFileLabel = document.getElementById("errorFileNameLabel");
  const errImgWrap = document.getElementById("errorImageWrap");
  const errImg = document.getElementById("errorScreenshotImg");

  if (currentStep === "ERROR" && autoState.profile_locked) {
    if (lockBanner && lockBannerText) {
      lockBannerText.textContent =
        autoState.error_reason ||
        "이미 해당 프로필의 크롬 창이 열려 있습니다. 창을 닫고 다시 시도해주세요.";
      lockBanner.classList.remove("hidden");
    }
    errCard.classList.add("hidden");
  } else if (currentStep === "ERROR" && autoState.error_reason) {
    if (lockBanner) lockBanner.classList.add("hidden");
    errCard.classList.remove("hidden");
    errReason.textContent = autoState.error_reason;
    if (autoState.screenshot_path) {
      const parts = autoState.screenshot_path.split(/[\\/]/);
      errFileLabel.textContent = `screenshots/${parts[parts.length - 1]}`;
    } else {
      errFileLabel.textContent = "캡처 파일 없음";
    }

    if (autoState.screenshot_data_url) {
      errImg.src = autoState.screenshot_data_url;
      errImgWrap.classList.remove("hidden");
    } else {
      errImgWrap.classList.add("hidden");
    }
  } else if (
    currentStep === "OPENING_PROFILE" ||
    currentStep === "COMPLETED" ||
    stepLabel === "로그인 세팅 중"
  ) {
    if (lockBanner) lockBanner.classList.add("hidden");
    errCard.classList.add("hidden");
  }
}

// ---------------- 실시간 글자 수(40~120자) 및 금지어(URL/#) 체크 ----------------
function updateCommentCharAndRuleStatus(text) {
  const len = text.length;
  const countLabel = document.getElementById("charCountLabel");
  const rangeBadge = document.getElementById("charRangeStatus");
  const violationBadge = document.getElementById("ruleViolationBadge");

  countLabel.textContent = `공백 포함 ${len}자 (필수 40~120자)`;

  if (len === 0) {
    rangeBadge.textContent = "초안 대기";
    rangeBadge.className = "char-range-badge";
  } else if (len >= 40 && len <= 120) {
    rangeBadge.textContent = "✓ 40~120자 충족";
    rangeBadge.className = "char-range-badge valid";
  } else if (len < 40) {
    rangeBadge.textContent = `⚠ ${40 - len}자 부족 (최소 40자)`;
    rangeBadge.className = "char-range-badge invalid";
  } else {
    rangeBadge.textContent = `⚠ ${len - 120}자 초과 (최대 120자)`;
    rangeBadge.className = "char-range-badge invalid";
  }

  const hasUrl = /https?:\/\/\S+|www\.\S+/i.test(text);
  const hasHashtag = /#\S+/.test(text) || text.includes("#");

  if (hasUrl || hasHashtag) {
    violationBadge.classList.remove("hidden");
    violationBadge.classList.add("violation");
    const reasons = [];
    if (hasUrl) reasons.push("URL 포함 금지");
    if (hasHashtag) reasons.push("해시태그(#) 포함 금지");
    violationBadge.textContent = `⚠ 금지 규칙 위반: ${reasons.join(", ")}`;
  } else {
    violationBadge.classList.add("hidden");
    violationBadge.textContent = "";
  }
}

// ---------------- 초기화 ----------------
window.addEventListener("pywebviewready", async () => {
  await initializeDashboard();
  setInterval(pollBackendStatus, 500);
});

async function initializeDashboard() {
  try {
    const initData = await window.pywebview.api.get_initial_state();
    state.config = initData.config;
    state.chromeProfiles = initData.chrome_profiles || [];
    state.queue = initData.queue || [];
    state.history = initData.history || [];
    if (initData.daily_quota) {
      state.dailyQuota = initData.daily_quota;
    }

    hydrateSettingsUI();
    renderVideoList();
    renderHistoryList();
    appendLogs(initData.logs || []);
    renderAutomationStepper(initData.automation_state);
  } catch (err) {
    console.error("초기화 실패:", err);
  }
}

function hydrateSettingsUI() {
  const cfg = state.config;
  if (!cfg) return;

  document.getElementById("geminiKeyInput").value = cfg.api_keys?.gemini_api_key || "";
  document.getElementById("youtubeKeyInput").value = cfg.api_keys?.youtube_data_api_key || "";

  const chromeCfg = cfg.chrome_browser || {};
  const rootInput = document.getElementById("profilesRootInput");
  if (rootInput) {
    rootInput.value = chromeCfg.profiles_root_dir || "./chrome_profiles";
  }

  const dailyLimitEl = document.getElementById("dailyLimitInput");
  if (dailyLimitEl) {
    dailyLimitEl.value = cfg.workflow_policy?.daily_post_limit ?? 10;
  }

  const accounts = chromeCfg.accounts || {};
  const p1 = accounts["유튜브1"]?.user_data_dir || "./chrome_profiles/youtube_1";
  const p2 = accounts["유튜브2"]?.user_data_dir || "./chrome_profiles/youtube_2";
  const p3 = accounts["유튜브3"]?.user_data_dir || "./chrome_profiles/youtube_3";

  document.getElementById("mapProfile1Input").value = p1;
  document.getElementById("mapProfile2Input").value = p2;
  document.getElementById("mapProfile3Input").value = p3;

  document.getElementById("accSubLabel1").textContent = p1;
  document.getElementById("accSubLabel2").textContent = p2;
  document.getElementById("accSubLabel3").textContent = p3;

  state.selectedAccountId = chromeCfg.selected_account_id || "유튜브1";
  renderProfileSessionBadges();
  syncAccountRadioUI();
}

function renderProfileSessionBadges() {
  const badgeMap = {
    "유튜브1": document.getElementById("accSessionBadge1"),
    "유튜브2": document.getElementById("accSessionBadge2"),
    "유튜브3": document.getElementById("accSessionBadge3"),
  };

  Object.entries(badgeMap).forEach(([accId, badgeEl]) => {
    if (!badgeEl) return;
    const prof = (state.chromeProfiles || []).find((p) => p.account_id === accId);
    const hasCookies = prof && prof.has_cookies === "true";
    if (hasCookies) {
      badgeEl.textContent = "✓ 세션 저장됨";
      badgeEl.classList.add("saved");
    } else {
      badgeEl.textContent = "초기 로그인 필요";
      badgeEl.classList.remove("saved");
    }
  });
}

function syncAccountRadioUI() {
  document.querySelectorAll(".account-radio-card").forEach((card) => {
    const acc = card.dataset.account;
    const radio = card.querySelector("input[type='radio']");
    const isSelected = acc === state.selectedAccountId;
    card.classList.toggle("active", isSelected);
    if (radio) radio.checked = isSelected;
  });
  updatePostButtonState();
}

// ---------------- 로그 및 스텝 상태 실시간 폴링 ----------------
async function pollBackendStatus() {
  if (!window.pywebview || !window.pywebview.api) return;
  try {
    const res = await window.pywebview.api.poll_status(state.lastLogId);
    if (res.logs && res.logs.length > 0) {
      appendLogs(res.logs);
    }
    if (res.automation_state) {
      renderAutomationStepper(res.automation_state);
    }
  } catch (e) {
    // ignore transient polling errors
  }
}

function appendLogs(entries) {
  const box = document.getElementById("logConsoleBox");
  entries.forEach((item) => {
    state.lastLogId = Math.max(state.lastLogId, item.id);
    const div = document.createElement("div");
    div.className = `log-entry log-level-${item.level}`;
    div.innerHTML = `<span class="log-time">[${item.timestamp}]</span><span class="log-mod">[${item.module}]</span>${escapeHtml(item.message)}`;
    box.appendChild(div);
  });
  box.scrollTop = box.scrollHeight;
}

// ---------------- 이벤트 바인딩 ----------------
document.addEventListener("DOMContentLoaded", () => {
  const searchInput = document.getElementById("searchQueryInput");

  // 1번째 칸: [영상 불러오기] 버튼 클릭
  document.getElementById("btnFetchVideos").addEventListener("click", async () => {
    await handleFetchVideos();
  });

  searchInput.addEventListener("keydown", async (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      await handleFetchVideos();
    }
  });

  // 연애/고민 추천 키워드 칩 클릭
  document.querySelectorAll(".chip-btn").forEach((chip) => {
    chip.addEventListener("click", async () => {
      document.querySelectorAll(".chip-btn").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      searchInput.value = chip.dataset.keyword || "연애";
      await handleFetchVideos();
    });
  });

  // 1번째 칸 리스트 필터 탭
  document.querySelectorAll(".filter-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".filter-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.listFilter = btn.dataset.filter || "all";
      renderVideoList();
    });
  });

  // 2번째 칸: 추출된 원문 탭 전환
  document.querySelectorAll(".data-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".data-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const tab = btn.dataset.tab;
      document.getElementById("tabContentSource").classList.toggle("hidden", tab !== "source");
      document.getElementById("tabContentComments").classList.toggle("hidden", tab !== "comments");
    });
  });

  // 3번째 칸: 크롬 프로필 라디오 선택 (유튜브1 / 유튜브2 / 유튜브3)
  document.querySelectorAll(".account-radio-card").forEach((card) => {
    card.addEventListener("click", () => {
      if (state.isBusy) return;
      state.selectedAccountId = card.dataset.account || "유튜브1";
      const lockBanner = document.getElementById("profileLockBanner");
      if (lockBanner) lockBanner.classList.add("hidden");
      syncAccountRadioUI();
    });
  });

  // 3번째 칸: 접이식 설정/경로매핑 토글
  document.getElementById("btnToggleConfigModal").addEventListener("click", () => {
    document.getElementById("configCollapseBox").classList.toggle("hidden");
  });

  // 설정 저장
  document.getElementById("btnSaveConfig").addEventListener("click", async () => {
    await syncAndSaveConfig();
  });

  // 선택한 프로필(유튜브1/2/3) 초기 로그인 세팅용 크롬 창 열기 (로그인 완료 후 창을 닫을 때까지 대기)
  const btnOpenLoginSession = document.getElementById("btnOpenLoginSession");
  btnOpenLoginSession.addEventListener("click", async () => {
    await syncAndSaveConfig();
    const accId = state.selectedAccountId;
    btnOpenLoginSession.textContent = `[${accId}] 로그인 후 창 닫기 대기 중...`;
    setBusy(true, `[${accId}] 로그인 세팅 창 열림 · 구글 로그인 완료 후 크롬 창을 닫아주세요`);
    try {
      const res = await window.pywebview.api.open_chrome_login_session(accId);
      if (res && res.chrome_profiles) {
        state.chromeProfiles = res.chrome_profiles;
        renderProfileSessionBadges();
      }
      if (res && res.automation_state) {
        renderAutomationStepper(res.automation_state);
      }
      if (res && !res.ok && !res.profile_locked && res.message) {
        alert(res.message);
      }
    } finally {
      btnOpenLoginSession.textContent = "프로필 열기 (로그인 세팅용)";
      setBusy(false, "대기 중");
    }
  });

  // 2번째 칸: 데이터 다시 추출
  document.getElementById("btnRecollect").addEventListener("click", async () => {
    if (!state.activeQueueId) return;
    await selectAndProcessQueueItem(state.activeQueueId, true);
  });

  // 2번째 칸: [초안 다시 생성] 버튼
  document.getElementById("btnRegenerateAI").addEventListener("click", async () => {
    if (!state.activeQueueId) return;
    await syncAndSaveConfig();
    const extraPrompt = document.getElementById("extraPromptInput").value.trim();

    setBusy(true, "문투와 패턴을 변주하여 AI 댓글 초안 다시 생성 중...");
    try {
      const res = await window.pywebview.api.regenerate_ai_comments(
        state.activeQueueId,
        "friendly_viewer",
        "",
        extraPrompt
      );
      if (res.ok && res.item) {
        state.queue = res.queue || state.queue;
        renderVideoList();
        renderActiveWorkspace(res.item);
      } else {
        alert(res.message || "AI 댓글 초안 재생성에 실패했습니다.");
      }
    } finally {
      setBusy(false, "대기 중");
    }
  });

  // 2번째 칸: 댓글 초안 텍스트박스 실시간 글자수 & 규칙 검사
  const finalTextarea = document.getElementById("finalCommentTextarea");
  finalTextarea.addEventListener("input", () => {
    updateCommentCharAndRuleStatus(finalTextarea.value);
    updatePostButtonState();
  });

  // 3번째 칸: [선택한 크롬 프로필로 댓글 달기 실행] 버튼
  document.getElementById("btnPostComment").addEventListener("click", async () => {
    const activeItem = getActiveItem();
    if (!activeItem) return;

    if (activeItem.is_completed || activeItem.status === "POSTED") {
      alert(
        `이 영상은 이미 [${activeItem.posted_account || "유튜브1"}] 계정으로 댓글 작성을 완료했습니다.\n(1개 영상에는 1개 계정만 댓글을 달 수 있습니다.)`
      );
      return;
    }

    const commentText = finalTextarea.value.trim();
    if (!commentText) {
      alert("2번째 칸에 댓글 초안이 비어 있습니다.");
      return;
    }

    await syncAndSaveConfig();
    setBusy(true, `[${state.selectedAccountId}] 브라우저 자동화 진행 중...`);
    try {
      const res = await window.pywebview.api.post_comment_with_account(
        state.activeQueueId,
        commentText,
        state.selectedAccountId
      );

      if (res.queue) state.queue = res.queue;
      if (res.history) state.history = res.history;
      if (res.daily_quota) state.dailyQuota = res.daily_quota;
      if (res.automation_state) renderAutomationStepper(res.automation_state);

      if (res.daily_limit_blocked && res.message) {
        alert(res.message);
      }

      renderVideoList();
      renderHistoryList();
      if (res.item) renderActiveWorkspace(res.item);
    } finally {
      setBusy(false, "대기 중");
      updatePostButtonState();
    }
  });

  // 4번째 스텝 '게시 완료 · 댓글 보기' 클릭 시 해당 유튜브 영상 열기
  document.getElementById("stepNode_COMPLETED").addEventListener("click", async () => {
    const activeItem = getActiveItem();
    const targetUrl = state.lastPostedVideoUrl || activeItem?.url;
    if (targetUrl) {
      await window.pywebview.api.open_url_in_system_browser(targetUrl);
    }
  });

  // 로그 지우기
  document.getElementById("btnClearLogs").addEventListener("click", async () => {
    await window.pywebview.api.clear_logs();
    document.getElementById("logConsoleBox").innerHTML = "";
  });

  // 히스토리 시간순 정렬 방향 토글 (최신순 ▾ / 오래된순 ▲)
  const btnToggleHistorySort = document.getElementById("btnToggleHistorySort");
  if (btnToggleHistorySort) {
    btnToggleHistorySort.addEventListener("click", () => {
      state.historySortAsc = !state.historySortAsc;
      btnToggleHistorySort.textContent = state.historySortAsc ? "시간순(오래된↑)" : "최신순 ▾";
      renderHistoryList();
    });
  }

  // 히스토리 전체 비우기
  const btnClearHistory = document.getElementById("btnClearHistory");
  if (btnClearHistory) {
    btnClearHistory.addEventListener("click", async () => {
      if (state.history.length === 0) return;
      const res = await window.pywebview.api.clear_all_history();
      if (res.queue) state.queue = res.queue;
      if (res.history) state.history = res.history;
      if (res.daily_quota) state.dailyQuota = res.daily_quota;
      renderVideoList();
      renderHistoryList();
      const activeItem = getActiveItem();
      if (activeItem) renderActiveWorkspace(activeItem);
      updatePostButtonState();
    });
  }
});

// ---------------- 1번째 칸: 뜨기 직전 영상(조회수 1,000~5,000 & 시간당 150+) 탐색 및 리스트 렌더링 ----------------
async function handleFetchVideos() {
  if (state.isBusy) return;
  await syncAndSaveConfig();

  const queryInput = document.getElementById("searchQueryInput");
  const query = queryInput.value.trim() || "연애";
  queryInput.value = query;
  const maxResults = parseInt(document.getElementById("maxResultsSelect").value, 10) || 8;
  const breakoutCheckbox = document.getElementById("breakoutFilterCheckbox");
  const breakoutOnly = breakoutCheckbox ? breakoutCheckbox.checked : true;

  const statusMsg = breakoutOnly
    ? `'${query}' 뜨기 직전 영상(조회수 1,000~5,000 · 시간당 150+) 탐색 중...`
    : `'${query}' 영상 목록 불러오는 중...`;

  setBusy(true, statusMsg);
  try {
    const res = await window.pywebview.api.fetch_videos(query, maxResults, breakoutOnly);
    state.queue = res.queue || [];
    renderVideoList();

    if (!res.ok) {
      alert(res.message || "영상 목록을 불러오지 못했습니다.");
    }
  } finally {
    setBusy(false, "대기 중");
  }
}

function renderVideoList() {
  const totalCount = state.queue.length;
  const completedCount = state.queue.filter(
    (item) => item.is_completed || item.status === "POSTED"
  ).length;
  const pendingCount = totalCount - completedCount;

  document.getElementById("videoCountBadge").textContent = `${totalCount}개`;
  document.getElementById("filterCountAll").textContent = totalCount;
  document.getElementById("filterCountPending").textContent = pendingCount;
  document.getElementById("filterCountCompleted").textContent = completedCount;

  const filtered = state.queue.filter((item) => {
    const isDone = Boolean(item.is_completed || item.status === "POSTED");
    if (state.listFilter === "completed") return isDone;
    if (state.listFilter === "pending") return !isDone;
    return true;
  });

  const emptyBox = document.getElementById("videoListEmpty");
  const listEl = document.getElementById("videoListContainer");
  listEl.innerHTML = "";

  if (filtered.length === 0) {
    emptyBox.classList.remove("hidden");
    listEl.classList.add("hidden");
    return;
  }

  emptyBox.classList.add("hidden");
  listEl.classList.remove("hidden");

  filtered.forEach((item) => {
    const isDone = Boolean(item.is_completed || item.status === "POSTED");
    const isActive = item.queue_id === state.activeQueueId;
    const thumbUrl =
      item.thumbnail ||
      item.video_data?.thumbnail ||
      `https://i.ytimg.com/vi/${item.video_id}/mqdefault.jpg`;
    const channelName = item.channel || item.video_data?.channel || "채널 미상";
    const uploadDate = item.upload_date || item.video_data?.upload_date || "날짜 정보 없음";
    const viewCount = Number(item.view_count ?? item.video_data?.view_count ?? 0);
    const viewsPerHour = Number(item.views_per_hour ?? 0);
    const accTag = item.posted_account ? ` (${item.posted_account})` : "";

    let statsHtml = "";
    if (viewCount > 0 || viewsPerHour > 0) {
      const vcFormatted = viewCount.toLocaleString("ko-KR");
      const vphBadge =
        viewsPerHour > 0
          ? `<span class="stat-pill ${viewsPerHour >= 150 ? "vph-hot" : ""}">⚡ 시간당 ${escapeHtml(String(viewsPerHour))}회</span>`
          : "";
      statsHtml = `
        <div class="breakout-stats-row">
          <span class="stat-pill">조회수 ${escapeHtml(vcFormatted)}회</span>
          ${vphBadge}
        </div>
      `;
    }

    let badgeHtml = `<span class="completion-badge pending" data-toggle-id="${item.queue_id}" title="클릭하여 완료 상태 수동 전환">작업 대기</span>`;
    if (isDone) {
      badgeHtml = `<span class="completion-badge done" data-toggle-id="${item.queue_id}" title="이미 댓글 작업이 완료된 영상입니다">✓ 작업완료${escapeHtml(accTag)}</span>`;
    } else if (item.status === "GENERATED") {
      badgeHtml = `<span class="completion-badge generated" data-toggle-id="${item.queue_id}">초안 생성됨</span>`;
    } else if (item.status === "COLLECTED") {
      badgeHtml = `<span class="completion-badge pending" data-toggle-id="${item.queue_id}">추출 완료</span>`;
    }

    const li = document.createElement("li");
    li.className = `video-list-item ${isActive ? "active" : ""} ${isDone ? "completed" : ""}`;
    li.innerHTML = `
      <div class="video-item-thumb-wrap">
        <img class="video-item-thumb" src="${escapeHtml(thumbUrl)}" alt="썸네일" loading="lazy" />
        ${isDone ? `<div class="thumb-done-overlay">✓ 완료${escapeHtml(accTag)}</div>` : ""}
      </div>
      <div class="video-item-content">
        <div class="video-item-top-row">
          <div class="video-item-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</div>
        </div>
        ${statsHtml}
        <div class="video-item-meta-row">
          <div class="video-item-channel-date">
            <span>${escapeHtml(channelName)}</span>
            <span class="meta-divider">·</span>
            <span>${escapeHtml(uploadDate)}</span>
          </div>
          <div class="video-item-badges">
            ${badgeHtml}
          </div>
        </div>
      </div>
    `;

    li.addEventListener("click", async (e) => {
      const toggleBadge = e.target.closest("[data-toggle-id]");
      if (toggleBadge) {
        e.stopPropagation();
        const qId = toggleBadge.getAttribute("data-toggle-id");
        const res = await window.pywebview.api.mark_video_completed_toggle(qId);
        if (res.queue) state.queue = res.queue;
        if (res.history) state.history = res.history;
        if (res.daily_quota) state.dailyQuota = res.daily_quota;
        renderVideoList();
        renderHistoryList();
        if (res.item && res.item.queue_id === state.activeQueueId) {
          renderActiveWorkspace(res.item);
        }
        updatePostButtonState();
        return;
      }

      if (!state.isBusy) {
        await selectAndProcessQueueItem(item.queue_id);
      }
    });

    listEl.appendChild(li);
  });
}

// ---------------- 2번째 칸: 영상 클릭 시 데이터 추출 및 AI 초안 생성 ----------------
async function selectAndProcessQueueItem(queueId, forceRefresh = false) {
  state.activeQueueId = queueId;
  renderVideoList();

  const item = state.queue.find((q) => q.queue_id === queueId);
  if (!item) return;

  if (!forceRefresh && item.video_data) {
    renderActiveWorkspace(item);
    return;
  }

  const extraPrompt = document.getElementById("extraPromptInput").value.trim();
  setBusy(true, "선택한 영상의 자막(또는 제목/설명란)·인기댓글 추출 및 AI 초안 생성 중...");
  try {
    const res = await window.pywebview.api.collect_and_analyze_item(queueId, extraPrompt, true);
    if (res.queue) state.queue = res.queue;
    renderVideoList();
    if (res.item) {
      renderActiveWorkspace(res.item);
    } else if (!res.ok) {
      alert(res.message || "데이터 추출 중 오류가 발생했습니다.");
    }
  } finally {
    setBusy(false, "대기 중");
  }
}

function renderActiveWorkspace(item) {
  document.getElementById("centerEmptyState").classList.add("hidden");
  document.getElementById("centerWorkspace").classList.remove("hidden");

  const v = item.video_data || {};
  const isDone = Boolean(item.is_completed || item.status === "POSTED");

  document.getElementById("videoThumb").src =
    v.thumbnail || item.thumbnail || `https://i.ytimg.com/vi/${item.video_id}/mqdefault.jpg`;
  document.getElementById("videoTitleText").textContent = v.title || item.title;
  document.getElementById("videoChannelBadge").textContent = v.channel || item.channel || "채널 미상";
  document.getElementById("videoDateBadge").textContent = v.upload_date || item.upload_date || "날짜 미상";
  document.getElementById("transcriptSourceBadge").textContent = v.transcript_source || "추출 완료";

  const statusBadge = document.getElementById("videoCompletedStatusBadge");
  if (isDone) {
    statusBadge.textContent = `✓ 작업완료 (${item.posted_account || "유튜브1"})`;
    statusBadge.className = "meta-badge done";
  } else {
    statusBadge.textContent = "작업 대기";
    statusBadge.className = "meta-badge";
  }

  const extLink = document.getElementById("videoExternalLink");
  extLink.href = item.url;
  extLink.textContent = item.url;

  const sourceModeLabel = document.getElementById("sourceModeLabel");
  const sourceTextBox = document.getElementById("extractedSourceTextBox");
  if (v.has_transcript && (v.transcript || "").trim()) {
    sourceModeLabel.textContent = "자막 스크립트 추출됨";
    sourceTextBox.textContent = v.transcript;
  } else {
    sourceModeLabel.textContent = "자막 없음 → 제목 + 설명란 대체 추출";
    sourceTextBox.textContent = `[제목]\n${v.title || item.title}\n\n[설명란(더보기 텍스트)]\n${v.description || "(설명란 텍스트 없음)"}`;
  }

  const comments = v.top_comments || [];
  document.getElementById("commentCountNum").textContent = comments.length;
  const cList = document.getElementById("topCommentsList");
  cList.innerHTML = "";
  if (comments.length === 0) {
    cList.innerHTML = `<li class="comment-item">추출된 인기 댓글이 없습니다.</li>`;
  } else {
    comments.forEach((c) => {
      const li = document.createElement("li");
      li.className = "comment-item";
      li.innerHTML = `
        <div class="comment-item-header">
          <span>${escapeHtml(c.author)}</span>
          <span>좋아요 ${c.like_count}</span>
        </div>
        <div>${escapeHtml(c.text)}</div>
      `;
      cList.appendChild(li);
    });
  }

  const ai = item.ai_result;
  const worryBox = document.getElementById("worryOneLineBox");
  const sceneText = document.getElementById("referencedSceneText");
  const cardsBox = document.getElementById("candidateCardsContainer");
  cardsBox.innerHTML = "";

  if (ai) {
    worryBox.textContent = ai.worry_one_line || ai.video_summary || "고민 요약 결과 없음";
    sceneText.textContent = ai.referenced_scene || "영상 속 상황 및 대사 반영됨";

    (ai.suggested_comments || []).forEach((cand, idx) => {
      const card = document.createElement("div");
      card.className = `candidate-card ${cand.comment_text === item.draft_comment ? "selected" : ""}`;
      card.innerHTML = `
        <div>
          <span class="candidate-style-tag">${escapeHtml(cand.style_label || `변주 패턴 ${idx + 1}`)} (${(cand.comment_text || "").length}자)</span>
          <p class="candidate-text">${escapeHtml(cand.comment_text)}</p>
        </div>
        <div class="candidate-rationale">인용: ${escapeHtml(cand.rationale || "")}</div>
      `;
      card.addEventListener("click", () => {
        document.querySelectorAll(".candidate-card").forEach((el) => el.classList.remove("selected"));
        card.classList.add("selected");
        setEditorCommentText(cand.comment_text);
      });
      cardsBox.appendChild(card);
    });
  } else {
    worryBox.textContent =
      "우측 [설정 · 경로매핑 ▾]에서 Gemini API 키를 입력하고 [↻ 초안 다시 생성] 버튼을 눌러주세요.";
    sceneText.textContent = "-";
  }

  setEditorCommentText(item.draft_comment || "");
  updatePostButtonState();
}

function setEditorCommentText(text) {
  const textarea = document.getElementById("finalCommentTextarea");
  textarea.value = text;
  updateCommentCharAndRuleStatus(text);
  updatePostButtonState();
}

async function syncAndSaveConfig() {
  if (!state.config) return;
  state.config.api_keys.gemini_api_key = document.getElementById("geminiKeyInput").value.trim();
  state.config.api_keys.youtube_data_api_key = document.getElementById("youtubeKeyInput").value.trim();

  const p1 = "./chrome_profiles/youtube_1";
  const p2 = "./chrome_profiles/youtube_2";
  const p3 = "./chrome_profiles/youtube_3";

  state.config.chrome_browser.profiles_root_dir = "./chrome_profiles";
  state.config.chrome_browser.selected_account_id = state.selectedAccountId;

  state.config.chrome_browser.accounts["유튜브1"].user_data_dir = p1;
  state.config.chrome_browser.accounts["유튜브1"].profile_folder = "youtube_1";
  state.config.chrome_browser.accounts["유튜브2"].user_data_dir = p2;
  state.config.chrome_browser.accounts["유튜브2"].profile_folder = "youtube_2";
  state.config.chrome_browser.accounts["유튜브3"].user_data_dir = p3;
  state.config.chrome_browser.accounts["유튜브3"].profile_folder = "youtube_3";

  document.getElementById("mapProfile1Input").value = p1;
  document.getElementById("mapProfile2Input").value = p2;
  document.getElementById("mapProfile3Input").value = p3;

  document.getElementById("accSubLabel1").textContent = p1;
  document.getElementById("accSubLabel2").textContent = p2;
  document.getElementById("accSubLabel3").textContent = p3;

  const dailyLimitEl = document.getElementById("dailyLimitInput");
  if (dailyLimitEl) {
    const parsedLimit = parseInt(dailyLimitEl.value, 10);
    if (!state.config.workflow_policy) {
      state.config.workflow_policy = { one_account_per_video: true, daily_post_limit: 10 };
    }
    state.config.workflow_policy.one_account_per_video = true;
    state.config.workflow_policy.daily_post_limit =
      !isNaN(parsedLimit) && parsedLimit >= 1 ? parsedLimit : 10;
  }

  const res = await window.pywebview.api.save_settings(state.config);
  if (res.config) state.config = res.config;
  if (res.daily_quota) state.dailyQuota = res.daily_quota;
  updatePostButtonState();
}

function renderHistoryList() {
  document.getElementById("historyCountNum").textContent = state.history.length;
  const listEl = document.getElementById("historyListContainer");
  listEl.innerHTML = "";

  const sortedHistory = [...(state.history || [])].sort((a, b) => {
    const tA = a.posted_at || "";
    const tB = b.posted_at || "";
    return state.historySortAsc ? tA.localeCompare(tB) : tB.localeCompare(tA);
  });

  sortedHistory.forEach((h) => {
    const li = document.createElement("li");
    li.className = "history-item";
    const accLabel = h.account_id || "유튜브1";
    const isSuccess = (h.status || "SUCCESS").toUpperCase() === "SUCCESS";
    const statusClass = isSuccess ? "SUCCESS" : "FAILED";
    const statusText = isSuccess ? "✓ 성공" : "✕ 실패";
    const delKey = h.history_id || h.video_id || "";
    const videoUrl = h.url || (h.video_id ? `https://www.youtube.com/watch?v=${h.video_id}` : "");

    li.innerHTML = `
      <div class="queue-item-top">
        <div style="display:flex;align-items:center;gap:5px;">
          <span class="status-badge ${statusClass}">${statusText}</span>
          <span class="history-profile-tag">프로필: ${escapeHtml(accLabel)}</span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;">
          <span style="font-size:11px;color:#868E96;">${escapeHtml(h.posted_at)}</span>
          <button type="button" class="history-del-btn" data-delete-id="${escapeHtml(delKey)}" title="이 히스토리 기록 삭제">삭제</button>
        </div>
      </div>
      <div class="history-video-row">
        <span class="history-field-label">[영상]</span>
        <a class="history-video-link" data-open-url="${escapeHtml(videoUrl)}" title="${escapeHtml(h.title)} (${escapeHtml(videoUrl)})">${escapeHtml(h.title || h.video_id || "제목 없음")}</a>
      </div>
      <div class="history-comment-row">
        <span class="history-field-label">[댓글]</span> "${escapeHtml(h.comment || "")}"
      </div>
      ${
        !isSuccess && h.error_reason
          ? `<div class="history-error-row">사유: ${escapeHtml(h.error_reason)}</div>`
          : ""
      }
    `;

    const videoLink = li.querySelector("[data-open-url]");
    if (videoLink && videoUrl) {
      videoLink.addEventListener("click", async (e) => {
        e.preventDefault();
        e.stopPropagation();
        await window.pywebview.api.open_url_in_system_browser(videoUrl);
      });
    }

    const delBtn = li.querySelector("[data-delete-id]");
    if (delBtn && delKey) {
      delBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const res = await window.pywebview.api.delete_history_item(delKey);
        if (res.queue) state.queue = res.queue;
        if (res.history) state.history = res.history;
        if (res.daily_quota) state.dailyQuota = res.daily_quota;
        renderVideoList();
        renderHistoryList();
        const activeItem = getActiveItem();
        if (activeItem) renderActiveWorkspace(activeItem);
        updatePostButtonState();
      });
    }

    listEl.appendChild(li);
  });
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
