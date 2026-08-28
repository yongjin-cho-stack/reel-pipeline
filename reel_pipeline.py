# -*- coding: utf-8 -*-
"""
브랜드 캐릭터 숏츠 파이프라인 - 로컬 Python 버전
n8n reel-pipeline.json 이랑 로직은 동일, 디버깅 편하게 하려고 순수 Python으로 옮김.

사용법:
    python reel_pipeline.py                # 대화형으로 입력받아 실제 실행
    python reel_pipeline.py --mock          # API 호출 없이 가짜 응답으로 흐름(로직)만 검증

환경변수 (실제 실행 시 필요):
    OPENROUTER_KEY, SUPABASE_URL, SUPABASE_KEY, KLING_KEY, GOOGLE_TTS_KEY
"""
import os
import sys
import re
import json
import time
import math
import base64
import argparse
import requests

# ============================================================
# 설정
# ============================================================
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
KLING_KEY = os.environ.get("KLING_KEY", "")
GOOGLE_TTS_KEY = os.environ.get("GOOGLE_TTS_KEY", "")
FFMPEG_SERVER_URL = os.environ.get("FFMPEG_SERVER_URL", "http://localhost:8000")

KLING_HOST = "https://api-singapore.klingai.com"


def log(stage, msg):
    print(f"[{stage}] {msg}", flush=True)


def supabase_headers(content_type=None):
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    if content_type:
        h["Content-Type"] = content_type
    return h


def kling_headers():
    return {"Authorization": f"Bearer {KLING_KEY}"}


def _check(resp, label):
    """실패시 상태코드만이 아니라 실제 응답 본문(에러 이유)까지 메시지에 포함시킨다."""
    if resp.status_code >= 300:
        raise RuntimeError(f"{label} 실패 ({resp.status_code}): {resp.text[:500]}")


# ============================================================
# 1단계: 캐릭터 확보 (신규 생성 또는 기존 조회)
# ============================================================
def create_character(brand_description, edit_category=None, edit_instruction=None, base_image_url=None):
    log("character", "이미지 생성 요청 만드는 중...")
    if not (brand_description or "").strip() and not (edit_instruction and base_image_url):
        raise RuntimeError("브랜드 설명이 비어있어요 — 입력하고 다시 시도하세요 (fail loud: LLM한테 빈 설명을 보내지 않음)")
    if edit_instruction and base_image_url:
        content = [
            {"type": "text", "text": f"이 이미지에서 {edit_category or '기타'}만 다음과 같이 바꿔줘: {edit_instruction}. 나머지 요소는 최대한 그대로 유지해줘."},
            {"type": "image_url", "image_url": {"url": base_image_url}},
        ]
    else:
        content = (
            "다음 설명에 맞는 브랜드 캐릭터 마스코트 이미지를 생성해줘. "
            "정면을 바라보는 전신 포즈, 단색(흰색) 배경, 다른 요소 없이 캐릭터만. "
            f"설명: {brand_description}"
        )

    body = {
        "model": "google/gemini-3.1-flash-image-preview",
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": content}],
    }
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
        json=body, timeout=60,
    )
    _check(resp, "OpenRouter 이미지 생성")
    data = resp.json()
    msg = data["choices"][0]["message"]
    data_url = None
    if msg.get("images"):
        data_url = msg["images"][0]["image_url"]["url"]
    elif isinstance(msg.get("content"), str) and msg["content"].startswith("data:image"):
        data_url = msg["content"]
    if not data_url or not data_url.startswith("data:image"):
        raise RuntimeError(f"OpenRouter 응답에서 이미지를 못 찾음: {str(data)[:500]}")

    header, b64data = data_url.split(",", 1)
    mime = header.split(";")[0].replace("data:", "")
    ext = mime.split("/")[-1].replace("+xml", "")
    image_bytes = base64.b64decode(b64data)
    log("character", f"이미지 디코딩 완료 ({len(image_bytes)} bytes, {mime})")

    brand_id = f"brand_{int(time.time() * 1000)}"
    storage_path = f"{brand_id}.{ext}"
    log("character", "Supabase Storage 업로드 중...")
    up = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/brand-characters/{storage_path}",
        headers={**supabase_headers(), "Content-Type": mime, "x-upsert": "true"},
        data=image_bytes, timeout=60,
    )
    if up.status_code >= 300:
        raise RuntimeError(f"이미지 업로드 실패 ({up.status_code}): {up.text[:300]}")

    image_url = f"{SUPABASE_URL}/storage/v1/object/public/brand-characters/{storage_path}"

    log("character", "브랜드ID로 영구 저장 중...")
    save = requests.post(
        f"{SUPABASE_URL}/rest/v1/characters",
        headers={**supabase_headers("application/json"), "Prefer": "resolution=merge-duplicates"},
        json={
            "brand_id": brand_id, "description": brand_description,
            "image_url": image_url, "example_script": None,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        },
        timeout=30,
    )
    if save.status_code >= 300:
        raise RuntimeError(f"브랜드 저장 실패 ({save.status_code}): {save.text[:300]}")

    log("character", f"완료: brand_id={brand_id}")
    return {"brand_id": brand_id, "image_url": image_url, "example_script": None}


