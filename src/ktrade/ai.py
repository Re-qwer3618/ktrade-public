"""AI 도우미 — 사용자 본인 API 키로 Claude · Gemini · ChatGPT를 부른다 (AI판 구독 기능, 지금은 관리자만: access.can_ai).

  · 백테스트 분석: 실험 결과(지표·거래 요약)와 매매조건 코드를 보내 해석·약점·다음 실험을 받는다
  · 매매조건 작성: docs/LLM_CONTEXT.md(작성 규격)를 시스템 프롬프트로, 요청 양식을 보내 strategy.yaml·strategy.py를 받는다

키는 credentials(Windows 자격 증명 관리자)에 두고, 요청은 그 회사 API로만 직접 간다(ktrade 서버 없음).
Claude는 공식 SDK(anthropic — 대시보드 설치에 포함), Gemini·ChatGPT는 표준 라이브러리 HTTP로 부른다.
모델 이름은 config ai.models에서 바꾼다(새 모델이 나오면 이름만 바꾸면 됨).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from ktrade import credentials

PROVIDERS = {"claude": "Claude (Anthropic)", "gemini": "Gemini (Google)", "openai": "ChatGPT (OpenAI)"}
KEY_NAME = {"claude": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}
KEY_HELP = {"claude": "console.anthropic.com → API Keys", "gemini": "aistudio.google.com → Get API key",
            "openai": "platform.openai.com → API keys"}
TIMEOUT = 300


class AiError(RuntimeError):
    pass


def settings(cfg: dict) -> dict:
    from ktrade.config import DEFAULTS
    a = {**DEFAULTS["ai"], **(cfg.get("ai") or {})}
    a["models"] = {**DEFAULTS["ai"]["models"], **(a.get("models") or {})}
    return a


def has_key(provider: str) -> bool:
    return bool(credentials.get(KEY_NAME[provider]))


def ask(cfg: dict, system: str, prompt: str, provider: str | None = None, max_tokens: int = 16000) -> str:
    """한 번 묻고 답 텍스트. 키가 없거나 거절·오류면 AiError."""
    s = settings(cfg)
    provider = provider or s["provider"]
    key = credentials.get(KEY_NAME[provider])
    if not key:
        raise AiError(f"{PROVIDERS[provider]} API 키가 없습니다 — AI 도우미 설정에서 넣으세요 ({KEY_HELP[provider]})")
    return {"claude": _claude, "gemini": _gemini, "openai": _openai}[provider](key, s["models"][provider], system, prompt,
                                                                              max_tokens)


def _claude(key: str, model: str, system: str, prompt: str, max_tokens: int) -> str:
    try:
        import anthropic
    except ImportError as exc:
        raise AiError("anthropic 패키지가 없습니다 — ktrade를 다시 설치하세요 (대시보드 설치에 포함)") from exc
    client = anthropic.Anthropic(api_key=key, timeout=TIMEOUT)
    try:
        # 안전 분류기가 거절하면 서버가 다른 모델로 이어 답하게(fallbacks) — 그래도 거절이면 refusal
        # 긴 시스템 프롬프트(작성 규격·개인 스킬)는 캐시 — 같은 스킬로 여러 종목을 물을 때 싸고 빠르게
        r = client.beta.messages.create(model=model, max_tokens=max_tokens,
                                        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                                        messages=[{"role": "user", "content": prompt}],
                                        thinking={"type": "adaptive"}, output_config={"effort": "high"},
                                        betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    except anthropic.AuthenticationError as exc:
        raise AiError("Claude API 키가 맞지 않습니다") from exc
    except anthropic.RateLimitError as exc:
        raise AiError("Claude 요청 한도 초과 — 잠시 뒤 다시") from exc
    except anthropic.APIStatusError as exc:
        raise AiError(f"Claude 오류 {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AiError("Claude에 연결하지 못했습니다 (인터넷 확인)") from exc
    if r.stop_reason == "refusal":
        raise AiError("Claude가 이 요청에 답하지 않았습니다 (안전 정책)")
    text = "".join(b.text for b in r.content if b.type == "text")
    return text + ("\n\n(답이 길어 중간에 끊겼습니다)" if r.stop_reason == "max_tokens" else "")


def _post(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AiError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}") from exc
    except urllib.error.URLError as exc:
        raise AiError(f"연결 실패: {exc.reason}") from exc


def _gemini(key: str, model: str, system: str, prompt: str, max_tokens: int) -> str:
    d = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
              {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": {"maxOutputTokens": max_tokens}}, {"x-goog-api-key": key})
    try:
        return "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError) as exc:
        raise AiError(f"Gemini 응답에 답이 없습니다: {str(d)[:300]}") from exc


def _openai(key: str, model: str, system: str, prompt: str, max_tokens: int) -> str:
    d = _post("https://api.openai.com/v1/chat/completions",
              {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
               "max_completion_tokens": max_tokens}, {"Authorization": f"Bearer {key}"})
    try:
        return d["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError) as exc:
        raise AiError(f"ChatGPT 응답에 답이 없습니다: {str(d)[:300]}") from exc


# ───────── 쓰임새 ─────────
ANALYSIS_SYSTEM = ("너는 한국 주식 매매조건의 백테스트 결과를 읽어 주는 분석가다. 한국어로, 숫자 근거를 들어 짧고 분명하게 쓴다. "
                   "수익을 보장하거나 특정 종목 매수·매도를 권하지 않는다. 표본이 적으면 그렇다고 말한다. 순서: "
                   "1) 한 줄 요약 2) 강점 3) 약점·위험(과최적화, 표본 수, 앞뒤 기간 차이, 시장 대비) "
                   "4) 다음에 해 볼 실험 3가지(파라미터·청산 설정을 구체적인 값으로).")


def analysis_prompt(info, result: dict, trades=None, code: str = "") -> str:
    keys = ("trades", "win_rate", "win_ci", "avg_ret", "median_ret", "profit_factor", "avg_excess", "max_dd", "start", "end",
            "universe", "exits", "is", "oos")
    parts = [f"매매조건: {info.id} {info.version} — {info.description or ''}", "백테스트 결과(JSON):",
             json.dumps({k: result.get(k) for k in keys if result.get(k) is not None}, ensure_ascii=False, default=str)]
    if trades is not None and len(trades):
        parts += ["거래 목록 요약:", f"건수 {len(trades)}, 청산 사유별 {trades['reason'].value_counts().to_dict()}, "
                  f"보유일 평균 {trades['days'].mean():.1f}, 수익률 분위수(10/50/90%) "
                  f"{trades['ret_pct'].quantile([.1, .5, .9]).round(2).tolist()}"]
    if code:
        parts += ["매매조건 코드(strategy.py):", code[:12000]]
    return "\n".join(parts)


def writer_system(docs_dir: Path | None) -> str:
    spec = (docs_dir / "LLM_CONTEXT.md").read_text(encoding="utf-8") if docs_dir and (docs_dir / "LLM_CONTEXT.md").exists() else ""
    return ("아래 ktrade 매매조건 작성 규격을 정확히 따른다. 답에는 ```yaml 블록(strategy.yaml) 하나와 ```python 블록(strategy.py) "
            "하나를 반드시 넣고, 그 밖의 설명은 짧게 한국어로.\n\n" + spec)


SEARCH_SYSTEM = """너는 한국 주식 종목 검색 조건을 만드는 도우미다. 사용자의 자연어 요청을 아래 JSON 형식 하나로만 바꾼다.
```json 블록 하나만 답한다. 형식에 없는 조건(재무·뉴스·테마 등)은 넣지 말고 summary에 '반영 못 함: …'으로 적는다.

{
  "markets": ["코스피", "코스닥"],                  // 비우면 전체
  "exclude_kinds": true,                            // ETF·ETN·스팩·리츠·우선주·관리종목·거래정지 제외
  "price": {"min": null, "max": null},              // 최근 종가(원)
  "cap": {"min": null, "max": null},                // 시가총액(억 원)
  "rules": [                                        // 모두 만족(AND)
    {"type": "change", "days": 5, "min": 10, "max": null},          // N일 등락률 %
    {"type": "volume_ratio", "days": 20, "min": 2, "max": null},    // 오늘 거래량 ÷ 직전 N일 평균
    {"type": "value", "days": 20, "min": 50, "max": null},          // N일 평균 거래대금(억 원)
    {"type": "ma", "days": 20, "position": "above", "within_pct": null},   // 종가가 N일 이평 위(above)/아래(below), 거리 % 이내
    {"type": "high", "days": 60, "within_pct": 3},                  // N일 최고가 대비 x% 이내 (0 = 신고가)
    {"type": "low", "days": 60, "within_pct": 5},                   // N일 최저가 대비 x% 이내
    {"type": "ma_cross", "fast": 5, "slow": 20, "direction": "golden", "within_days": 3}   // 최근 N일 안 골든(dead)크로스
  ],
  "sort": {"by": "value", "days": 20, "desc": true},   // change | volume_ratio | value | cap
  "limit": 30,
  "summary": "적용한 조건을 한국어 한 줄로"
}"""

STOCK_SYSTEM = ("너는 한국 주식 차트를 읽는 분석가다. 주어진 일봉·거래량·지수 데이터만 근거로, 한국어로 분명하게 쓴다. "
                "매수·매도를 권하거나 수익을 보장하지 않는다. 데이터에 없는 사실(뉴스·실적)은 지어내지 않는다.")


def skill_text(cfg: dict, cap: int = 300_000) -> tuple[str, list[str]]:
    """개인 스킬 폴더(config ai.skill_dir)의 SKILL.md + references/*.md(바로 아래 것만) — (본문, 읽은 파일). 없으면 ("", [])."""
    d = settings(cfg).get("skill_dir")
    if not d:
        return "", []
    root = Path(str(d)).expanduser()
    files = [root / "SKILL.md"] + sorted((root / "references").glob("*.md")) if (root / "SKILL.md").exists() else []
    parts, used, size = [], [], 0
    for f in files:
        t = f.read_text(encoding="utf-8", errors="replace")
        if size + len(t) > cap:
            break
        parts.append(f"=== {f.relative_to(root).as_posix()} ===\n{t}")
        used.append(f.relative_to(root).as_posix())
        size += len(t)
    return "\n\n".join(parts), used


def stock_system(cfg: dict) -> str:
    skill, _ = skill_text(cfg)
    if not skill:
        return STOCK_SYSTEM
    return (STOCK_SYSTEM + "\n\n아래는 사용자의 개인 분석 스킬이다. 이 스킬의 방법·용어·출력 형식을 그대로 따라 분석한다. "
            "스킬이 요구하는 자료(호가창·분차트 등)가 없으면 '자료 없음'으로 표시하고 있는 자료로만 판단한다.\n\n" + skill)


def stock_prompt(symbol: str, name: str, bars, index=None, question: str = "") -> str:
    """일봉(최근 120개)과 지수를 표로 붙인 분석 요청."""
    cols = [c for c in ("date", "open", "high", "low", "close", "volume") if c in bars]
    parts = [f"종목: {name} ({symbol})", f"일봉 최근 {min(len(bars), 120)}개 (CSV):", bars[cols].tail(120).to_csv(index=False)]
    if index is not None and len(index):
        parts += ["코스피 지수 최근 60개 (CSV):", index[["date", "close"]].tail(60).to_csv(index=False)]
    parts.append(question or "이 종목을 분석해 줘.")
    return "\n".join(parts)


def parse_json(text: str) -> dict:
    """AI 답에서 JSON 하나 (```json 블록 또는 맨 바깥 { })."""
    m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    raw = re.sub(r"//[^\n]*", "", raw)                       # 주석을 따라 쓴 경우
    try:
        d = json.loads(raw)
    except ValueError as exc:
        raise AiError(f"검색 조건(JSON)을 읽지 못했습니다 — 다시 시도해 보세요: {exc}") from exc
    if not isinstance(d, dict):
        raise AiError("검색 조건 형식이 아닙니다")
    return d


def parse_files(text: str) -> tuple[str, str]:
    """AI 답에서 (strategy.yaml, strategy.py). 없으면 AiError."""
    y = re.search(r"```ya?ml\s*\n(.*?)```", text, re.S)
    py = re.search(r"```python\s*\n(.*?)```", text, re.S)
    if not y or not py:
        raise AiError("답에서 strategy.yaml·strategy.py 코드 블록을 찾지 못했습니다 — 다시 요청해 보세요")
    return y.group(1).strip() + "\n", py.group(1).strip() + "\n"
