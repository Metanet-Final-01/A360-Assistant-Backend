# -*- coding: utf-8 -*-
"""compose의 두 미확인 전제를 API 호출 2회로 닫는다 (RPA-298 항목 B).

`_compose_candidate`는 두 가지를 **모른 채** 보수적으로 굴러간다:

  1. `response_format={"type":"json_object"}`와 `tools`를 **같이** 걸 수 있는가?
     못 걸면 첫 호출이 400이라, 프로브 없이 켜면 모든 후보 생성이 죽는다.
     그래서 `graph._JSON_MODE_WITH_TOOLS = False`로 두고, 탐색 턴에는 JSON mode를 안 건다.
  2. 이 모델의 실제 출력 상한은 얼마인가?
     실측 파싱 실패는 3,604 출력 토큰 지점이었고 `max_completion_tokens`는 걸려 있지 않다.
     상한이 훨씬 위라면 그 실패는 **잘림이 아니라 문법 오류**이고, 처방이 달라진다.

⚠️ **실제 OpenAI API를 호출한다**(토큰 요금 발생, 2콜 · 수백 토큰). 로컬 .env의
`OPENAI_API_KEY`·`OPENAI_MODEL`을 쓴다.

    python -m scripts.agent_v4.probe_compose_json_mode

결과에 따라 할 일:
  - 1이 OK  → `graph._JSON_MODE_WITH_TOOLS = True`로 바꾼다(탐색 턴도 JSON 보장).
  - 1이 400 → False 그대로 두고, 이 스크립트 출력의 오류 문구를 그 상수 주석에 남긴다.
  - 2의 상한이 실측 실패 지점(≈3.6k)의 몇 배인지 보고, 배수가 크면 '잘림' 가설을 접는다.
"""

import os
import sys


def _client():
    from openai import OpenAI

    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        sys.exit("OPENAI_API_KEY가 없습니다 — .env를 읽는 환경에서 실행하세요.")
    return OpenAI(api_key=key)


_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_action_spec",
        "description": "액션 스펙을 조회한다",
        "parameters": {
            "type": "object",
            "properties": {"package": {"type": "string"}, "action": {"type": "string"}},
            "required": ["package", "action"],
        },
    },
}


def probe_json_mode_with_tools(client, model: str) -> None:
    """전제 1 — tools + response_format 병용 가부."""
    print("\n[1] tools + response_format={'type':'json_object'} 병용")
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "너는 JSON만 출력한다."},
                {"role": "user", "content": '{"ok": true} 라는 json 객체 하나만 출력하라.'},
            ],
            tools=[_TOOL],
            response_format={"type": "json_object"},
        )
        print("    ✅ 병용 가능 —", repr((r.choices[0].message.content or "")[:80]))
        print("    → graph._JSON_MODE_WITH_TOOLS = True 로 바꿔도 된다.")
    except Exception as e:  # noqa: BLE001 — 프로브의 목적이 예외를 보는 것이다
        print(f"    ❌ 거부됨 ({type(e).__name__}): {str(e)[:300]}")
        print("    → _JSON_MODE_WITH_TOOLS = False 유지. 이 문구를 그 상수 주석에 남길 것.")


def probe_output_ceiling(client, model: str) -> None:
    """전제 2 — 출력 상한. 넘는 값을 요청하면 API가 실제 상한을 오류에 담아 준다."""
    print("\n[2] 출력 상한(max_completion_tokens)")
    absurd = 1_000_000
    try:
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "안녕"}],
            max_completion_tokens=absurd,
        )
        print(f"    ⚠️ {absurd:,} 요청이 거부되지 않았다 — 상한을 이 방법으로는 못 읽는다.")
    except Exception as e:  # noqa: BLE001
        print(f"    ℹ️ {type(e).__name__}: {str(e)[:300]}")
        print("    → 위 문구의 숫자가 실제 상한이다. 실측 실패 지점(≈3,604 출력 토큰)과 비교할 것:")
        print("       상한이 훨씬 크면 그 실패는 잘림이 아니라 **문법 오류**다.")


def main() -> None:
    model = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
    print(f"모델: {model}")
    client = _client()
    probe_json_mode_with_tools(client, model)
    probe_output_ceiling(client, model)


if __name__ == "__main__":
    main()