def get_character(brand_id):
    log("character", f"기존 브랜드 조회: {brand_id}")
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/characters",
        headers=supabase_headers(),
        params={"brand_id": f"eq.{brand_id}", "select": "*"},
        timeout=30,
    )
    _check(resp, "브랜드 조회")
    rows = resp.json()
    if not rows:
        raise RuntimeError(f"brand_id={brand_id} 에 해당하는 캐릭터를 못 찾았어요. 등록부터 다시 하세요.")
    row = rows[0]
    return {"brand_id": row["brand_id"], "image_url": row["image_url"], "example_script": row.get("example_script")}


# ============================================================
# 2단계: 반복 횟수 계산
# ============================================================
def calc_segments(target_length_sec):
    segments_needed_extend = max(0, math.ceil((target_length_sec - 10) / 4.5))
    total_segments = segments_needed_extend + 1
    durations = [10] + [5] * segments_needed_extend
    return total_segments, durations


# ============================================================
# 3단계: 대본 생성 + 검증 (재시도 최대 2바퀴 = 최대 3회 시도)
# ============================================================
def build_script_prompt(topic, target_length_sec, must_include_keywords, example_script, script_feedback, attempt, fail_reason, previous_narration=None):
    rate = 4
    max_chars = round(target_length_sec * rate)
    example = example_script or "(예시 없음, 자연스러운 홍보 톤으로 자유롭게)"
    extra = ""
    if attempt > 0 and fail_reason:
        extra += f"\n\n[이전 시도 문제점 — 반드시 고쳐서 다시 써줘]\n{fail_reason}"
    # attempt 번호와 상관없이 항상 반영 — 예전엔 attempt>0일 때만 넣어서 재시도 첫 시도에서
    # 사용자 피드백이 조용히 씹히는 버그가 있었음
    if script_feedback:
        if previous_narration:
            extra += (
                f"\n\n[방금 나온 대본 — 이 대본을 기준으로 아래 요청사항만 반영해서 수정해줘, "
                f"나머지 내용/톤은 최대한 유지]\n{previous_narration}\n\n[수정 요청]\n{script_feedback}"
            )
        else:
            extra += f"\n\n[사용자 수정 요청 — 톤/타깃/강조/금지어]\n{script_feedback}"

    kw_line = f"다음 키워드는 대본에 반드시 그대로 포함해라: {', '.join(must_include_keywords)}\n" if must_include_keywords else ""
    prompt = (
        "너는 브랜드 홍보 짧은 영상의 나레이션 대본을 쓰는 카피라이터다.\n"
        "없는 사실을 지어내지 마라.\n"
        f"{kw_line}"
        f"총 글자수는 {max_chars}자 이내로, 실제 사람이 말하듯 자연스러운 구어체 문장으로 써라.\n"
        "출력은 나레이션 문장만, 다른 설명·따옴표·마크다운 없이 순수 텍스트로만 출력해라.\n\n"
        f"[예시 대본 — 톤 참고용]\n{example}\n\n"
        f"[주제]\n{topic}{extra}"
    )
    return prompt, max_chars


