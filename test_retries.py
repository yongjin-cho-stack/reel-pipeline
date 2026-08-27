# -*- coding: utf-8 -*-
"""재시도 경로들을 강제로 실패시켜서 로직이 맞는지 확인하는 테스트 스크립트."""
from mock_deps import build_mock_deps
from reel_pipeline import run_pipeline


def run(label, keywords="", **mock_kwargs):
    print(f"\n===== {label} =====")
    deps = build_mock_deps(**mock_kwargs)
    inputs = {
        "brand_id": "", "brand_description": "테스트 캐릭터",
        "topic": "재시도 테스트", "target_length_sec": 20,
        "must_include_keywords": keywords,
    }
    try:
        result = run_pipeline(inputs, deps)
        print(f"결과: 성공 -> {result['final_video_url']}")
    except RuntimeError as e:
        print(f"결과: 실패(의도된 실패 경로 확인용) -> {e}")


# 1. 대본 1번 실패 후 성공 (재시도 살아있는 경우) -- "신메뉴" 키워드가 fail 텍스트엔 없고 success 텍스트엔 있음
run("대본 1회 실패 -> 재시도 성공", keywords="신메뉴", script_fail_times=1)

# 2. 대본 3번 다 실패 -> 사람에게 넘어가는 경로
run("대본 3회 모두 실패 -> 실패 출력", keywords="신메뉴", script_fail_times=99)

# 3. 영상 1차 1번 실패 후 성공
run("영상 1차 1회 실패 -> 재시도 성공", video1st_fail_times=1)

# 4. 영상 1차 4번 다 실패 -> 사람에게
run("영상 1차 4회 모두 실패 -> 실패 출력", video1st_fail_times=99)

# 5. Extend 1번 실패 후 성공
run("Extend 1회 실패 -> 재시도 성공", extend_fail_times=1)

# 6. Extend 3번 다 실패 -> 사람에게
run("Extend 3회 모두 실패 -> 실패 출력", extend_fail_times=99)
