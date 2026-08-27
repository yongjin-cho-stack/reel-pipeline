from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
import asyncio
import subprocess
import tempfile
import os
import uuid
import requests

app = FastAPI()

OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-3.1-flash")


@app.get("/delay/{seconds}")
async def delay(seconds: float):
    # Dify Loop 노드에 sleep 기능이 없어서, 루프 안에서 이 엔드포인트를 호출해
    # Kling API를 너무 자주 두드리지 않도록 텀을 준다.
    await asyncio.sleep(min(seconds, 15))
    return {"waited": seconds}


@app.post("/ask")
async def ask(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")

    if not prompt:
        return {"error": "prompt is missing"}

    if not OPENROUTER_KEY:
        # 키가 없을 때는 LLM 없이 그냥 프롬프트를 그대로 다듬어서 돌려줌 (임시 대체)
        return {"result": prompt}

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data["choices"][0]["message"]["content"]
        return {"result": result}
    except Exception as e:
        return {"error": f"OpenRouter call failed: {e}"}


@app.post("/compose")
async def compose(request: Request):
    form = await request.form()

    user_video_url = form.get("user_video_url")
    position = form.get("position", "right-bottom")
    size = int(form.get("size", 180))
    character = form.get("character_video")

    if not user_video_url:
        return {"error": "user_video_url is missing"}
    if character is None:
        return {"error": "character_video is missing"}

    work_dir = tempfile.mkdtemp()
    user_path = os.path.join(work_dir, "user.mp4")
    character_path = os.path.join(work_dir, "character.mp4")
    output_path = os.path.join(work_dir, f"{uuid.uuid4()}.mp4")

    response = requests.get(user_video_url)
    if response.status_code != 200:
        return {"error": "Failed to download user video", "status": response.status_code}

    with open(user_path, "wb") as f:
        f.write(response.content)

    with open(character_path, "wb") as f:
        f.write(await character.read())

    positions = {
        "left-top": "20:20",
        "center-top": "(W-w)/2:20",
        "right-top": "W-w-20:20",
        "left-center": "20:(H-h)/2",
        "center": "(W-w)/2:(H-h)/2",
        "right-center": "W-w-20:(H-h)/2",
        "left-bottom": "20:H-h-20",
        "center-bottom": "(W-w)/2:H-h-20",
        "right-bottom": "W-w-20:H-h-20",
    }
    overlay_position = positions.get(position, positions["right-bottom"])

    filter_complex = (
        f"[1:v]scale={size}:-1,"
        f"colorkey=white:0.25:0.08[char];"
        f"[0:v][char]overlay={overlay_position}:shortest=1"
    )

    command = [
        "ffmpeg", "-y",
        "-i", user_path,
        "-i", character_path,
        "-filter_complex", filter_complex,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        output_path,
    ]

    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if result.returncode != 0:
        return {"error": "FFmpeg failed", "detail": result.stderr[-3000:]}

    return FileResponse(output_path, media_type="video/mp4", filename="final.mp4")


def _probe_duration(path):
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        return float(p.stdout.strip())
    except ValueError:
        return None


def _atempo_chain(ratio):
    """ffmpeg atempo 필터는 한 번에 0.5~2.0 배속만 지원해서, 그 밖의 비율은 체이닝해서 만든다."""
    filters = []
    r = ratio
    while r > 2.0:
        filters.append("atempo=2.0")
        r /= 2.0
    while r < 0.5:
        filters.append("atempo=0.5")
        r /= 0.5
    filters.append(f"atempo={r:.4f}")
    return ",".join(filters)


@app.post("/merge-audio")
async def merge_audio(request: Request):
    """영상(video_url)에 나레이션 오디오 조각들을 구간 목표 길이(target_duration_s)에 맞춰
    속도 미세조정(time-stretch)한 뒤 순서대로 이어붙여서 입힌다. 이렇게 해야 구간 경계가
    영상 구간 경계(1차=10초, 각 Extend=5초)와 정확히 맞아 음성-화면 드리프트가 안 생긴다.
    JSON body: {"video_url": "...", "segments": [{"audio_url": "...", "target_duration_s": 10}, ...]}
    (하위호환: "audio_urls": [...] 만 주면 stretch 없이 그냥 이어붙임)
    """
    data = await request.json()
    video_url = data.get("video_url")
    segments = data.get("segments")
    if not segments:
        segments = [{"audio_url": u, "target_duration_s": None} for u in data.get("audio_urls", [])]

    if not video_url:
        return {"error": "video_url is missing"}
    if not segments:
        return {"error": "segments (or audio_urls) is missing or empty"}

    work_dir = tempfile.mkdtemp()
    video_path = os.path.join(work_dir, "video.mp4")
    output_path = os.path.join(work_dir, f"{uuid.uuid4()}.mp4")

    resp = requests.get(video_url)
    if resp.status_code != 200:
        return {"error": "Failed to download video", "status": resp.status_code}
    with open(video_path, "wb") as f:
        f.write(resp.content)

    stretched_paths = []
    stretch_log = []
    for i, seg in enumerate(segments):
        url = seg.get("audio_url")
        target = seg.get("target_duration_s")
        r = requests.get(url)
        if r.status_code != 200:
            return {"error": f"Failed to download audio segment {i}", "status": r.status_code}
        raw_path = os.path.join(work_dir, f"audio_{i}_raw.mp3")
        with open(raw_path, "wb") as f:
            f.write(r.content)

        if target:
            actual = _probe_duration(raw_path)
            if actual and actual > 0.1:
                ratio = actual / target  # atempo 배속 = 원래길이/목표길이
                ratio = max(0.3, min(3.0, ratio))  # 극단적인 왜곡 방지 안전장치
                stretched_path = os.path.join(work_dir, f"audio_{i}.m4a")
                cmd = ["ffmpeg", "-y", "-i", raw_path, "-filter:a", _atempo_chain(ratio), "-c:a", "aac", stretched_path]
                rr = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if rr.returncode != 0:
                    return {"error": f"Segment {i} time-stretch failed", "detail": rr.stderr[-2000:]}
                stretch_log.append({"segment": i, "actual_s": round(actual, 2), "target_s": target, "speed_ratio": round(ratio, 3)})
                stretched_paths.append(stretched_path)
                continue
        stretched_paths.append(raw_path)

    audio_paths = stretched_paths
    if stretch_log:
        print(f"[merge-audio] 구간별 속도 조정: {stretch_log}")

    # 나레이션 조각들을 순서대로 이어붙이기 (이미 구간별로 목표 길이에 맞춰져 있어 경계가 영상과 맞음)
    concat_list_path = os.path.join(work_dir, "concat_list.txt")
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for p in audio_paths:
            f.write(f"file '{p}'\n")
    narration_path = os.path.join(work_dir, "narration.m4a")
    concat_cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", concat_list_path,
        "-c:a", "aac",
        narration_path,
    ]
    r1 = subprocess.run(concat_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r1.returncode != 0:
        return {"error": "Audio concat failed", "detail": r1.stderr[-3000:]}

    video_dur = _probe_duration(video_path)
    narration_dur = _probe_duration(narration_path)
    diff = None
    if video_dur is not None and narration_dur is not None:
        diff = round(narration_dur - video_dur, 2)
        print(f"[merge-audio] video={video_dur:.2f}s narration={narration_dur:.2f}s diff={diff:+.2f}s")
        if abs(diff) > 1.5:
            print(f"[merge-audio] ⚠️ 길이 차이가 {abs(diff):.1f}초나 나요 — "
                  f"{'나레이션이 잘릴 수 있어요' if diff > 0 else '영상 끝에 무음 구간이 생겨요'}")

    # 이어붙인 나레이션을 영상에 입히기 (영상 자체 오디오는 대체됨)
    # 나레이션이 영상보다 길면 잘라내지 않고 영상 쪽을 나레이션 길이에 맞춰 마지막 프레임을 정지시켜 늘림
    if diff is not None and diff > 0:
        merge_cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", narration_path,
            "-filter_complex",
            f"[0:v]tpad=stop_mode=clone:stop_duration={diff}[v]",
            "-map", "[v]",
            "-map", "1:a:0",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            output_path,
        ]
    else:
        merge_cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", narration_path,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            output_path,
        ]
    r2 = subprocess.run(merge_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r2.returncode != 0:
        return {"error": "Merge failed", "detail": r2.stderr[-3000:]}

    headers = {}
    if diff is not None:
        headers["X-Video-Duration"] = str(video_dur)
        headers["X-Narration-Duration"] = str(narration_dur)
        headers["X-Duration-Diff"] = str(diff)
    return FileResponse(output_path, media_type="video/mp4", filename="final_with_audio.mp4", headers=headers)


@app.post("/run-pipeline")
def run_pipeline_endpoint(body: dict):
    """브랜드 캐릭터 숏츠 파이프라인 전체를 한 번에 실행한다.
    JSON body 예시:
    {
      "brand_id": "",                 // 비워두면 신규 등록, 있으면 기존 캐릭터 재사용
      "brand_description": "...",     // 신규 등록시
      "edit_category": "", "edit_instruction": "", "base_image_url": "",  // 캐릭터 수정시만
      "topic": "...", "target_length_sec": 15,
      "must_include_keywords": "키워드1, 키워드2",
      "script_feedback": "", "video_feedback_want": "", "video_feedback_avoid": ""
    }
    일부러 async가 아니라 일반 def로 만듦 — FastAPI가 이런 건 스레드풀에서 돌려서,
    이 안에서 서버 자기 자신(/merge-audio)을 다시 호출해도 이벤트 루프가 막히지 않음.
    """
    import reel_pipeline
    try:
        result = reel_pipeline.run_pipeline(body, reel_pipeline.real_deps())
        return result
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# 단계별 API (웹페이지에서 한 단계씩 승인/재시도 하면서 쓰는 용도)
# 전부 일반 def — 이벤트 루프를 막지 않고 스레드풀에서 실행됨 (자기 자신 호출 교착 방지)
# ============================================================
@app.post("/api/character")
def api_character(body: dict):
    import reel_pipeline
    try:
        if body.get("brand_id"):
            result = reel_pipeline.get_character(body["brand_id"])
        else:
            result = reel_pipeline.create_character(
                body.get("brand_description", ""), body.get("edit_category"),
                body.get("edit_instruction"), body.get("base_image_url"),
            )
        return result
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/script")
def api_script(body: dict):
    import reel_pipeline
    try:
        keywords = [k.strip() for k in (body.get("must_include_keywords") or "").split(",") if k.strip()]
        narration = reel_pipeline.generate_script(
            body["topic"], body["target_length_sec"], keywords,
            body.get("example_script"), body.get("script_feedback"),
            reel_pipeline.call_llm_openrouter,
        )
        reel_pipeline.save_example_script(body["brand_id"], narration)
        return {"narration": narration}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/render")
def api_render(body: dict):
    import reel_pipeline
    try:
        total_segments, durations = reel_pipeline.calc_segments(body["target_length_sec"])
        segments = reel_pipeline.split_narration(body["narration"], total_segments, durations)
        segments = reel_pipeline.generate_narration_audio(segments, body["brand_id"], reel_pipeline.call_tts_google)
        segments = reel_pipeline.generate_video_prompts(segments, total_segments, reel_pipeline.call_llm_openrouter)
        video_url = reel_pipeline.render_video(
            body["image_url"], segments,
            body.get("video_feedback_want"), body.get("video_feedback_avoid"),
            reel_pipeline.kling_submit_1st,
            lambda tid: reel_pipeline.kling_poll(tid, None, reel_pipeline.kling_status_1st),
            reel_pipeline.kling_submit_extend,
            lambda tid: reel_pipeline.kling_poll(tid, None, reel_pipeline.kling_status_extend),
        )
        final_bytes = reel_pipeline.merge_audio(video_url, segments)
        final_url = reel_pipeline.upload_final_video(final_bytes, body["brand_id"])
        return {"final_video_url": final_url}
    except Exception as e:
        return {"error": str(e)}


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(os.path.dirname(__file__), "index.html"), "r", encoding="utf-8") as f:
        return f.read()
