"""
LINEスタンプ画像処理モジュール

ペット画像の背景除去、構図調整、文字入れを行う。
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ExifTags
from pillow_heif import register_heif_opener
from rembg import remove, new_session

# HEIC/HEIF形式をPillowで読めるように登録
register_heif_opener()

STAMP_WIDTH = 370
STAMP_HEIGHT = 320
MARGIN = 15
TEXT_AREA_HEIGHT = 55
OUTLINE_WIDTH = 3

# LINE Creators Market 必須画像サイズ
MAIN_IMAGE_SIZE = (240, 240)  # メイン画像
TAB_IMAGE_SIZE = (96, 74)     # タブ画像

# 高速化: 入力画像の最大サイズ（長辺px）
# IS-Netは1024x1024で推論するため、それ以上の入力は無駄に重い
# alpha mattingも入力解像度で実行されるため、縮小で大幅に高速化
MAX_INPUT_DIMENSION = 1500

# 並列処理ワーカー数
PARALLEL_WORKERS = 4

# --- フォント候補（丸ゴシック優先） ---
FONT_CANDIDATES = [
    # macOS
    "/System/Library/Fonts/ヒラギノ丸ゴ ProN W4.ttc",
    "/System/Library/Fonts/Hiragino Sans GB W3.otf",
    "/System/Library/Fonts/HiraginoSans-W6.ttc",
    # Linux (rounded-mgenplus等を手動インストール)
    "/usr/share/fonts/truetype/rounded-mgenplus/rounded-mgenplus-1c-medium.ttf",
    # プロジェクト内バンドルフォント
    str(Path(__file__).resolve().parent.parent / "fonts" / "font.ttf"),
]


def _load_font(font_path: Optional[str] = None, size: int = 32) -> ImageFont.FreeTypeFont:
    """丸ゴシックフォントをロードする。"""
    if font_path and os.path.exists(font_path):
        return ImageFont.truetype(font_path, size)

    for candidate in FONT_CANDIDATES:
        if os.path.exists(candidate):
            return ImageFont.truetype(candidate, size)

    print("警告: 丸ゴシックフォントが見つかりません。デフォルトフォントを使用します。")
    try:
        return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", size)
    except OSError:
        return ImageFont.load_default()


def correct_orientation(img: Image.Image) -> Image.Image:
    """EXIF情報に基づいて画像の向きを補正する。"""
    try:
        exif = img._getexif()
        if not exif:
            return img
        orientation_key = None
        for tag, name in ExifTags.TAGS.items():
            if name == "Orientation":
                orientation_key = tag
                break
        if orientation_key and orientation_key in exif:
            orientation = exif[orientation_key]
            rotations = {3: 180, 6: 270, 8: 90}
            if orientation in rotations:
                img = img.rotate(rotations[orientation], expand=True)
    except (AttributeError, KeyError):
        pass
    return img


def correct_brightness(img: Image.Image, threshold: float = 85.0) -> Image.Image:
    """暗すぎる画像の明るさを自動補正する。"""
    grayscale = img.convert("L")
    mean_brightness = np.array(grayscale).mean()
    if mean_brightness < threshold:
        factor = min(threshold / max(mean_brightness, 1), 2.0)
        img = ImageEnhance.Brightness(img).enhance(factor)
    return img


def _predownscale(img: Image.Image, max_dim: int = MAX_INPUT_DIMENSION) -> Image.Image:
    """大きな画像をrembg処理前に縮小して高速化する。

    IS-Netモデルは内部で1024x1024にリサイズするため、
    それを大きく超える入力画像は事前に縮小しても品質への影響が少ない。
    alpha mattingは入力解像度で実行されるため、縮小の効果が特に大きい。
    """
    w, h = img.size
    if max(w, h) <= max_dim:
        return img
    ratio = max_dim / max(w, h)
    new_w = int(w * ratio)
    new_h = int(h * ratio)
    return img.resize((new_w, new_h), Image.LANCZOS)


# IS-Netモデルをモジュール読み込み時に一度だけ初期化
_bg_session = None


def _get_bg_session():
    global _bg_session
    if _bg_session is None:
        _bg_session = new_session("isnet-general-use")
    return _bg_session


def remove_background(img: Image.Image) -> Image.Image:
    """背景を除去し、毛並みを自然に保持する（IS-Net + alpha matting）。"""
    session = _get_bg_session()
    result = remove(
        img,
        session=session,
        alpha_matting=True,
        alpha_matting_foreground_threshold=230,
        alpha_matting_background_threshold=20,
        alpha_matting_erode_size=8,
    )
    return result


def _get_subject_bbox(img: Image.Image) -> Tuple[int, int, int, int]:
    """透過部分を除いた被写体のバウンディングボックスを取得する。"""
    if img.mode != "RGBA":
        return (0, 0, img.width, img.height)

    alpha = np.array(img)[:, :, 3]
    rows = np.any(alpha > 10, axis=1)
    cols = np.any(alpha > 10, axis=0)

    if not rows.any() or not cols.any():
        return (0, 0, img.width, img.height)

    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]
    return (int(cmin), int(rmin), int(cmax) + 1, int(rmax) + 1)


def _detect_face_position(img: Image.Image) -> str:
    """被写体の顔が上部か下部かを推定する。

    ペットの顔は通常上部にあるため、上半分のコンテンツ密度が
    高い場合に 'upper' を返す。
    """
    if img.mode != "RGBA":
        return "upper"

    alpha = np.array(img)[:, :, 3]
    h = alpha.shape[0]
    # 上部 1/3 と 下部 1/3 を比較
    upper_third = np.sum(alpha[: h // 3] > 10)
    lower_third = np.sum(alpha[2 * h // 3 :] > 10)

    return "upper" if upper_third >= lower_third else "lower"


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """テキストが最大幅を超える場合に自動改行する。"""
    dummy = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(dummy)

    bbox = draw.textbbox((0, 0), text, font=font)
    if bbox[2] - bbox[0] <= max_width:
        return text

    # 文字単位で改行位置を探す
    best_break = len(text) // 2
    for i in range(len(text)):
        line1 = text[: i + 1]
        bbox1 = draw.textbbox((0, 0), line1, font=font)
        if bbox1[2] - bbox1[0] > max_width:
            best_break = max(i - 1, 1)
            break

    return text[:best_break] + "\n" + text[best_break:]


def _center_and_resize(
    img: Image.Image, text_position: Optional[str], has_text: bool
) -> Image.Image:
    """被写体を中央配置し、スタンプサイズにリサイズする。"""
    bbox = _get_subject_bbox(img)
    cropped = img.crop(bbox)

    available_w = STAMP_WIDTH - (MARGIN * 2)
    if has_text:
        available_h = STAMP_HEIGHT - (MARGIN * 2) - TEXT_AREA_HEIGHT
    else:
        available_h = STAMP_HEIGHT - (MARGIN * 2)

    ratio = min(available_w / cropped.width, available_h / cropped.height)
    new_w = int(cropped.width * ratio)
    new_h = int(cropped.height * ratio)
    cropped = cropped.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGBA", (STAMP_WIDTH, STAMP_HEIGHT), (0, 0, 0, 0))

    x = (STAMP_WIDTH - new_w) // 2

    if has_text and text_position == "top":
        # テキストが上 → 被写体は下
        y = MARGIN + TEXT_AREA_HEIGHT + (available_h - new_h) // 2
    elif has_text and text_position == "bottom":
        # テキストが下 → 被写体は上
        y = MARGIN + (available_h - new_h) // 2
    else:
        y = (STAMP_HEIGHT - new_h) // 2

    canvas.paste(cropped, (x, y), cropped)
    return canvas


def _add_text(
    img: Image.Image,
    text: str,
    text_position: str,
    font_path: Optional[str] = None,
    font_size: int = 32,
) -> Image.Image:
    """白文字＋黒縁取りのテキストを追加する。"""
    font = _load_font(font_path, font_size)
    draw = ImageDraw.Draw(img)

    max_text_width = STAMP_WIDTH - (MARGIN * 2) - 10
    text = _wrap_text(text, font, max_text_width)

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    x = (STAMP_WIDTH - text_w) // 2

    if text_position == "bottom":
        y = STAMP_HEIGHT - text_h - MARGIN - 5
    else:
        y = MARGIN + 5

    # 黒縁取り
    for dx in range(-OUTLINE_WIDTH, OUTLINE_WIDTH + 1):
        for dy in range(-OUTLINE_WIDTH, OUTLINE_WIDTH + 1):
            if dx * dx + dy * dy <= OUTLINE_WIDTH * OUTLINE_WIDTH + 1:
                draw.text(
                    (x + dx, y + dy), text, font=font, fill=(0, 0, 0, 255), anchor=None
                )

    # 白文字
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))
    return img


def process_single_image(
    input_path,
    output_path,
    message: Optional[str] = None,
    font_path: Optional[str] = None,
    font_size: int = 32,
) -> Path:
    """1枚の画像をLINEスタンプに変換する。

    Args:
        input_path: 入力画像パス
        output_path: 出力PNGパス
        message: スタンプに表示するメッセージ（Noneの場合テキストなし）
        font_path: フォントファイルパス（省略時は自動検出）
        font_size: フォントサイズ

    Returns:
        出力ファイルのパス
    """
    output_path = Path(output_path)

    # 画像読み込み
    img = Image.open(input_path).convert("RGBA")

    # Step1: 向き補正
    img = correct_orientation(img)

    # Step1b: 大きな画像を事前縮小（背景除去の高速化）
    original_size = img.size
    img = _predownscale(img)
    if img.size != original_size:
        print(f"  プリダウンスケール: {original_size[0]}x{original_size[1]} → {img.size[0]}x{img.size[1]}")

    # Step1c: 明るさ補正
    img_rgb = img.convert("RGB")
    img_rgb = correct_brightness(img_rgb)
    img = img_rgb.convert("RGBA")

    # Step2: 背景除去
    print(f"  背景除去中... {Path(input_path).name}")
    img = remove_background(img)

    # 顔位置検出 → テキスト配置決定
    has_text = message is not None and message.strip() != ""
    if has_text:
        face_pos = _detect_face_position(img)
        text_position = "bottom" if face_pos == "upper" else "top"
    else:
        text_position = None

    # Step3: 構図調整
    img = _center_and_resize(img, text_position, has_text)

    # 文字追加
    if has_text:
        img = _add_text(img, message, text_position, font_path, font_size)

    # 保存
    img.save(str(output_path), "PNG")
    print(f"  完了: {output_path.name}")
    return output_path


def process_all_images(
    input_dir,
    output_dir,
    messages: List[Optional[str]],
    font_path: Optional[str] = None,
    font_size: int = 32,
) -> List[Path]:
    """8枚の画像を一括処理する。

    Args:
        input_dir: 入力画像ディレクトリ
        output_dir: 出力ディレクトリ
        messages: 8つのメッセージ（メッセージなしの場合はNoneを含むリスト）
        font_path: フォントファイルパス
        font_size: フォントサイズ

    Returns:
        出力ファイルパスのリスト
    """
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 画像ファイル収集（拡張子でフィルタ）
    valid_extensions = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp"}
    image_files = sorted(
        [f for f in input_dir.iterdir() if f.suffix.lower() in valid_extensions]
    )

    if len(image_files) < 8:
        raise ValueError(
            f"画像が{len(image_files)}枚しかありません。8枚必要です。"
        )

    if len(image_files) > 8:
        print(f"注意: {len(image_files)}枚の画像が見つかりました。最初の8枚を使用します。")
        image_files = image_files[:8]

    if len(messages) != 8:
        raise ValueError(f"メッセージは8個必要です（現在{len(messages)}個）。")

    # セッションを事前初期化（並列処理前にモデルロードを完了）
    print("モデルを初期化中...")
    _get_bg_session()

    # 並列処理で8枚を同時に処理
    start_time = time.time()
    results = [None] * 8

    def _process_one(args):
        i, img_file, msg = args
        output_path = output_dir / f"{i:02d}.png"
        print(f"\n[{i}/8] {img_file.name} を処理中...")
        t0 = time.time()
        result = process_single_image(img_file, output_path, msg, font_path, font_size)
        print(f"  [{i}/8] 完了 ({time.time() - t0:.1f}秒)")
        return i, result

    tasks = [
        (i + 1, img_file, msg)
        for i, (img_file, msg) in enumerate(zip(image_files, messages))
    ]

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = {executor.submit(_process_one, task): task[0] for task in tasks}
        for future in as_completed(futures):
            idx, result = future.result()
            results[idx - 1] = result

    elapsed = time.time() - start_time
    print(f"\n全8枚の処理完了（{elapsed:.1f}秒）")

    # main.png / tab.png を自動生成
    generate_main_and_tab(output_dir)

    return results


def generate_main_and_tab(output_dir: Path):
    """01.pngからmain画像(240x240)とtab画像(96x74)を生成する。
    LINE Creators Marketの必須画像。透過PNG。
    """
    output_dir = Path(output_dir)
    source = output_dir / "01.png"
    if not source.exists():
        print("警告: 01.pngが見つかりません。main/tab画像を生成できません。")
        return

    img = Image.open(source).convert("RGBA")

    # main.png: 240x240 - 被写体を中央配置
    main_img = _fit_to_canvas(img, MAIN_IMAGE_SIZE[0], MAIN_IMAGE_SIZE[1])
    main_path = output_dir / "main.png"
    main_img.save(main_path)
    print(f"main.png 生成完了: {MAIN_IMAGE_SIZE[0]}x{MAIN_IMAGE_SIZE[1]}")

    # tab.png: 96x74 - 被写体を中央配置
    tab_img = _fit_to_canvas(img, TAB_IMAGE_SIZE[0], TAB_IMAGE_SIZE[1])
    tab_path = output_dir / "tab.png"
    tab_img.save(tab_path)
    print(f"tab.png 生成完了: {TAB_IMAGE_SIZE[0]}x{TAB_IMAGE_SIZE[1]}")


def _fit_to_canvas(img: Image.Image, canvas_w: int, canvas_h: int) -> Image.Image:
    """画像を指定サイズのキャンバスに収まるようリサイズして中央配置する。"""
    # 透明部分をトリミングして被写体だけ取得
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)

    # マージン（4px）を確保してリサイズ
    margin = 4
    max_w = canvas_w - margin * 2
    max_h = canvas_h - margin * 2

    # アスペクト比を維持してリサイズ
    ratio = min(max_w / img.width, max_h / img.height)
    new_w = int(img.width * ratio)
    new_h = int(img.height * ratio)
    resized = img.resize((new_w, new_h), Image.LANCZOS)

    # 透明キャンバスに中央配置
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    x = (canvas_w - new_w) // 2
    y = (canvas_h - new_h) // 2
    canvas.paste(resized, (x, y), resized)
    return canvas
