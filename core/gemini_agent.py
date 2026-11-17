import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from core.logger import AgentLogger


class SuggestedCommentItem(BaseModel):
    style_label: str = Field(description="적용된 문투/변주 패턴 이름 (예: 주변 지인 언급형, 궁금증 독백형, 현실 공감형)")
    comment_text: str = Field(
        description="공백 포함 40자 이상 120자 이하의 댓글 초안. 영상 속 구체적 장면/대사를 최소 1개 언급하고, '운명원' 사주/운세에 대한 자연스러운 호기심을 유도하며, URL/해시태그(#)/거짓 직접 체험 후기는 절대 포함하지 않음"
    )
    rationale: str = Field(description="언급한 영상 속 장면/대사 포인트 요약")


class VideoAnalysisResult(BaseModel):
    worry_one_line: str = Field(
        description="영상 속 핵심 연애 고민을 명확하게 짚어주는 1줄 요약 텍스트 (한 문장)"
    )
    draft_comment: str = Field(
        description="대표 AI 댓글 초안 (공백 포함 40자 이상 ~ 120자 이하 필수, 구체적 장면/대사 1개 이상 언급, 운명원 호기심 유도, URL/해시태그/거짓후기 금지)"
    )
    referenced_scene: str = Field(
        description="댓글 작성에 활용한 영상 속 구체적인 장면, 상황 또는 출연자/내레이션 대사"
    )
    suggested_comments: List[SuggestedCommentItem] = Field(
        description="서로 다른 문투와 패턴으로 변주한 추가 댓글 초안 3개 (모두 공백 포함 40~120자 규칙 준수)"
    )


VARIATION_PATTERNS = [
    {
        "name": "주변 이야기 전달형 (부드러운 구어체 ~네요/~더라고요)",
        "guide": "영상 속 특정 장면이나 대사에 깊이 공감하면서, '주변에 비슷한 연애 고민하던 친구가 운명원 사주 궁합 얘기하길래 궁금해지네요'처럼 지인/주변 언급 형태로 은근하게 녹여낼 것."
    },
    {
        "name": "호기심·궁금증 독백형 (친근한 댓글체 ~듯/ㅋㅋ/ㅠㅠ)",
        "guide": "출연자의 말이나 사연 상황에 찐하게 과몰입해 반응한 뒤, '이럴 때 사람들이 운명원 같은 데서 연애운이나 속마음 찾아보나 본데 진짜 맞는지 궁금하긴 함ㅋㅋ'처럼 자연스러운 호기심 형태로 작성할 것."
    },
    {
        "name": "댓글창 여론 공감형 (차분한 공감체 ~같아요/~싶네요)",
        "guide": "내레이션이나 상담 포인트 하나를 구체적으로 짚어주고, '이런 인연 문제는 답이 없어서 요즘 운명원 사주 같은 걸 많이들 찾아보나 봐요, 저도 한 번 알아보고 싶어지네요'처럼 은근하게 언급할 것."
    },
    {
        "name": "현실 연애 과몰입형 (감탄/탄식 후 질문형 ~나요?/~까요?)",
        "guide": "영상 속 가장 안타깝거나 답답했던 대사/장면을 콕 집어 언급하고, '인연이 따로 있는 건지 이럴 때 다들 얘기하는 운명원 궁합 같은 거 찾아보면 실마리가 좀 보이려나요'처럼 여운을 남길 것."
    },
]


