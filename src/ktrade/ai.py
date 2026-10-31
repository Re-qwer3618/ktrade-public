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
        r = client.beta.messages.create(model=model, max_tokens=max_tokens, system=system,
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


def parse_files(text: str) -> tuple[str, str]:
    """AI 답에서 (strategy.yaml, strategy.py). 없으면 AiError."""
    y = re.search(r"```ya?ml\s*\n(.*?)```", text, re.S)
    py = re.search(r"```python\s*\n(.*?)```", text, re.S)
    if not y or not py:
        raise AiError("답에서 strategy.yaml·strategy.py 코드 블록을 찾지 못했습니다 — 다시 요청해 보세요")
    return y.group(1).strip() + "\n", py.group(1).strip() + "\n"
