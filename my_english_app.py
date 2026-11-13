import os
import re
import json
import sqlite3
import hashlib
import time
from datetime import datetime, date
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIStatusError, APIConnectionError, APITimeoutError

# ---------------------------------------------------------
# 기본 설정
# ---------------------------------------------------------
load_dotenv()
APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "english_learning.db"
MODEL_NAME = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
DAILY_API_LIMIT = max(1, int(os.getenv("DAILY_API_LIMIT", "30")))
MONTHLY_API_BUDGET_USD = max(0.1, float(os.getenv("MONTHLY_API_BUDGET_USD", "5")))

# USD per 1M tokens. Keep estimates aligned with the selected model.
MODEL_PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}
def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    input_price, output_price = MODEL_PRICES.get(model, MODEL_PRICES["gpt-4o-mini"])
    return (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000

st.set_page_config(
    page_title="Natural English Lab",
    page_icon="🗣️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------
# 데이터베이스
# ---------------------------------------------------------
def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db():
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS study_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                korean_input TEXT NOT NULL,
                result_json TEXT NOT NULL,
                cache_key TEXT
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(study_history)")}
        if "cache_key" not in columns:
            conn.execute("ALTER TABLE study_history ADD COLUMN cache_key TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_study_cache_key ON study_history(cache_key)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL
            )
            """
        )
        conn.commit()


def make_cache_key(korean_input: str, tone: str) -> str:
    # 캐시 버전(v4): 번역 응답 형식 및 검증 로직 변경으로 이전 캐시와 분리
    normalized = " ".join(korean_input.strip().casefold().split())
    raw = f"v4_{normalized}\n{tone.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_cached_result(cache_key: str):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT result_json FROM study_history WHERE cache_key = ? LIMIT 1",
            (cache_key,),
        ).fetchone()
    if not row:
        return None
    return json.loads(row["result_json"])


def save_study(korean_input: str, result: dict, cache_key: str):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO study_history
                (created_at, korean_input, result_json, cache_key)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                created_at = excluded.created_at,
                korean_input = excluded.korean_input,
                result_json = excluded.result_json
            """,
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                korean_input,
                json.dumps(result, ensure_ascii=False),
                cache_key,
            ),
        )
        conn.commit()


def count_api_calls_today():
    today = date.today().isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM api_usage WHERE substr(created_at, 1, 10) = ?",
            (today,),
        ).fetchone()
    return int(row["n"])


def monthly_usage():
    month = datetime.now().strftime("%Y-%m")
    with get_connection() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens), 0) AS input_tokens,
                      COALESCE(SUM(completion_tokens), 0) AS output_tokens,
                      COALESCE(SUM(cost_usd), 0) AS cost
               FROM api_usage WHERE substr(created_at, 1, 7) = ?""",
            (month,),
        ).fetchone()
    return dict(row)


def start_api_attempt():
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO api_usage (created_at, model, status) VALUES (?, ?, 'pending')",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), MODEL_NAME),
        )
        conn.commit()
        return cur.lastrowid


def finish_api_attempt(attempt_id: int, status: str, prompt_tokens=0, completion_tokens=0):
    cost = estimate_cost(MODEL_NAME, prompt_tokens, completion_tokens)
    with get_connection() as conn:
        conn.execute(
            "UPDATE api_usage SET status = ?, prompt_tokens = ?, completion_tokens = ?, cost_usd = ? WHERE id = ?",
            (status, prompt_tokens, completion_tokens, cost, attempt_id),
        )
        conn.commit()
    return cost


def load_history(limit=100):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, korean_input, result_json
            FROM study_history
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return rows


def delete_history(record_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM study_history WHERE id = ?", (record_id,))
        conn.commit()


initialize_db()

# ---------------------------------------------------------
# API 키
# ---------------------------------------------------------
def get_api_key():
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if key:
        return key
    try:
        return st.secrets.get("OPENAI_API_KEY", "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------
# AI 프롬프트 및 결과 생성
# ---------------------------------------------------------
SYSTEM_PROMPT = """
You are an expert English coach for Korean learners.
Translate the user's Korean sentence into natural, authentic American English.

Return exactly one valid JSON object. Do not use Markdown fences or any text before or after JSON.
Language separation is mandatory:
- natural_translation, polite_spoken, written, pattern, and examples[*].english: English only.
- natural_korean, polite_korean, written_korean, pattern_meaning, pattern_explanation,
  grammar_notes, and examples[*].korean: Korean only.