def verify_script(narration, must_include_keywords, target_length_sec):
    rate = 4
    est_sec = len(narration) / rate
    diff_ratio = abs(est_sec - target_length_sec) / target_length_sec
    missing = [k for k in must_include_keywords if k not in narration]

    problems = []
    if missing:
        problems.append(f"필수 키워드({', '.join(missing)})가 대본에 안 들어감")
    if diff_ratio > 0.2:
        problems.append(f"예상 낭독 길이 {est_sec:.1f}초, 목표 {target_length_sec}초와 차이가 큼")
    return len(problems) == 0, " / ".join(problems)


def generate_script(topic, target_length_sec, must_include_keywords, example_script, script_feedback, call_llm, previous_narration=None):
    attempt = 0
    fail_reason = ""
    while True:
        prompt, max_chars = build_script_prompt(
            topic, target_length_sec, must_include_keywords, example_script, script_feedback, attempt, fail_reason, previous_narration
        )
        log("script", f"시도 {attempt + 1}회 — LLM 호출...")
        narration = call_llm(prompt)
        passed, fail_reason = verify_script(narration, must_include_keywords, target_length_sec)
        attempt += 1
        if passed:
            log("script", f"검증 통과 (시도 {attempt}회): {narration}")
            return narration
        log("script", f"검증 실패: {fail_reason}")
        if attempt >= 3:  # 최초 1회 + 재시도 2회
            raise RuntimeError(f"대본 검증 3회 실패 → 사람에게: {fail_reason}")


def call_llm_openrouter(prompt, model="openai/gpt-5"):
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
        json={"model": model, "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    _check(resp, "OpenRouter LLM 호출")
    return resp.json()["choices"][0]["message"]["content"].strip()


def save_example_script(brand_id, narration):
    log("script", "예시로 저장 중...")
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/characters",
        headers={**supabase_headers("application/json"), "Prefer": "resolution=merge-duplicates"},
        json={"brand_id": brand_id, "example_script": narration,
              "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())},
        timeout=30,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"예시 저장 실패 ({resp.status_code}): {resp.text[:300]}")


# ============================================================
# 4단계: 나레이션 구간 분리 (구간 목표 길이 비율대로 배분)
# ============================================================
def split_narration(narration, total_segments, durations):
    sentences = [s for s in re.split(r"(?<=[.!?。])\s+", narration) if s.strip()]
    if not sentences:
        sentences = [narration]
    sum_dur = sum(durations)
    total_chars = sum(len(s) for s in sentences) or 1

    segments = []
    si = 0
    for i in range(total_segments):
        budget = total_chars * (durations[i] / sum_dur)
        remaining = total_segments - i
        chunk = []
        used = 0
        while si < len(sentences):
            s = sentences[si]
            if remaining == 1:
                chunk.append(s)
                si += 1
                continue
            if used == 0 or used + len(s) <= budget * 1.3:
                chunk.append(s)
                used += len(s)
                si += 1
            else:
                break
        text = " ".join(chunk).strip() or (sentences[-1] if sentences else narration)
        segments.append({"index": i, "narration_text": text, "target_duration_s": durations[i]})
    return segments


# ============================================================
# 5단계: 나레이션 생성 (Google TTS) 구간별
# ============================================================
def generate_narration_audio(segments, brand_id, call_tts):
    for i, seg in enumerate(segments):
        log("tts", f"구간 {i} TTS 생성 중 ({len(seg['narration_text'])}자)...")
        audio_url = call_tts(seg["narration_text"], brand_id, i)
        seg["audio_url"] = audio_url
    return segments


def call_tts_google(text, brand_id, index):
    body = {
        "input": {"text": text},
        "voice": {"languageCode": "ko-KR", "name": "ko-KR-Neural2-A"},
        "audioConfig": {"audioEncoding": "MP3"},
    }
    resp = requests.post(
        "https://texttospeech.googleapis.com/v1/text:synthesize",
        headers={"X-goog-api-key": GOOGLE_TTS_KEY, "Content-Type": "application/json"},
        json=body, timeout=60,
    )
    _check(resp, "Google TTS 호출")
    audio_content = resp.json().get("audioContent")
    if not audio_content:
        raise RuntimeError(f"Google TTS 응답에 audioContent 없음: {str(resp.json())[:300]}")
    audio_bytes = base64.b64decode(audio_content)

    storage_path = f"{brand_id}_{index}.mp3"
    up = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/narration-audio/{storage_path}",
        headers={**supabase_headers(), "Content-Type": "audio/mpeg", "x-upsert": "true"},
        data=audio_bytes, timeout=60,
    )
    if up.status_code >= 300:
        raise RuntimeError(f"오디오 업로드 실패 ({up.status_code}): {up.text[:300]}")
    return f"{SUPABASE_URL}/storage/v1/object/public/narration-audio/{storage_path}"


