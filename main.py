#!/usr/bin/env python3
"""
LINEスタンプ自動でつくるくん

ペット画像8枚からLINE Creators Market用スタンプを自動生成する。
"""

import json
import random
import sys
from pathlib import Path

from scripts.process_images import process_all_images
from scripts.make_preview import make_preview
from scripts.zip_output import create_zip

PROJECT_ROOT = Path(__file__).resolve().parent
TEMPLATES_PATH = PROJECT_ROOT / "prompts" / "message_templates.json"
INPUT_DIR = PROJECT_ROOT / "input_images"
OUTPUT_DIR = PROJECT_ROOT / "output"


def load_templates() -> list[str]:
    """メッセージテンプレートを読み込む。"""
    with open(TEMPLATES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data["templates"]


def select_message_mode() -> str:
    """メッセージモードを選択させる。"""
    print("\n" + "=" * 50)
    print("メッセージモードを選択してください")
    print("=" * 50)
    print("  A: メッセージランダム生成")
    print("  B: テンプレートから選択")
    print("  C: ユーザー自由入力")
    print("  D: メッセージなし（画像のみ）")
    print()

    while True:
        mode = input("モードを入力 (A/B/C/D): ").strip().upper()
        if mode in ("A", "B", "C", "D"):
            return mode
        print("A, B, C, D のいずれかを入力してください。")


def get_messages_mode_a(templates: list[str]) -> list[str]:
    """モードA: テンプレートからランダムに8個選択。"""
    selected = random.sample(templates, min(8, len(templates)))
    print("\n選択されたメッセージ:")
    for i, msg in enumerate(selected, 1):
        print(f"  {i}. {msg}")
    return selected


def get_messages_mode_b(templates: list[str]) -> list[str]:
    """モードB: ユーザーがテンプレートから8個選択。"""
    print("\nテンプレート一覧:")
    for i, msg in enumerate(templates, 1):
        print(f"  {i:2d}. {msg}")

    print("\n8個の番号をスペース区切りで入力してください。")
    print("例: 1 3 5 7 9 10 11 12")

    while True:
        try:
            nums = input("番号: ").strip().split()
            if len(nums) != 8:
                print(f"8個選択してください（現在{len(nums)}個）。")
                continue
            indices = [int(n) - 1 for n in nums]
            if any(i < 0 or i >= len(templates) for i in indices):
                print(f"1〜{len(templates)}の範囲で入力してください。")
                continue
            selected = [templates[i] for i in indices]
            print("\n選択されたメッセージ:")
            for i, msg in enumerate(selected, 1):
                print(f"  {i}. {msg}")
            return selected
        except ValueError:
            print("数字で入力してください。")


def get_messages_mode_c() -> list[str]:
    """モードC: ユーザーが8個のメッセージを自由入力。"""
    print("\n8個のメッセージを入力してください。")
    messages = []
    for i in range(1, 9):
        while True:
            msg = input(f"  メッセージ {i}: ").strip()
            if msg:
                messages.append(msg)
                break
            print("  メッセージを入力してください。")
    return messages


def get_messages_mode_d() -> list[None]:
    """モードD: メッセージなし。"""
    return [None] * 8


def check_input_images() -> bool:
    """入力画像が8枚あるか確認する。"""
    valid_extensions = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp"}
    image_files = [
        f for f in INPUT_DIR.iterdir() if f.suffix.lower() in valid_extensions
    ] if INPUT_DIR.exists() else []

    if len(image_files) < 8:
        print(f"\nエラー: input_images/ に画像が{len(image_files)}枚しかありません。")
        print("8枚のペット画像を input_images/ フォルダに配置してください。")
        print(f"\n対応形式: {', '.join(sorted(valid_extensions))}")
        return False

    print(f"\n入力画像: {len(image_files)}枚確認済み")
    for f in sorted(image_files)[:8]:
        print(f"  - {f.name}")
    return True


def run_quality_check(output_dir: Path) -> bool:
    """品質チェックを実行する。"""
    print("\n" + "=" * 50)
    print("品質チェック")
    print("=" * 50)

    from PIL import Image
    import numpy as np

    all_ok = True
    for i in range(1, 9):
        path = output_dir / f"{i:02d}.png"
        if not path.exists():
            print(f"  NG: {path.name} が存在しません")
            all_ok = False
            continue

        img = Image.open(path)
        checks = []

        # サイズチェック
        if img.size == (370, 320):
            checks.append("サイズOK")
        else:
            checks.append(f"サイズNG ({img.size[0]}x{img.size[1]})")
            all_ok = False

        # 透過チェック
        if img.mode == "RGBA":
            alpha = np.array(img)[:, :, 3]
            transparent_ratio = np.sum(alpha == 0) / alpha.size
            if transparent_ratio > 0.05:
                checks.append(f"透過OK ({transparent_ratio:.0%})")
            else:
                checks.append("透過NG（背景が残っている可能性）")
                all_ok = False
        else:
            checks.append("透過NG（RGBAモードではありません）")
            all_ok = False

        # 画質チェック（ファイルサイズ）
        file_size = path.stat().st_size / 1024
        if file_size > 5:
            checks.append(f"画質OK ({file_size:.0f}KB)")
        else:
            checks.append("画質NG（ファイルサイズが小さすぎます）")
            all_ok = False

        status = "OK" if all(c.endswith("OK") or "OK" in c for c in checks) else "要確認"
        print(f"  {path.name}: [{status}] {' / '.join(checks)}")

    return all_ok


def main():
    print("=" * 50)
    print("  LINEスタンプ自動でつくるくん")
    print("=" * 50)

    # 入力画像チェック
    if not check_input_images():
        sys.exit(1)

    # メッセージモード選択
    mode = select_message_mode()
    templates = load_templates()

    if mode == "A":
        messages = get_messages_mode_a(templates)
    elif mode == "B":
        messages = get_messages_mode_b(templates)
    elif mode == "C":
        messages = get_messages_mode_c()
    else:
        messages = get_messages_mode_d()

    # 確認
    print("\n" + "-" * 50)
    print("以下の設定でスタンプを生成します。")
    print(f"  モード: {mode}")
    print(f"  出力先: {OUTPUT_DIR}/")
    confirm = input("\n続行しますか？ (y/n): ").strip().lower()
    if confirm != "y":
        print("キャンセルしました。")
        sys.exit(0)

    # 画像処理
    print("\n" + "=" * 50)
    print("スタンプ生成開始")
    print("=" * 50)

    process_all_images(INPUT_DIR, OUTPUT_DIR, messages)

    # 品質チェック
    run_quality_check(OUTPUT_DIR)

    # プレビュー生成
    print("\n" + "-" * 50)
    make_preview(OUTPUT_DIR)

    # ZIP生成
    create_zip(OUTPUT_DIR)

    print("\n" + "=" * 50)
    print("全処理完了！")
    print("=" * 50)
    print(f"\n出力ファイル:")
    print(f"  スタンプ:   {OUTPUT_DIR}/01.png 〜 08.png")
    print(f"  プレビュー: {OUTPUT_DIR}/preview.png")
    print(f"  ZIP:        {OUTPUT_DIR}/line_stamp.zip")
    print(f"\nLINE Creators Market にアップロードしてください。")


if __name__ == "__main__":
    main()
