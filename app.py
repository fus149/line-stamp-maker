"""
LINEスタンプ自動でつくるくん - Webアプリケーション
"""

from __future__ import annotations

import json
import random
import shutil
import threading
import time
import uuid
from pathlib import Path

import io

import pillow_heif
from PIL import Image as PILImageModule

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from scripts.process_images import process_all_images
from scripts.make_preview import make_preview
from scripts.zip_output import create_zip

# HEIF/HEICサポートを起動時に一度だけ登録
pillow_heif.register_heif_opener()

app = FastAPI(title="LINEスタンプ自動でつくるくん")

PROJECT_ROOT = Path(__file__).resolve().parent
TEMPLATES_PATH = PROJECT_ROOT / "prompts" / "message_templates.json"
SESSIONS_DIR = PROJECT_ROOT / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))

# サーバー起動時刻をキャッシュバスターに使用（別PCでも最新CSS/JSを確実に読み込む）
CACHE_BUST = str(int(time.time()))

# 同時LINE登録処理の上限（各処理がヘッドレスChrome1つを使うため）
_upload_semaphore = threading.Semaphore(3)


def load_templates() -> list[str]:
    with open(TEMPLATES_PATH, encoding="utf-8") as f:
        return json.load(f)["templates"]


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "v": CACHE_BUST})


@app.get("/api/templates")
async def get_templates():
    return {"templates": load_templates()}


@app.post("/api/preview-image")
async def preview_image(file: UploadFile = File(...)):
    """画像をJPEGサムネイルに変換して返す（HEIC対応）。"""
    content = await file.read()
    try:
        img = PILImageModule.open(io.BytesIO(content))
        # 高速リサイズ: draft()でデコード段階から縮小（JPEG向け）
        if hasattr(img, 'draft') and img.format == 'JPEG':
            img.draft('RGB', (150, 150))
        img.thumbnail((150, 150), PILImageModule.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60, optimize=False)
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

    # 全ファイルを先にメモリに読み込み（I/Oパイプライン最適化）
    file_data = []
    for i, file in enumerate(files):
        content = await file.read()
        ext = Path(file.filename).suffix.lower() or ".jpg"
        file_data.append((f"{i + 1:02d}{ext}", content, file.filename))

    # ディスクへの書き込みを一括実行
    saved = []
    for fname, content, original_name in file_data:
        dest = session_dir / fname
        dest.write_bytes(content)
        saved.append(original_name)

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
        result = process_all_images(input_dir, output_dir, msg_list)
        make_preview(output_dir)
        create_zip(output_dir)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return {
        "session_id": session_id,
        "messages": msg_list,
        "stamps": [f"{i:02d}.png" for i in range(1, 9)],
        "text_positions": result["text_positions"],
    }


@app.get("/api/stamp/{session_id}/{filename}")
async def get_stamp(session_id: str, filename: str):
    path = SESSIONS_DIR / session_id / "output" / filename
    if not path.exists():
        return JSONResponse({"error": "ファイルが見つかりません"}, status_code=404)
    return FileResponse(path, media_type="image/png")