SYSTEM_INSTRUCTION = """당신은 유튜브 연애/고민 영상을 끝까지 시청하고 깊이 공감한 '일반 시청자' 페르소나입니다.

[핵심 목적]
영상을 본 다른 시청자들이 댓글을 읽고 자연스럽게 '운명원'이라는 곳의 사주/운세에 호기심을 갖고 직접 검색해 찾아보고 싶어지도록 은근하게 유도해야 합니다.

[반드시 지켜야 할 필수 조건]
1. 구체적 장면/대사 언급 (필수 조건 1):
   - 제공된 영상 자막(자막이 없을 경우 제목·설명란·인기 댓글)에서 나온 구체적인 장면, 사연 상황, 또는 출연자/내레이션의 실제 대사를 최소 1개 이상 반드시 댓글에 자연스럽게 포함하세요.
2. 글자 수 제한 (필수 조건 2):
   - 댓글 초안은 반드시 공백 포함 40자 이상 ~ 120자 이하로 작성하세요. (너무 짧거나 120자를 넘기면 안 됩니다.)
3. 문투 및 패턴 변주 (필수 조건 3):
   - 영상마다, 생성할 때마다 문투(~네요, ~더라고요, ~듯, ~같아요, 질문형 등)와 문장 구조를 다르게 변주하여 기계적이거나 광고 봇 같은 느낌을 완전히 없애세요.

[절대 금지 사항 (어길 시 실패)]
1) URL 링크(http, https, www, 도메인 등) 절대 포함 금지
2) 해시태그(#운명원 등 '#' 기호 전체) 절대 포함 금지
3) '운명원'을 직접 이용해 본 것처럼 꾸며낸 거짓 체험 후기("운명원에서 봤는데 소름 돋았어요", "제가 운명원 사주 봤더니 다 맞았어요" 등) 절대 작성 금지!
   -> 반드시 "주변에서 얘기하길래", "다들 운명원 사주 언급하길래 궁금하네요", "이럴 때 운명원 같은 데서 궁합 찾아보나 본데 어떤지 궁금하긴 하네요"처럼 제3자 언급이나 자연스러운 호기심/궁금증 형태로만 녹여내세요.
"""


