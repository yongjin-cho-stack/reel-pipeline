# -*- coding: utf-8 -*-
"""reel_pipeline.py --mock 용 가짜 의존성. 실제 API 호출 없이 흐름/로직만 검증한다."""
import itertools


def build_mock_deps(script_fail_times=0, video1st_fail_times=0, extend_fail_times=0):
    """script_fail_times 등을 조절해서 재시도 경로도 시뮬레이션할 수 있게 만듦."""

    state = {"script_calls": 0, "video1st_calls": 0, "extend_calls_per_segment": {}}

    def get_character(brand_id):
        return {"brand_id": brand_id, "image_url": f"https://fake/{brand_id}.png", "example_script": "예전에 승인된 대본"}

    def create_character(brand_description, edit_category, edit_instruction, base_image_url):
        return {"brand_id": "brand_mock123", "image_url": "https://fake/brand_mock123.png", "example_script": None}

    def call_llm(prompt):
        # 대본 생성 프롬프트인지 영상프롬프트 생성인지 구분
        if "video_prompts" in prompt:
            # 구간 개수를 프롬프트에서 역산해서 정확히 그만큼 돌려줌
            import re
            m = re.search(r"구간 개수는 정확히 (\d+)개", prompt)
            n = int(m.group(1)) if m else 1
            import json
            return json.dumps({"video_prompts": [f"scene {i} description" for i in range(n)]})
        else:
            state["script_calls"] += 1
            # 프롬프트 안의 "총 글자수는 N자 이내로" 지시를 역산해서, 실제 LLM처럼 그 길이에 맞춰 씀
            import re
            m = re.search(r"총 글자수는 (\d+)자 이내로", prompt)
            max_chars = int(m.group(1)) if m else 40
            if state["script_calls"] <= script_fail_times:
                # 일부러 필수 키워드를 빼서 검증 실패를 유도
                return ("그냥 아무 문장 " * 10)[:max_chars]
            base = "이 가게 신메뉴 정말 맛있어요 꼭 한번 드셔보시고 후기도 남겨주세요 기대하셔도 좋습니다 "
            return (base * 5)[:max(1, max_chars - 2)]

    def generate_script(topic, target_length_sec, must_include_keywords, example_script, script_feedback, call_llm_fn):
        from reel_pipeline import build_script_prompt, verify_script
        attempt = 0
        fail_reason = ""
        while True:
            prompt, max_chars = build_script_prompt(topic, target_length_sec, must_include_keywords, example_script, script_feedback, attempt, fail_reason)
            narration = call_llm_fn(prompt)
            passed, fail_reason = verify_script(narration, must_include_keywords, target_length_sec)
            attempt += 1
            print(f"  [mock] script attempt {attempt}: passed={passed} reason={fail_reason!r}")
            if passed:
                return narration
            if attempt >= 3:
                raise RuntimeError(f"대본 검증 3회 실패: {fail_reason}")

    def save_example_script(brand_id, narration):
        pass

    def generate_narration_audio(segments, brand_id, call_tts_fn):
        for i, seg in enumerate(segments):
            seg["audio_url"] = f"https://fake-audio/{brand_id}_{i}.mp3"
        return segments

    def generate_video_prompts(segments, total_segments, call_llm_fn):
        from reel_pipeline import generate_video_prompts as real_gvp
        return real_gvp(segments, total_segments, call_llm_fn)

    def kling_submit_1st(image_url, prompt, negative_prompt):
        state["video1st_calls"] += 1
        return f"task1st_{state['video1st_calls']}"

    def kling_poll_1st(task_id):
        call_n = state["video1st_calls"]
        print(f"  [mock] 1st poll call#{call_n} task={task_id}")
        if call_n <= video1st_fail_times:
            raise RuntimeError("mock: 1차 생성 실패 시뮬레이션")
        return "https://fake-video/1st.mp4", f"videoId_1st_{call_n}"

    def kling_submit_extend(video_id, prompt, negative_prompt):
        n = state["extend_calls_per_segment"].get(video_id, 0) + 1
        state["extend_calls_per_segment"][video_id] = n
        return f"ext_task_{video_id}_{n}"

    def kling_poll_extend(task_id):
        print(f"  [mock] extend poll task={task_id}")
        # task_id 형태: ext_task_<video_id>_<n>
        parts = task_id.rsplit("_", 2)
        video_id_in, n = parts[1], int(parts[2])
        if n <= extend_fail_times:
            raise RuntimeError("mock: extend 실패 시뮬레이션")
        return f"https://fake-video/{task_id}.mp4", f"videoId_{task_id}"

    def render_video(image_url, segments, want, avoid, submit_1st, poll_1st, submit_extend, poll_extend):
        from reel_pipeline import render_video as real_render
        return real_render(image_url, segments, want, avoid, submit_1st, poll_1st, submit_extend, poll_extend)

    def merge_audio(video_url, segments):
        total_audio_dur = sum(s["target_duration_s"] for s in segments)
        print(f"  [mock] merge_audio: video={video_url}, segments={len(segments)}, sum_target_dur={total_audio_dur}")
        return b"FAKE_MP4_BYTES"

    def upload_final_video(video_bytes, brand_id):
        return f"https://fake/{brand_id}_final.mp4"

    return {
        "get_character": get_character,
        "create_character": create_character,
        "generate_script": generate_script,
        "save_example_script": save_example_script,
        "generate_narration_audio": generate_narration_audio,
        "generate_video_prompts": generate_video_prompts,
        "render_video": render_video,
        "merge_audio": merge_audio,
        "upload_final_video": upload_final_video,
        "call_llm": call_llm,
        "call_tts": None,
        "kling_submit_1st": kling_submit_1st,
        "kling_poll_1st": kling_poll_1st,
        "kling_submit_extend": kling_submit_extend,
        "kling_poll_extend": kling_poll_extend,
    }