- Never put Korean characters in any English-only field.
- Translate the user's actual input. Do not copy the sample sentence as the answer.
- Provide exactly five examples, each with both english and korean.
- Keep all requested keys and use strings for text fields and an array for grammar_notes/examples.
"""


def build_user_prompt(korean_text: str, tone: str):
    return f"""Translate this Korean sentence into natural American English.

Korean sentence: {korean_text}
Conversation context/tone: {tone}

Return a JSON object with exactly these keys and types:
{{
  "natural_translation": "English translation of the input",
  "natural_korean": "한국어 의미",
  "polite_spoken": "A polite, natural English version",
  "polite_korean": "정중한 표현의 한국어 의미",
  "written": "A natural written English version",
  "written_korean": "문어체의 한국어 의미",
  "pattern": "Reusable English sentence pattern",
  "pattern_meaning": "한국어 패턴 의미",
  "pattern_explanation": "한국어 설명",
  "grammar_notes": ["한국어 문법 설명"],
  "examples": [
    {{"english": "A natural English example", "korean": "한국어 뜻"}},
    {{"english": "A natural English example", "korean": "한국어 뜻"}},
    {{"english": "A natural English example", "korean": "한국어 뜻"}},
    {{"english": "A natural English example", "korean": "한국어 뜻"}},
    {{"english": "A natural English example", "korean": "한국어 뜻"}}
  ]
}}
"""


def contains_hangul(value) -> bool:
    """영어 전용 필드에 한글이 섞였는지 검사."""
    return bool(re.search(r"[가-힣ㄱ-ㅎㅏ-ㅣ]", str(value or "")))


def normalize_result(data: dict):
    """필드 누락을 기본값으로 채우되, 영어 오류를 임의의 예문으로 숨기지 않는다."""
    if not isinstance(data, dict):
        raise ValueError("API 응답이 JSON 객체가 아닙니다.")
    defaults = {
        "natural_translation": "", "natural_korean": "",
        "polite_spoken": "", "polite_korean": "",
        "written": "", "written_korean": "", "pattern": "",
        "pattern_meaning": "", "pattern_explanation": "",
        "grammar_notes": [], "examples": [],
    }
    for key, value in defaults.items():
        if key not in data or data[key] is None:
            data[key] = value

    if not isinstance(data["examples"], list):
        raise ValueError("examples 항목이 배열 형식이 아닙니다.")
    normalized_examples = []
    for ex in data["examples"]:
        if not isinstance(ex, dict):
            raise ValueError("예문 항목의 형식이 올바르지 않습니다.")
        normalized_examples.append({
            "english": str(ex.get("english") or ex.get("en") or "").strip(),
            "korean": str(ex.get("korean") or ex.get("kr") or "").strip(),
        })
    data["examples"] = normalized_examples
    if not isinstance(data["grammar_notes"], list):
        data["grammar_notes"] = [str(data["grammar_notes"])] if data["grammar_notes"] else []
    return data


def validate_english_result(data: dict):
    """필수 영어 필드에 한글이 없고 실제 영문자가 있는지 확인."""
    required_fields = ("natural_translation", "polite_spoken", "written", "pattern")
    for field in required_fields:
        value = str(data.get(field, "")).strip()
        if not value or contains_hangul(value) or not re.search(r"[A-Za-z]", value):
            raise ValueError(f"'{field}' 항목이 정상적인 영어가 아닙니다.")

    examples = data.get("examples", [])
    if len(examples) != 5:
        raise ValueError("유사 예문은 정확히 5개여야 합니다.")
    for index, example in enumerate(examples, start=1):
        english = str(example.get("english", "")).strip()
        if not english or contains_hangul(english) or not re.search(r"[A-Za-z]", english):
            raise ValueError(f"{index}번 예문이 정상적인 영어가 아닙니다.")
    return data


def generate_english(korean_text: str, tone: str):
    cache_key = make_cache_key(korean_text, tone)
    cached = get_cached_result(cache_key)
    if cached is not None:
        try:
            return validate_english_result(normalize_result(cached)), True, 0.0
        except (ValueError, TypeError, AttributeError):
            # 잘못된 캐시는 사용하지 않고 새 API 결과로 교체한다.
            pass

    api_key = get_api_key()
    if not api_key:
        raise ValueError("OpenAI API 키가 설정되지 않았습니다. Streamlit Secrets 또는 .env를 확인하세요.")

    if monthly_usage()["cost"] >= MONTHLY_API_BUDGET_USD:
        raise RuntimeError("이번 달 설정 예산을 모두 사용했습니다.")

    client = OpenAI(api_key=api_key, max_retries=0, timeout=45.0)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(korean_text, tone)},
    ]
    last_validation_error = None
    total_cost = 0.0

    # 최초 1회 + 검증 실패 시 최대 2회 재생성
    for attempt_no in range(3):
        if count_api_calls_today() >= DAILY_API_LIMIT:
            raise RuntimeError(f"오늘 API 호출 한도({DAILY_API_LIMIT}회)에 도달했습니다.")
        if monthly_usage()["cost"] >= MONTHLY_API_BUDGET_USD:
            raise RuntimeError("재생성 도중 이번 달 설정 예산에 도달했습니다.")

        usage_id = start_api_attempt()
        usage_recorded = False
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                temperature=0.2,
                max_tokens=3500,
                response_format={"type": "json_object"},
                messages=messages,
            )
            content = response.choices[0].message.content or ""
            usage = response.usage
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            attempt_cost = finish_api_attempt(usage_id, "received", prompt_tokens, completion_tokens)
            usage_recorded = True
            total_cost += attempt_cost

            try:
                result = normalize_result(json.loads(content))
                validate_english_result(result)
            except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as exc:
                last_validation_error = str(exc)
                with get_connection() as conn:
                    conn.execute("UPDATE api_usage SET status = 'invalid_output' WHERE id = ?", (usage_id,))
                    conn.commit()
                if attempt_no >= 2:
                    raise RuntimeError(
                        "AI가 3회 연속으로 올바른 영어 결과를 반환하지 않았습니다. "
                        f"마지막 검증 오류: {last_validation_error}"
                    ) from exc
                # 직전 응답을 대화에 포함하고, 오류를 명시해 다시 생성하도록 요청
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous JSON did not pass validation: " + last_validation_error +
                        ". Regenerate the entire JSON object. Ensure every English-only field "
                        "contains natural English with absolutely no Korean characters. "
                        "Return valid JSON only, with exactly five examples."
                    ),
                })
                continue

            with get_connection() as conn:
                conn.execute("UPDATE api_usage SET status = 'success' WHERE id = ?", (usage_id,))
                conn.commit()
            save_study(korean_text, result, cache_key)
            return result, False, total_cost

        except RateLimitError as exc:
            if not usage_recorded:
                finish_api_attempt(usage_id, "rate_limited")
            error_code = getattr(exc, "code", None) or getattr(getattr(exc, "body", None), "get", lambda *_: None)("code")
            if error_code in ("insufficient_quota", "billing_hard_limit_reached") or attempt_no >= 2:
                raise
            time.sleep(2 ** attempt_no)
        except (APITimeoutError, APIConnectionError):
            if not usage_recorded:
                finish_api_attempt(usage_id, "connection_uncertain")
            raise RuntimeError("OpenAI 서버 연결 또는 응답 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.")
        except APIStatusError:
            if not usage_recorded:
                finish_api_attempt(usage_id, "api_error")
            raise
        except RuntimeError:
            raise
        except Exception:
            if not usage_recorded:
                finish_api_attempt(usage_id, "failed")
            raise

    raise RuntimeError("영어 문장 생성에 실패했습니다. " + (last_validation_error or "응답을 확인할 수 없습니다."))


# ---------------------------------------------------------
# 화면 유틸리티 (단어 클릭 관련 기능 주석 처리)
# ---------------------------------------------------------
# def clean_word(token: str):
#     return re.sub(r"^[^A-Za-z']+|[^A-Za-z']+$", "", token).lower()


# def render_clickable_sentence(sentence: str, glossary: list, key_prefix: str):
#     tokens = re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)?", sentence)
#     words = [t for t in tokens if clean_word(t)]
#     if not words:
#         st.write(sentence)
#         return

#     st.caption("영어 문장의 단어를 클릭하면 오른쪽에 뜻과 사용법이 표시됩니다.")
#     cols = st.columns(min(len(tokens), 8))
#     for i, token in enumerate(tokens):
#         if not clean_word(token):
#             continue
#         col = cols[i % len(cols)]
#         label = token
#         if col.button(label, key=f"{key_prefix}_{i}_{clean_word(token)}", use_container_width=True):
#             st.session_state.selected_word = clean_word(token)
#             st.session_state.selected_word_source = sentence


# def render_word_detail(result: dict):
#     selected = st.session_state.get("selected_word", "")
#     if not selected:
#         st.info("왼쪽 영어 문장에서 단어를 클릭해 보세요.")
#         return

#     glossary = result.get("word_glossary", [])
#     match = None
#     for item in glossary:
#         if clean_word(str(item.get("word", ""))) == selected:
#             match = item
#             break

#     st.subheader(f"📖 {selected}")
#     if match:
#         st.markdown(f"**기본 뜻:** {match.get('meaning', '설명 없음')}")
#         st.markdown(f"**품사:** {match.get('part_of_speech', '정보 없음')}")
#         st.markdown(f"**발음기호:** `{match.get('pronunciation', '정보 없음')}`")
#         st.markdown("**현재 문장에서의 의미**")
#         st.write(match.get("context_meaning", "설명 없음"))
#         if match.get("example"):
#             st.markdown("**추가 예문**")
#             st.write(match["example"])
#             st.caption(match.get("example_korean", ""))
#         collocations = match.get("collocations", [])
#         if collocations:
#             st.markdown("**자주 함께 쓰는 표현**")
#             for phrase in collocations:
#                 st.write(f"• {phrase}")
#     else:
#         st.warning("이 단어의 상세 설명이 결과에 포함되지 않았습니다.")


# ---------------------------------------------------------
# 사이드바
# ---------------------------------------------------------
with st.sidebar:
    st.title("🗣️ Natural English Lab")
    st.caption("한국어로 생각하고, 자연스러운 영어로 말하기")
    page = st.radio(
        "메뉴",
        ["문장 학습", "나의 학습장", "API 사용량", "사용 안내"],
        label_visibility="collapsed",
    )
    st.divider()
    st.caption("현재 버전: 영어 변환 안정화 v4")

# ---------------------------------------------------------
# 페이지 1: 문장 학습
# ---------------------------------------------------------
if page == "문장 학습":
    st.title("한국어 → 자연스러운 영어")
    st.write("영어로 말하고 싶은 내용을 한국어로 입력해 보세요.")

    with st.form("translation_form"):
        korean_input = st.text_area(
            "한국어 문장",
            placeholder="예: 오늘은 너무 피곤해서 집에서 쉬고 싶어.",
            height=110,
        )
        tone = st.selectbox(
            "원하는 대화 상황",
            [
                "일반적인 일상 회화",
                "친구나 가족과 편하게 대화",
                "직장 동료와 대화",
                "상사·거래처 등에게 정중하게 대화",
                "특정 상황 없이 가장 자연스러운 표현",
            ],
        )
        submitted = st.form_submit_button(
            "✨ 자연스러운 영어 문장 만들기",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        st.session_state.pop("current_result", None)
        st.session_state.pop("current_korean", None)
        st.session_state.pop("current_tone", None)
        if not korean_input.strip():
            st.warning("한국어 문장을 먼저 입력해 주세요.")
        else:
            with st.spinner("자연스러운 영어 표현과 유사 예문을 만들고 있습니다..."):
                try:
                    result, from_cache, generation_cost = generate_english(korean_input.strip(), tone)
                    st.session_state.current_result = result
                    st.session_state.current_korean = korean_input.strip()
                    st.session_state.current_tone = tone
                    if from_cache:
                        st.success("동일한 문장과 말투의 기존 결과를 불러왔습니다.")
                    else:
                        st.success(f"학습 결과를 자동 저장했습니다. 추정 비용: ${generation_cost:.6f}")
                except Exception as e:
                    st.error(f"문장 생성 중 오류가 발생했습니다: {e}")

    result = st.session_state.get("current_result")
    if result:
        try:
            result = validate_english_result(normalize_result(result))
        except (ValueError, TypeError, AttributeError) as validation_error:
            st.session_state.pop("current_result", None)
            result = None
            st.error(f"결과 검증에 실패했습니다: {validation_error}")
            
    if result:
        st.divider()
        st.subheader("1. 가장 자연스러운 일상 회화")
        st.caption("실제 대화에서 바로 사용할 수 있는 표현")
        
        # 기존 단어 클릭 레이아웃 대신 전체 너비로 회화 문장 표시
        st.markdown(
            f"<div style='font-size:1.4rem;font-weight:650;color:#1E88E5;line-height:1.6;margin-bottom:8px;'>"
            f"{result.get('natural_translation','')}</div>",
            unsafe_allow_html=True,
        )
        st.write(result.get("natural_korean", ""))
        
        # [주석 처리된 단어 클릭 기능 부분]
        # render_clickable_sentence(
        #     result.get("natural_translation", ""),
        #     result.get("word_glossary", []),
        #     "main_word",
        # )

        st.markdown("---")
        st.markdown("**핵심 문장 패턴**")
        st.code(result.get("pattern", ""), language="text")
        st.write(result.get("pattern_meaning", ""))
        st.info(result.get("pattern_explanation", ""))

        st.divider()
        st.subheader("2. 유사 패턴 예문 5개")
        st.caption("같은 문장 구조를 다른 상황에 적용해 보세요.")
        
        for idx, example in enumerate(result.get("examples", []), start=1):
            # 데이터가 딕셔너리 형태일 때 영어와 한국어 안전하게 분리 추출
            if isinstance(example, dict):
                eng_text = example.get('english') or example.get('en') or ""
                kor_text = example.get('korean') or example.get('kr') or ""
            else:
                eng_text = str(example)
                kor_text = ""

            # 만약 영어 자리에 실수로 한글이 들어갔다면 임시 영어로 교체
            if not eng_text or re.search(r"[가-힣ㄱ-ㅎㅏ-ㅣ]", eng_text):
                eng_text = f"Example sentence {idx} (English translation needed)"

            with st.container(border=True):
                # 굵은 글씨: 영어 문장
                st.markdown(f"**{idx}. {eng_text}**")
                # 아래 작은 글씨: 한국어 뜻
                st.write(kor_text if kor_text else "한국어 뜻")

        st.divider()
        st.subheader("3. 상황별 표현 비교")
        c1, c2 = st.columns(2, gap="large")
        with c1:
            with st.container(border=True):
                st.markdown("### 정중한 구어체")
                st.markdown(f"**{result.get('polite_spoken', '')}**")
                st.write(result.get("polite_korean", ""))
        with c2:
            with st.container(border=True):
                st.markdown("### 문어체")
                st.markdown(f"**{result.get('written', '')}**")
                st.write(result.get("written_korean", ""))

        st.divider()
        st.subheader("4. 문법 및 표현 학습")
        for note in result.get("grammar_notes", []):
            st.markdown(f"- {note}")

# ---------------------------------------------------------
# 페이지 2: 학습장
# ---------------------------------------------------------
elif page == "나의 학습장":
    st.title("📚 나의 학습장")
    st.write("저장한 영어 문장과 예문을 다시 확인할 수 있습니다.")
    rows = load_history()

    if not rows:
        st.info("아직 저장된 학습 내용이 없습니다.")
    else:
        st.caption(f"저장된 학습 기록: {len(rows)}개")
        for row in rows:
            with st.expander(f"{row['created_at']}  |  {row['korean_input'][:70]}"):
                try:
                    data = json.loads(row["result_json"])
                except json.JSONDecodeError:
                    st.error("저장된 학습 데이터를 읽을 수 없습니다.")
                    continue

                st.markdown(f"**자연스러운 영어**")
                st.write(data.get("natural_translation", ""))
                st.caption(data.get("natural_korean", ""))
                st.markdown("**유사 예문**")
                for i, ex in enumerate(data.get("examples", []), 1):
                    st.write(f"{i}. {ex.get('english', '')}")
                    st.caption(ex.get("korean", ""))
                st.markdown("**정중한 구어체**")
                st.write(data.get("polite_spoken", ""))
                st.markdown("**문어체**")
                st.write(data.get("written", ""))

                if st.button("이 기록 삭제", key=f"delete_{row['id']}"):
                    delete_history(row["id"])
                    st.success("삭제했습니다.")
                    st.rerun()

# ---------------------------------------------------------
# 페이지 3: API 사용량
# ---------------------------------------------------------
elif page == "API 사용량":
    st.title("📊 API 사용량 및 비용 관리")
    usage = monthly_usage()
    remaining = max(0.0, MONTHLY_API_BUDGET_USD - usage["cost"])
    daily_calls = count_api_calls_today()
    a, b, c, d = st.columns(4)
    a.metric("오늘 API 요청", f"{daily_calls} / {DAILY_API_LIMIT}")
    b.metric("이번 달 호출", f"{usage['calls']:,}회")
    c.metric("이번 달 추정 사용액", f"${usage['cost']:.4f}")
    d.metric("설정 예산 잔액", f"${remaining:.4f}")
    st.progress(min(1.0, usage["cost"] / MONTHLY_API_BUDGET_USD))

# ---------------------------------------------------------
# 페이지 4: 사용 안내
# ---------------------------------------------------------
else:
    st.title("사용 안내")
    st.markdown("한국어 문장을 입력하면 자연스러운 영어 회화 표현과 유사 예문 5개를 생성합니다.")