class GeminiAgent:
    def __init__(self, logger: AgentLogger):
        self.logger = logger
        self._variation_counter = 0
        self._last_working_model: Optional[str] = None

    @staticmethod
    def sanitize_comment(text: str) -> str:
        """URL 및 해시태그(#) 기호를 강제 제거하고 공백을 정리합니다."""
        if not text:
            return ""
        # URL 제거
        cleaned = re.sub(r"https?://\S+|www\.\S+", "", text)
        # 해시태그(#단어 또는 # 기호) 제거
        cleaned = re.sub(r"#\S+", "", cleaned)
        cleaned = cleaned.replace("#", "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def analyze_and_generate_comments(
        self,
        video_data: Dict[str, Any],
        config: Dict[str, Any],
        extra_user_prompt: str = "",
    ) -> Dict[str, Any]:
        api_key = config.get("api_keys", {}).get("gemini_api_key", "").strip()
        if not api_key:
            raise ValueError("Gemini API Key가 설정되지 않았습니다. 우측 설정 패널에서 Gemini API 키를 입력해 주세요.")

        ai_cfg = config.get("ai_settings", {})
        model_name = ai_cfg.get("gemini_model", "gemini-3.8-flash")

        # 매 생성마다 다른 문투/변주 패턴 선택
        pattern = VARIATION_PATTERNS[self._variation_counter % len(VARIATION_PATTERNS)]
        self._variation_counter += 1
        random_seed_hint = random.randint(1000, 9999)

        has_transcript = bool(video_data.get("has_transcript") and (video_data.get("transcript") or "").strip())
        comments_list = (video_data.get("top_comments") or [])[:10]
        comments_preview = "\n".join(
            [
                f"- [좋아요 {c.get('like_count', 0)}] {c.get('author', '시청자')}: {c.get('text', '')[:180]}"
                for c in comments_list
            ]
        )
        if not comments_preview:
            comments_preview = "(인기 댓글 없음)"

        if has_transcript:
            source_block = f"""[데이터 수집 모드: 1순위 - 영상 자막(스크립트) + 인기 댓글 상위 {len(comments_list)}개]
- 영상 제목: {video_data.get('title', '')}
- 채널명: {video_data.get('channel', '')}
- 추출된 자막(스크립트):
{(video_data.get('transcript') or '')[:12000]}
"""
        else:
            source_block = f"""[데이터 수집 모드: 2순위(자막 없음 폴백) - 영상 제목 + 설명란(더보기 텍스트) + 인기 댓글 상위 {len(comments_list)}개]
- 영상 제목: {video_data.get('title', '')}
- 채널명: {video_data.get('channel', '')}
- 영상 설명란(더보기 텍스트):
{(video_data.get('description') or '(설명란 텍스트 없음)')[:4000]}
"""

        user_prompt = f"""{source_block}

[인기 댓글 상위 반응 (최대 10개)]
{comments_preview}

[이번 생성에 적용할 대표 문투/변주 가이드 (시드 #{random_seed_hint})]
- 변주 스타일: {pattern['name']}
- 세부 지침: {pattern['guide']}
- 사용자 추가 요청: {extra_user_prompt if extra_user_prompt else '없음'}

[출력 요청]
1. `worry_one_line`: 이 영상에서 다루는 핵심 연애/인간관계 고민이 무엇인지 딱 1줄(한 문장)로 명확하게 요약해 주세요.
2. `draft_comment`: 위 시스템 규칙을 100% 준수한 대표 댓글 초안 1개를 작성하세요.
   - 반드시 영상 속 구체적인 장면/상황/출연자·내레이션 대사를 최소 1개 이상 언급할 것
   - 공백 포함 40자 이상 ~ 120자 이하 엄수 (권장 55~105자 사이)
   - URL 금지, 해시태그(#) 금지, '운명원' 직접 체험 거짓 후기 절대 금지 (주변 이야기, 소문, 호기심/궁금증 형태로만 자연스럽게 '운명원' 사주/운세 언급)
3. `suggested_comments`: 서로 다른 문투로 변주한 대안 초안 3개도 동일한 규칙(40~120자, 장면 언급, 운명원 호기심 유도, 금지사항 준수)으로 함께 생성해 주세요.
"""

        self.logger.info(
            f"Gemini AI 고민 1줄 요약 및 댓글 초안 생성 요청 (변주: {pattern['name']})",
            "GEMINI",
        )

        client = genai.Client(api_key=api_key)
        fallback_models = [
            self._last_working_model,
            "gemini-3.5-flash-lite",
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite",
            "gemini-flash-lite-latest",
            model_name,
            "gemini-3.8-flash",
            "gemini-3.7-flash",
        ]
        # None 및 중복 제거하면서 순서 유지
        model_candidates = list(dict.fromkeys([m for m in fallback_models if m]))

        def _call_model(cand_model: str):
            return client.models.generate_content(
                model=cand_model,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.85,
                    response_mime_type="application/json",
                    response_schema=VideoAnalysisResult,
                ),
            )

        response = None
        last_error = None
        for idx, candidate_model in enumerate(model_candidates):
            try:
                self.logger.info(f"'{candidate_model}' 모델로 댓글 초안 생성 중...", "GEMINI")
                with ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(_call_model, candidate_model)
                    response = future.result(timeout=15.0)
                self._last_working_model = candidate_model
                if idx > 0:
                    self.logger.success(
                        f"'{candidate_model}' 모델로 즉시 전환하여 응답 수신 성공!", "GEMINI"
                    )
                break
            except FuturesTimeoutError:
                last_error = TimeoutError(f"'{candidate_model}' 응답 시간 초과 (15초)")
                self.logger.warn(
                    f"'{candidate_model}' 서버 응답 지연(15초 초과) -> 대기 없이 다음 모델로 즉시 전환합니다.",
                    "GEMINI",
                )
            except Exception as api_err:
                last_error = api_err
                err_str = str(api_err)
                self.logger.warn(
                    f"'{candidate_model}' 트래픽 혼잡/제한 감지 ({err_str[:65]}...) -> 대기 없이 다음 모델로 즉시 전환합니다.",
                    "GEMINI",
                )

        if response is None:
            raise RuntimeError(f"모든 Gemini 모델 호출이 실패했습니다: {last_error}")

        result_dict = json.loads(response.text)

        # 후처리: URL 및 해시태그(#) 제거 보장
        draft = self.sanitize_comment(result_dict.get("draft_comment", ""))
        result_dict["draft_comment"] = draft

        sanitized_suggestions = []
        for item in result_dict.get("suggested_comments", []):
            c_txt = self.sanitize_comment(item.get("comment_text", ""))
            item["comment_text"] = c_txt
            sanitized_suggestions.append(item)
        result_dict["suggested_comments"] = sanitized_suggestions

        # 만약 draft_comment가 비어있거나 40~120자 범위를 벗어났는데 후보 중 범위 내 항목이 있다면 자동 보정
        if not (40 <= len(draft) <= 120):
            for cand in sanitized_suggestions:
                c_txt = cand.get("comment_text", "")
                if 40 <= len(c_txt) <= 120:
                    result_dict["draft_comment"] = c_txt
                    draft = c_txt
                    break

        # 프론트엔드 하위 호환 필드 유지
        result_dict["video_summary"] = result_dict.get("worry_one_line", "")

        self.logger.success(
            f"AI 초안 생성 완료 ({len(draft)}자): \"{draft[:45]}...\"",
            "GEMINI",
        )
        return result_dict