# ============================================================
# 6단계: 구간별 영상 프롬프트 작성
# ============================================================
def generate_video_prompts(segments, total_segments, call_llm):
    seg_list = "\n".join(f"구간{s['index']}: {s['narration_text']}" for s in segments)
    prompt = (
        "아래는 짧은 홍보 영상의 나레이션을 시간 구간별로 나눈 것이다.\n"
        "각 구간마다, 그 나레이션이 나올 때 화면에 어떤 장면이 나와야 하는지 영상 생성용 장면 묘사를 만들어라.\n"
        "나레이션 문장을 그대로 옮기지 말고, 카메라/동작/표정 등 시각적 요소로 새로 써라 (영어로, 한 구간당 1문장).\n"
        "출력은 반드시 아래 JSON 형식 그대로만 출력해라 (설명 없이):\n"
        '{"video_prompts": ["구간0 장면 묘사", "구간1 장면 묘사", ...]}\n'
        f"구간 개수는 정확히 {total_segments}개여야 한다.\n\n"
        f"[나레이션 구간]\n{seg_list}"
    )
    log("video_prompt", "구간별 영상 프롬프트 생성 중...")
    raw = call_llm(prompt)
    raw = re.sub(r"^```json\s*|```$", "", raw.strip())
    try:
        parsed = json.loads(raw)
        prompts = parsed.get("video_prompts", [])
    except json.JSONDecodeError:
        prompts = []

    if len(prompts) != total_segments:
        log("video_prompt", f"경고: 개수 불일치 (받은 {len(prompts)}개, 필요 {total_segments}개) — 부족분은 나레이션 텍스트로 대체")

    for i, seg in enumerate(segments):
        seg["video_prompt"] = prompts[i] if i < len(prompts) else seg["narration_text"]
    return segments


# ============================================================
# 7단계: Kling 영상 생성 (1차 + Extend 반복)
# ============================================================
def kling_poll(task_id, get_url_fn, call_status):
    while True:
        time.sleep(2)  # 실전에서는 10초 정도로
        status, video_url, video_id = call_status(task_id)
        if "succeed" in (status or "").lower():
            return video_url, video_id
        if "fail" in (status or "").lower():
            raise RuntimeError(f"Kling 작업 실패 (task_id={task_id})")
        # 계속 진행 중이면 다시 대기


