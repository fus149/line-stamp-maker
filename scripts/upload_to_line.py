"""
LINE Creators Market 自動アップロードスクリプト

Playwrightでブラウザを開き、ログイン→スタンプ作成→画像アップロードまで自動化。
ログインはユーザーが手動で行い、その後の操作をすべて自動化する。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, TimeoutError as PwTimeout

CREATOR_URL = "https://creator.line.me/ja/"
MYPAGE_URL = "https://creator.line.me/ja/mypage/"


def wait_for_login(page: Page, timeout: int = 300):
    """ユーザーがログインするまで待機。"""
    print("\n  ブラウザが開きます。LINEアカウントでログインしてください。")
    print(f"  （{timeout}秒以内にログインしてください）\n")

    page.goto(CREATOR_URL)
    time.sleep(3)

    # 既にログイン済みか確認
    if "/mypage/" in page.url:
        print("  既にログイン済みです。")
        return True

    # ログインボタンを探してクリック
    try:
        login_link = page.locator("a[href*='login'], a:has-text('ログイン'), a:has-text('Log In')").first
        if login_link.is_visible(timeout=3000):
            login_link.click()
            time.sleep(2)
    except Exception:
        pass

    # マイページ遷移（＝ログイン完了）を待機
    try:
        page.wait_for_url("**/mypage/**", timeout=timeout * 1000)
        print("  ログイン完了！\n")
        time.sleep(2)
        return True
    except PwTimeout:
        print("  タイムアウト: ログインが完了しませんでした。")
        return False


def navigate_to_new_sticker(page: Page) -> bool:
    """スタンプ新規作成ページに遷移する。"""
    print("  スタンプ新規作成ページに移動中...")

    # マイページからスタンプ作成へ
    page.goto("https://creator.line.me/ja/mypage/sticker/new/")
    page.wait_for_load_state("networkidle")
    time.sleep(3)

    # ページが正しく読み込まれたか確認
    if "sticker" in page.url.lower() or "new" in page.url.lower():
        print("  スタンプ作成ページに到着。")
        return True

    # フォールバック: マイページから遷移を試みる
    page.goto(MYPAGE_URL)
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    try:
        new_btn = page.locator("a:has-text('新規登録'), a:has-text('Create New'), button:has-text('新規')").first
        if new_btn.is_visible(timeout=5000):
            new_btn.click()
            time.sleep(2)

        sticker_opt = page.locator("a:has-text('スタンプ'), a:has-text('Sticker')").first
        if sticker_opt.is_visible(timeout=5000):
            sticker_opt.click()
            time.sleep(2)

        print("  スタンプ作成ページに到着。")
        return True
    except Exception as e:
        print(f"  遷移に失敗: {e}")
        return False


def fill_sticker_info(page: Page, title: str, description: str):
    """スタンプのタイトル・説明文を入力する。"""
    print("  スタンプ情報を入力中...")

    # フォーム要素を取得して入力
    # LINE Creators Market のフォーム構造に合わせる
    inputs = page.locator("input[type='text']")
    textareas = page.locator("textarea")

    # タイトル入力（最初のtext input）
    try:
        count = inputs.count()
        for i in range(count):
            inp = inputs.nth(i)
            placeholder = inp.get_attribute("placeholder") or ""
            name = inp.get_attribute("name") or ""
            if "title" in name.lower() or "タイトル" in placeholder:
                inp.fill(title)
                print(f"    タイトル: {title}")
                break
        else:
            # フォールバック: 最初のinputに入力
            if count > 0:
                inputs.first.fill(title)
                print(f"    タイトル（推定）: {title}")
    except Exception as e:
        print(f"    タイトル入力失敗: {e}")

    # 説明文入力
    try:
        ta_count = textareas.count()
        for i in range(ta_count):
            ta = textareas.nth(i)
            placeholder = ta.get_attribute("placeholder") or ""
            name = ta.get_attribute("name") or ""
            if "desc" in name.lower() or "説明" in placeholder:
                ta.fill(description)
                print(f"    説明: {description}")
                break
        else:
            if ta_count > 0:
                textareas.first.fill(description)
                print(f"    説明（推定）: {description}")
    except Exception as e:
        print(f"    説明文入力失敗: {e}")

    time.sleep(1)


def upload_images(page: Page, output_dir: Path):
    """スタンプ画像を1枚ずつアップロードする。"""
    stamp_files = sorted(output_dir.glob("[0-9][0-9].png"))
    total = len(stamp_files)
    print(f"\n  スタンプ画像をアップロード中... ({total}枚)")

    for i, stamp_file in enumerate(stamp_files, 1):
        try:
            # file input を探す
            file_inputs = page.locator("input[type='file']")
            if file_inputs.count() > 0:
                file_inputs.first.set_input_files(str(stamp_file))
                print(f"    [{i}/{total}] {stamp_file.name} OK")
                time.sleep(3)  # アップロード処理を待つ
            else:
                print(f"    [{i}/{total}] {stamp_file.name} - ファイル入力が見つかりません")
        except Exception as e:
            print(f"    [{i}/{total}] {stamp_file.name} 失敗: {e}")

    print("  画像アップロード完了！\n")


def upload_to_line(
    output_dir: Path,
    title: str = "ペットスタンプ",
    description: str = "かわいいペットのスタンプです",
):
    """LINE Creators Marketにスタンプを自動登録する。

    Args:
        output_dir: 01.png〜08.pngが格納されたディレクトリ
        title: スタンプセットのタイトル
        description: スタンプセットの説明文
    """
    output_dir = Path(output_dir)
    stamp_files = sorted(output_dir.glob("[0-9][0-9].png"))

    if not stamp_files:
        print(f"エラー: {output_dir} にスタンプ画像が見つかりません")
        return False

    print("=" * 50)
    print("  LINE Creators Market 自動登録")
    print("=" * 50)
    print(f"  スタンプ:  {len(stamp_files)}枚")
    print(f"  タイトル:  {title}")
    print(f"  説明:      {description}")
    print(f"  画像元:    {output_dir}")
    print()

    with sync_playwright() as p:
        # 可視ブラウザを起動（ユーザーがログインするため）
        browser = p.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        context = browser.new_context(
            locale="ja-JP",
            no_viewport=True,
        )
        page = context.new_page()

        try:
            # Step 1: ログイン
            print("[Step 1/4] ログイン")
            if not wait_for_login(page):
                return False

            # Step 2: スタンプ作成ページへ遷移
            print("[Step 2/4] スタンプ作成ページ")
            if not navigate_to_new_sticker(page):
                print("  手動でスタンプ作成ページに移動してください。")
                print("  移動したらEnterを押してください...")
                input()

            # Step 3: スタンプ情報入力
            print("[Step 3/4] スタンプ情報入力")
            fill_sticker_info(page, title, description)

            # Step 4: 画像アップロード
            print("[Step 4/4] 画像アップロード")
            upload_images(page, output_dir)

            print("=" * 50)
            print("  自動登録完了！")
            print("=" * 50)
            print()
            print("  以下を確認してください:")
            print("  1. アップロード画像の確認")
            print("  2. 販売価格の設定")
            print("  3.「リクエスト」ボタンで審査提出")
            print()
            print("  ブラウザを閉じるにはEnterを押してください...")
            input()
            return True

        except KeyboardInterrupt:
            print("\n中断しました。")
            return False
        finally:
            browser.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="LINE Creators Marketにスタンプを自動登録")
    parser.add_argument("output_dir", nargs="?", default=None, help="スタンプ画像のディレクトリ")
    parser.add_argument("--title", default="ペットスタンプ", help="スタンプタイトル")
    parser.add_argument("--desc", default="かわいいペットのスタンプです", help="スタンプ説明文")
    args = parser.parse_args()

    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        project_root = Path(__file__).resolve().parent.parent
        sessions_dir = project_root / "sessions"
        out_dir = None
        if sessions_dir.exists():
            for sd in sorted(sessions_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                candidate = sd / "output"
                if candidate.exists() and list(candidate.glob("[0-9][0-9].png")):
                    out_dir = candidate
                    break
        if out_dir is None:
            out_dir = project_root / "output"

    upload_to_line(out_dir, args.title, args.desc)
