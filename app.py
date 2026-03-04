"""
LINEスタンプ自動でつくるくん - Webアプリケーション
"""

from __future__ import annotations

import json
import random
import shutil
import uuid
from pathlib import Path

import io

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from scripts.process_images import process_all_images
from scripts.make_preview import make_preview
from scripts.zip_output import create_zip

app = FastAPI(title="LINEスタンプ自動でつくるくん")

PROJECT_ROOT = Path(__file__).resolve().parent
TEMPLATES_PATH = PROJECT_ROOT / "prompts" / "message_templates.json"
SESSIONS_DIR = PROJECT_ROOT / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))


def load_templates() -> list[str]:
    with open(TEMPLATES_PATH, encoding="utf-8") as f:
        return json.load(f)["templates"]


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/templates")
async def get_templates():
    return {"templates": load_templates()}


@app.post("/api/preview-image")
async def preview_image(file: UploadFile = File(...)):
    """画像をJPEGサムネイルに変換して返す（HEIC対応）。"""
    import pillow_heif
    pillow_heif.register_heif_opener()
    from PIL import Image

    content = await file.read()
    try:
        img = Image.open(io.BytesIO(content))
        img.thumbnail((200, 200))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        buf.seek(0)
        return Response(content=buf.getvalue(), media_type="image/jpeg")
    except Exception:
        return JSONResponse({"error": "変換失敗"}, status_code=400)


@app.post("/api/upload")
async def upload_images(files: list[UploadFile] = File(...)):
    """画像をアップロードしてセッションIDを返す。"""
    session_id = str(uuid.uuid4())[:8]
    session_dir = SESSIONS_DIR / session_id / "input"
    session_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for i, file in enumerate(files):
        ext = Path(file.filename).suffix.lower() or ".jpg"
        dest = session_dir / f"{i + 1:02d}{ext}"
        with open(dest, "wb") as f:
            content = await file.read()
            f.write(content)
        saved.append(file.filename)

    return {"session_id": session_id, "files": saved, "count": len(saved)}


@app.post("/api/generate")
async def generate_stamps(
    session_id: str = Form(...),
    mode: str = Form(...),
    messages: str = Form(""),
):
    """スタンプを生成する。"""
    session_dir = SESSIONS_DIR / session_id
    input_dir = session_dir / "input"
    output_dir = session_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_dir.exists():
        return JSONResponse({"error": "セッションが見つかりません"}, status_code=404)

    # メッセージ処理
    all_templates = load_templates()

    if mode == "A":
        msg_list = random.sample(all_templates, 8)
    elif mode == "B":
        msg_list = json.loads(messages) if messages else all_templates[:8]
    elif mode == "C":
        msg_list = json.loads(messages) if messages else [""] * 8
    else:  # D
        msg_list = [None] * 8

    try:
        process_all_images(input_dir, output_dir, msg_list)
        make_preview(output_dir)
        create_zip(output_dir)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return {
        "session_id": session_id,
        "messages": msg_list,
        "stamps": [f"{i:02d}.png" for i in range(1, 9)],
    }


@app.get("/api/stamp/{session_id}/{filename}")
async def get_stamp(session_id: str, filename: str):
    path = SESSIONS_DIR / session_id / "output" / filename
    if not path.exists():
        return JSONResponse({"error": "ファイルが見つかりません"}, status_code=404)
    return FileResponse(path, media_type="image/png")


@app.get("/api/preview/{session_id}")
async def get_preview(session_id: str):
    path = SESSIONS_DIR / session_id / "output" / "preview.png"
    if not path.exists():
        return JSONResponse({"error": "プレビューが見つかりません"}, status_code=404)
    return FileResponse(path, media_type="image/png")


@app.get("/api/download/{session_id}")
async def download_zip(session_id: str):
    path = SESSIONS_DIR / session_id / "output" / "line_stamp.zip"
    if not path.exists():
        return JSONResponse({"error": "ZIPが見つかりません"}, status_code=404)
    return FileResponse(
        path,
        media_type="application/zip",
        filename="line_stamp.zip",
    )


@app.post("/api/upload-to-line/{session_id}")
async def upload_to_line_api(
    session_id: str,
    title: str = Form("ペットスタンプ"),
    description: str = Form("かわいいペットのスタンプです"),
):
    """Playwrightでブラウザを開き、LINE Creators Marketに自動登録する。"""
    output_dir = SESSIONS_DIR / session_id / "output"
    if not output_dir.exists():
        return JSONResponse({"error": "セッションが見つかりません"}, status_code=404)

    stamp_files = sorted(output_dir.glob("[0-9][0-9].png"))
    if not stamp_files:
        return JSONResponse({"error": "スタンプ画像が見つかりません"}, status_code=404)

    # ステータスファイルのパスを設定
    status_file = SESSIONS_DIR / session_id / "upload_status.json"

    # Playwrightを別スレッドで実行（ブラウザが開く）
    import threading
    from scripts.upload_to_line import upload_to_line

    thread = threading.Thread(
        target=upload_to_line,
        args=(output_dir, title, description),
        kwargs={"interactive": False, "status_file": status_file},
        daemon=True,
    )
    thread.start()

    return {
        "status": "started",
        "message": "ブラウザが開きます。LINEアカウントでログインしてください。",
        "session_id": session_id,
    }


@app.get("/api/upload-status/{session_id}")
async def get_upload_status(session_id: str):
    """LINE自動登録の進捗を返す。"""
    status_file = SESSIONS_DIR / session_id / "upload_status.json"
    if not status_file.exists():
        return {"step": "待機中", "message": "処理を開始しています...", "progress": 0}

    import json as json_module
    try:
        data = json_module.loads(status_file.read_text(encoding="utf-8"))
        return data
    except Exception:
        return {"step": "待機中", "message": "ステータスを取得中...", "progress": 0}


@app.delete("/api/session/{session_id}")
async def cleanup_session(session_id: str):
    session_dir = SESSIONS_DIR / session_id
    if session_dir.exists():
        shutil.rmtree(session_dir)
    return {"status": "ok"}