def render_video(image_url, segments, video_feedback_want, video_feedback_avoid, submit_1st, poll_1st, submit_extend, poll_extend):
    seg0 = segments[0]
    attempt = 0
    while True:
        prompt = seg0["video_prompt"]
        negative = "blurry, low quality, distorted, extra limbs, watermark"
        if attempt > 0 and video_feedback_want:
            prompt += f". {video_feedback_want}"
        if attempt > 0 and video_feedback_avoid:
            negative += f", {video_feedback_avoid}"
        log("video", f"1차 생성 시도 {attempt + 1}회...")
        try:
            task_id = submit_1st(image_url, prompt, negative)
            video_url, video_id = poll_1st(task_id)
            break
        except RuntimeError as e:
            attempt += 1
            log("video", f"1차 생성 실패: {e}")
            if attempt >= 4:  # 최초 1회 + 재시도 3회
                raise RuntimeError("영상 생성(1차) 4회 실패 → 사람에게")

    current_video_id = video_id
    for i in range(1, len(segments)):
        seg = segments[i]
        attempt = 0
        while True:
            prompt = seg["video_prompt"]
            negative = "blurry, low quality, distorted, extra limbs, watermark"
            if attempt > 0 and video_feedback_want:
                prompt += f". {video_feedback_want}"
            if attempt > 0 and video_feedback_avoid:
                negative += f", {video_feedback_avoid}"
            log("video", f"이어붙이기 구간 {i} 시도 {attempt + 1}회... (videoId={current_video_id})")
            try:
                ext_task_id = submit_extend(current_video_id, prompt, negative)
                video_url, ext_video_id = poll_extend(ext_task_id)
                current_video_id = ext_video_id
                break
            except RuntimeError as e:
                attempt += 1
                log("video", f"이어붙이기 구간 {i} 실패: {e}")
                if attempt >= 3:  # 최초 1회 + 재시도 2회
                    raise RuntimeError(f"이어붙이기 구간 {i} — 3회 실패 → 사람에게")
    return video_url


def _extract_video_id(video_obj):
    return video_obj.get("id") or video_obj.get("video_id") or video_obj.get("videoId") or ""


def kling_submit_1st(image_url, prompt, negative_prompt):
    body = {"model_name": "kling-v2-6", "image": image_url, "prompt": prompt,
            "negative_prompt": negative_prompt, "duration": "10", "mode": "std"}
    resp = requests.post(f"{KLING_HOST}/v1/videos/image2video", headers=kling_headers(), json=body, timeout=30)
    _check(resp, "Kling 1차 생성 제출")
    return str(resp.json().get("data", {}).get("task_id") or resp.json().get("data", {}).get("id") or "")


def kling_status_1st(task_id):
    resp = requests.get(f"{KLING_HOST}/v1/videos/image2video/{task_id}", headers=kling_headers(), timeout=30)
    _check(resp, "Kling 1차 상태 조회")
    task = resp.json().get("data", resp.json())
    status = task.get("task_status") or task.get("status") or ""
    videos = task.get("task_result", {}).get("videos") or [{}]
    if "succeed" in (status or "").lower():
        log("video", f"1차 생성 성공, 응답 구조 확인용 로그: {json.dumps(task, ensure_ascii=False)[:600]}")
    video_url = videos[0].get("url", "")
    video_id = _extract_video_id(videos[0])
    return status, video_url, video_id


def kling_submit_extend(video_id, prompt, negative_prompt):
    body = {"videoId": video_id, "prompt": prompt, "negative_prompt": negative_prompt, "duration": "5", "mode": "std"}
    resp = requests.post(f"{KLING_HOST}/v1/videos/video-extend", headers=kling_headers(), json=body, timeout=30)
    _check(resp, "Kling Extend 제출")
    return str(resp.json().get("data", {}).get("task_id") or resp.json().get("data", {}).get("id") or "")


def kling_status_extend(task_id):
    resp = requests.get(f"{KLING_HOST}/v1/videos/video-extend/{task_id}", headers=kling_headers(), timeout=30)
    _check(resp, "Kling Extend 상태 조회")
    task = resp.json().get("data", resp.json())
    status = task.get("task_status") or task.get("status") or ""
    videos = task.get("task_result", {}).get("videos") or [{}]
    if "succeed" in (status or "").lower():
        log("video", f"Extend 성공, 응답 구조 확인용 로그: {json.dumps(task, ensure_ascii=False)[:600]}")
    video_url = videos[0].get("url", "")
    video_id = _extract_video_id(videos[0])
    return status, video_url, video_id