@app.put("/api/stamp/{session_id}/{filename}")
async def update_stamp(
    session_id: str,
    filename: str,
    image: UploadFile = File(...),
    text: str = Form(""),
    text_x: str = Form("-1"),
    text_y: str = Form("-1"),
    font_id: str = Form("zen-maru"),
    text_color: str = Form("white"),
    vertical: str = Form("false"),
):
    """編集済みスタンプ画像を上書き保存する。"""
    import re
    from PIL import Image as PILImage
    from scripts.process_images import _add_text, generate_main_and_tab

    # ファイル名バリデーション（01.png〜08.png のみ）
    if not re.match(r"^0[1-8]\.png$", filename):
        return JSONResponse({"error": "無効なファイル名です"}, status_code=400)

    output_dir = SESSIONS_DIR / session_id / "output"
    stamp_path = output_dir / filename
    base_path = output_dir / filename.replace(".png", "_base.png")

    if not stamp_path.exists():
        return JSONResponse({"error": "スタンプが見つかりません"}, status_code=404)

    # アップロードされたbase画像を検証
    content = await image.read()
    try:
        img = PILImage.open(io.BytesIO(content))
        if img.size != (370, 320):
            return JSONResponse({"error": f"画像サイズが不正です: {img.size}"}, status_code=400)
        if img.mode != "RGBA":
            img = img.convert("RGBA")
    except Exception:
        return JSONResponse({"error": "無効な画像データです"}, status_code=400)

    # base画像を上書き保存
    img.save(str(base_path), "PNG")

    # テキストがあればサーバー側で描画して最終画像を作成
    is_vertical = vertical.lower() in ("true", "1", "yes")
    tx, ty = int(text_x), int(text_y)
    if text.strip() and tx >= 0 and ty >= 0:
        final_img = img.copy()
        final_img = _add_text(
            final_img, text.strip(),
            text_x=tx, text_y=ty,
            font_id=font_id, text_color=text_color, vertical=is_vertical,
        )
        final_img.save(str(stamp_path), "PNG")
    else:
        img.save(str(stamp_path), "PNG")

    # 01.png編集時はmain/tab画像を再生成
    if filename == "01.png":
        generate_main_and_tab(output_dir)

    # preview.pngとZIPを再生成
    make_preview(output_dir)
    create_zip(output_dir)

    return {"status": "ok", "filename": filename}


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
    title: str = Form("Pet Stickers"),
    description: str = Form("Cute pet stickers"),
    email: str = Form(""),
    password: str = Form(""),
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

    # Playwrightを別スレッドで実行（ヘッドレスブラウザ）
    from scripts.upload_to_line import upload_to_line

    def _run_upload():
        if not _upload_semaphore.acquire(timeout=30):
            # 同時接続数上限に達した場合
            error_data = {
                "step": "エラー",
                "message": "現在混み合っています。しばらくしてからもう一度お試しください。",
                "progress": 0,
                "logs": ["[エラー] 同時処理数の上限に達しました"],
            }
            status_file.write_text(json.dumps(error_data, ensure_ascii=False), encoding="utf-8")
            return
        try:
            upload_to_line(output_dir, title, description, interactive=False, status_file=status_file, email=email, password=password)
        except Exception as e:
            # スレッド内の未捕捉エラーをステータスファイルに記録
            error_data = {
                "step": "エラー",
                "message": f"スレッド内エラー: {e}",
                "progress": 0,
                "logs": [f"[エラー] スレッド内エラー: {e}"],
            }
            status_file.write_text(json.dumps(error_data, ensure_ascii=False), encoding="utf-8")
        finally:
            _upload_semaphore.release()

    thread = threading.Thread(target=_run_upload, daemon=True)
    thread.start()

    return {
        "status": "started",
        "message": "LINEに自動ログインしています。しばらくお待ちください。",
        "session_id": session_id,
    }


@app.get("/api/qr-code/{session_id}")
async def get_qr_code(session_id: str):
    """LINEログイン用QRコード画像を返す（スマホ表示用）。"""
    path = SESSIONS_DIR / session_id / "qr_code.png"
    if not path.exists():
        return JSONResponse({"error": "QRコードがまだ準備できていません"}, status_code=404)
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


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


@app.get("/api/debug-screenshots/{session_id}")
async def list_debug_screenshots(session_id: str):
    """デバッグスクリーンショットの一覧を返す。"""
    debug_dir = SESSIONS_DIR / session_id / "output" / "debug"
    if not debug_dir.exists():
        return {"screenshots": []}
    files = sorted(f.name for f in debug_dir.glob("*.png"))
    return {"screenshots": files}


@app.get("/api/debug-screenshots/{session_id}/{filename}")
async def get_debug_screenshot(session_id: str, filename: str):
    """デバッグスクリーンショットを返す。"""
    path = SESSIONS_DIR / session_id / "output" / "debug" / filename
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="image/png")


@app.delete("/api/session/{session_id}")
async def cleanup_session(session_id: str):
    session_dir = SESSIONS_DIR / session_id
    if session_dir.exists():
        shutil.rmtree(session_dir)
    return {"status": "ok"}
