"""
プレビュー画像生成モジュール

8枚のスタンプを並べた確認用画像を生成する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image

STAMP_WIDTH = 370
STAMP_HEIGHT = 320
COLS = 4
ROWS = 2
PADDING = 20
BG_COLOR = (240, 240, 240, 255)


def make_preview(output_dir: str | Path, preview_path: str | Path | None = None) -> Path:
    """8枚のスタンプ画像を4x2グリッドに並べたプレビューを生成する。

    Args:
        output_dir: 01.png〜08.pngが格納されたディレクトリ
        preview_path: プレビュー画像の保存先（省略時は output_dir/preview.png）

    Returns:
        プレビュー画像のパス
    """
    output_dir = Path(output_dir)
    if preview_path is None:
        preview_path = output_dir / "preview.png"
    else:
        preview_path = Path(preview_path)

    # プレビューキャンバスサイズ
    canvas_w = COLS * STAMP_WIDTH + (COLS + 1) * PADDING
    canvas_h = ROWS * STAMP_HEIGHT + (ROWS + 1) * PADDING

    # 市松模様風の背景（透過がわかりやすいように）
    canvas = Image.new("RGBA", (canvas_w, canvas_h), BG_COLOR)

    for i in range(8):
        stamp_path = output_dir / f"{i + 1:02d}.png"
        if not stamp_path.exists():
            print(f"警告: {stamp_path.name} が見つかりません。スキップします。")
            continue

        stamp = Image.open(stamp_path).convert("RGBA")

        col = i % COLS
        row = i // COLS
        x = PADDING + col * (STAMP_WIDTH + PADDING)
        y = PADDING + row * (STAMP_HEIGHT + PADDING)

        # 個別スタンプの背景（白）
        cell_bg = Image.new("RGBA", (STAMP_WIDTH, STAMP_HEIGHT), (255, 255, 255, 255))
        cell_bg.paste(stamp, (0, 0), stamp)
        canvas.paste(cell_bg, (x, y))

    canvas.save(str(preview_path), "PNG")
    print(f"\nプレビュー生成完了: {preview_path}")
    return preview_path