# ============================================================
# 8단계: 오디오 합성 (로컬 ffmpeg 서버)
# ============================================================
def merge_audio(video_url, segments):
    log("merge", "오디오 합성 요청 중...")
    body = {
        "video_url": video_url,
        "segments": [{"audio_url": s["audio_url"], "target_duration_s": s["target_duration_s"]} for s in segments],
    }
    resp = requests.post(f"{FFMPEG_SERVER_URL}/merge-audio", json=body, timeout=600)
    if resp.status_code >= 300:
        raise RuntimeError(f"오디오 합성 실패 ({resp.status_code}): {resp.text[:300]}")
    return resp.content  # mp4 bytes


def upload_final_video(video_bytes, brand_id):
    storage_path = f"{brand_id}_{int(time.time())}.mp4"
    up = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/final-videos/{storage_path}",
        headers={**supabase_headers(), "Content-Type": "video/mp4", "x-upsert": "true"},
        data=video_bytes, timeout=120,
    )
    if up.status_code >= 300:
        raise RuntimeError(f"최종 영상 업로드 실패 ({up.status_code}): {up.text[:300]}")
    return f"{SUPABASE_URL}/storage/v1/object/public/final-videos/{storage_path}"


# ============================================================
# 오케스트레이션
# ============================================================
def run_pipeline(inputs, deps):
    """deps: 실제/목(mock) 함수들을 주입받아서 --mock일 때 네트워크 없이도 로직만 검증 가능"""
    if inputs.get("brand_id"):
        char = deps["get_character"](inputs["brand_id"])
    else:
        char = deps["create_character"](
            inputs["brand_description"], inputs.get("edit_category"),
            inputs.get("edit_instruction"), inputs.get("base_image_url"),
        )

    total_segments, durations = calc_segments(inputs["target_length_sec"])
    log("main", f"목표 {inputs['target_length_sec']}초 → 구간 {total_segments}개 {durations}")

    keywords = [k.strip() for k in (inputs.get("must_include_keywords") or "").split(",") if k.strip()]
    narration = deps["generate_script"](
        inputs["topic"], inputs["target_length_sec"], keywords,
        char.get("example_script"), inputs.get("script_feedback"),
        deps["call_llm"],
    )
    deps["save_example_script"](char["brand_id"], narration)

    segments = split_narration(narration, total_segments, durations)
    segments = deps["generate_narration_audio"](segments, char["brand_id"], deps["call_tts"])
    segments = deps["generate_video_prompts"](segments, total_segments, deps["call_llm"])

    video_url = deps["render_video"](
        char["image_url"], segments,
        inputs.get("video_feedback_want"), inputs.get("video_feedback_avoid"),
        deps["kling_submit_1st"], deps["kling_poll_1st"],
        deps["kling_submit_extend"], deps["kling_poll_extend"],
    )

    final_bytes = deps["merge_audio"](video_url, segments)
    final_url = deps["upload_final_video"](final_bytes, char["brand_id"])

    return {"brand_id": char["brand_id"], "narration": narration, "final_video_url": final_url}


def real_deps():
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
        "call_llm": call_llm_openrouter,
        "call_tts": call_tts_google,
        "kling_submit_1st": kling_submit_1st,
        "kling_poll_1st": lambda tid: kling_poll(tid, None, kling_status_1st),
        "kling_submit_extend": kling_submit_extend,
        "kling_poll_extend": lambda tid: kling_poll(tid, None, kling_status_extend),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="API 호출 없이 로직만 검증")
    parser.add_argument("--brand_id", default="")
    parser.add_argument("--brand_description", default="")
    parser.add_argument("--topic", default="신메뉴 출시 기념 할인")
    parser.add_argument("--target_length_sec", type=int, default=10)
    parser.add_argument("--must_include_keywords", default="")
    args = parser.parse_args()

    inputs = {
        "brand_id": args.brand_id, "brand_description": args.brand_description or "선글라스 낀 피자 마스코트",
        "topic": args.topic, "target_length_sec": args.target_length_sec,
        "must_include_keywords": args.must_include_keywords,
    }

    if args.mock:
        from mock_deps import build_mock_deps  # 같은 폴더의 mock_deps.py
        deps = build_mock_deps()
    else:
        deps = real_deps()

    result = run_pipeline(inputs, deps)
    log("main", f"완료: {json.dumps(result, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
