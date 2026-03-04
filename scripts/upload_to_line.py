"""
LINE Creators Market 自動アップロードスクリプト

Playwrightでブラウザを開き、ログイン→スタンプ作成→画像アップロードまで自動化。
ログインはユーザーが手動で行い、その後の操作をすべて自動化する。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, TimeoutError as PwTimeout


CREATOR_URL = "https://creator.line.me/ja/"
LOGIN_URL = "https://creator.line.me/signup/line_auth"
DASHBOARD_URL = "https://creator.line.me/studio/app/folder"


class UploadStatus:
    """Web UIにステータスを伝えるためのファイルベースのステータス管理。"""

    def __init__(self, status_file: Optional[Path] = None):
        self.status_file = status_file
        self.logs: list[str] = []

    def update(self, step: str, message: str, progress: int = 0):
        self.logs.append(f"[{step}] {message}")
        print(f"  [{step}] {message}")
        if self.status_file:
            data = {
                "step": step,
                "message": message,
                "progress": progress,
                "logs": self.logs[-20:],  # 直近20件
            }
            self.status_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _is_logged_in(page: Page) -> bool:
    """ページ上のログイン状態を判定する。"""
    url = page.url
    # studio ダッシュボードにいる
    if "/studio/" in url:
        return True
    # LINE OAuth コールバック後のリダイレクト
    if "/signup/line_callback" in url:
        return True
    # ログアウトリンクやナビ要素が存在する（＝ログイン済み）
    logged_in_selectors = [
        "a[href*='logout']",
        "a[href*='/studio/']",
        "a:has-text('マイページ')",
        "a:has-text('My page')",
        "a:has-text('ログアウト')",
        "a:has-text('Log Out')",
        "[class*='logout']",
        "[class*='user-icon']",
        "[class*='avatar']",
    ]
    for sel in logged_in_selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=500):
                return True
        except Exception:
            continue
    return False


def wait_for_login(page: Page, status: UploadStatus, timeout: int = 300) -> bool:
    """ユーザーがログインするまで待機。"""
    status.update("ログイン", "ブラウザが開きます。LINEアカウントでログインしてください。", 5)

    # LINE OAuth ログインページに直接アクセス
    page.goto(LOGIN_URL)
    time.sleep(3)

    # 既にログイン済みか確認（ダッシュボードにリダイレクトされている場合）
    if _is_logged_in(page):
        status.update("ログイン", "既にログイン済みです。", 15)
        return True

    status.update("ログイン", f"ログイン待機中... ({timeout}秒以内にログインしてください)", 10)

    # ポーリングでログイン完了を検出
    elapsed = 0
    interval = 3
    while elapsed < timeout:
        time.sleep(interval)
        elapsed += interval

        current_url = page.url

        # studio ダッシュボードにいる ＝ ログイン済み
        if "/studio/" in current_url:
            status.update("ログイン", "ログイン完了！", 20)
            time.sleep(2)
            return True

        # OAuth コールバック後のリダイレクト
        if "/signup/line_callback" in current_url:
            status.update("ログイン", "認証コールバック検出、リダイレクト待機中...", 15)
            time.sleep(5)
            continue

        # creator.line.me に戻ってきた場合
        if "creator.line.me" in current_url and "access.line.me" not in current_url:
            if _is_logged_in(page):
                status.update("ログイン", "ログイン完了！ダッシュボードに移動中...", 20)
                page.goto(DASHBOARD_URL, timeout=15000)
                page.wait_for_load_state("networkidle", timeout=15000)
                time.sleep(2)
                return True

    status.update("エラー", "タイムアウト: ログインが完了しませんでした。")
    return False


def _log_page_links(page: Page, status: UploadStatus):
    """ページ上のリンクとボタンをステータスに記録（デバッグ用）。"""
    try:
        items = []
        for el in page.locator("a[href], button").all()[:30]:
            text = (el.text_content() or "").strip()
            href = el.get_attribute("href") or ""
            tag = el.evaluate("e => e.tagName")
            if text and len(text) < 50:
                items.append(f"[{tag}] {text} → {href}" if href else f"[{tag}] {text}")
        if items:
            status.update("デバッグ", "ページ要素: " + " | ".join(items[:15]))
    except Exception:
        pass


def navigate_to_new_sticker(page: Page, status: UploadStatus) -> bool:
    """スタンプ新規作成ページに遷移する。"""
    status.update("ページ遷移", "ダッシュボードに移動中...", 25)

    # studio ダッシュボードに移動
    page.goto(DASHBOARD_URL, timeout=15000)
    page.wait_for_load_state("networkidle", timeout=15000)
    time.sleep(3)

    current_url = page.url
    status.update("ページ遷移", f"現在のURL: {current_url}", 27)

    # ログインにリダイレクトされた場合はログイン待機
    if "access.line.me" in current_url or "login" in current_url:
        status.update("ページ遷移", "ログインが必要です。ログインしてください。", 27)
        # ログイン後にダッシュボードに戻るまで待機
        try:
            page.wait_for_url("**/studio/**", timeout=120000)
            time.sleep(3)
        except PwTimeout:
            status.update("エラー", "ダッシュボードへの遷移がタイムアウトしました。")
            return False

    current_url = page.url
    status.update("ページ遷移", f"ダッシュボード: {current_url}", 28)

    # ダッシュボードのリンク・ボタンを記録
    _log_page_links(page, status)

    # 「新規登録」「作成」系のリンク・ボタンを探してクリック
    new_btn_selectors = [
        "a:has-text('新規登録')",
        "a:has-text('新規作成')",
        "a:has-text('新しく作る')",
        "button:has-text('新規登録')",
        "button:has-text('新規作成')",
        "a:has-text('Create New')",
        "a:has-text('New Submission')",
        "a:has-text('Create')",
        "a:has-text('新規')",
        "button:has-text('新規')",
        "button:has-text('Create')",
        # studio UIのアイコンボタン
        "[class*='create']",
        "[class*='add-new']",
        "[class*='new-item']",
    ]

    clicked_new = False
    for sel in new_btn_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn_text = (btn.text_content() or "").strip()
                status.update("ページ遷移", f"ボタンをクリック: {btn_text or sel}", 30)
                btn.click()
                time.sleep(3)
                page.wait_for_load_state("networkidle", timeout=10000)
                clicked_new = True
                break
        except Exception:
            continue

    if clicked_new:
        status.update("ページ遷移", f"クリック後のURL: {page.url}", 32)
        _log_page_links(page, status)

    # スタンプタイプ選択画面が出た場合
    sticker_selectors = [
        "a:has-text('スタンプ')",
        "button:has-text('スタンプ')",
        "a:has-text('Sticker')",
        "a:has-text('sticker')",
        "a[href*='sticker']",
        "button:has-text('Sticker')",
    ]

    for sel in sticker_selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el_text = (el.text_content() or "").strip()
                status.update("ページ遷移", f"「スタンプ」を選択: {el_text}", 33)
                el.click()
                time.sleep(3)
                page.wait_for_load_state("networkidle", timeout=10000)
                break
        except Exception:
            continue

    time.sleep(2)
    current_url = page.url
    status.update("ページ遷移", f"最終URL: {current_url}", 35)

    # フォーム要素があれば成功
    form_count = page.locator("input[type='text'], textarea, input[type='file'], select").count()
    if form_count > 0:
        status.update("ページ遷移", f"スタンプ作成ページに到着（フォーム要素: {form_count}個）", 35)
        return True

    # フォームが見つからない場合、ページ内容を記録
    _log_page_links(page, status)
    status.update("ページ遷移", "フォームが見つかりません。ページ要素をログに記録しました。", 30)
    return False


def fill_sticker_info(page: Page, title: str, description: str, status: UploadStatus):
    """スタンプのタイトル・説明文を入力する。"""
    status.update("情報入力", "スタンプ情報を入力中...", 40)

    # フォーム要素を全て取得
    inputs = page.locator("input[type='text']")
    textareas = page.locator("textarea")
    selects = page.locator("select")

    # タイトル入力
    title_filled = False
    try:
        count = inputs.count()
        for i in range(count):
            inp = inputs.nth(i)
            placeholder = (inp.get_attribute("placeholder") or "").lower()
            name = (inp.get_attribute("name") or "").lower()
            label_text = ""
            # 対応するlabelのテキストを確認
            inp_id = inp.get_attribute("id")
            if inp_id:
                label = page.locator(f"label[for='{inp_id}']")
                if label.count() > 0:
                    label_text = (label.first.text_content() or "").lower()

            if any(kw in name for kw in ["title", "name"]) or \
               any(kw in placeholder for kw in ["title", "タイトル", "名前"]) or \
               any(kw in label_text for kw in ["title", "タイトル"]):
                inp.fill(title)
                status.update("情報入力", f"タイトル: {title}", 45)
                title_filled = True
                break

        if not title_filled and count > 0:
            inputs.first.fill(title)
            status.update("情報入力", f"タイトル（最初のinputに入力）: {title}", 45)
    except Exception as e:
        status.update("情報入力", f"タイトル入力失敗: {e}")

    # 説明文入力
    desc_filled = False
    try:
        ta_count = textareas.count()
        for i in range(ta_count):
            ta = textareas.nth(i)
            placeholder = (ta.get_attribute("placeholder") or "").lower()
            name = (ta.get_attribute("name") or "").lower()
            label_text = ""
            ta_id = ta.get_attribute("id")
            if ta_id:
                label = page.locator(f"label[for='{ta_id}']")
                if label.count() > 0:
                    label_text = (label.first.text_content() or "").lower()

            if any(kw in name for kw in ["desc", "detail", "explain"]) or \
               any(kw in placeholder for kw in ["desc", "説明", "詳細"]) or \
               any(kw in label_text for kw in ["desc", "説明"]):
                ta.fill(description)
                status.update("情報入力", f"説明: {description}", 50)
                desc_filled = True
                break

        if not desc_filled and ta_count > 0:
            textareas.first.fill(description)
            status.update("情報入力", f"説明（最初のtextareaに入力）: {description}", 50)
    except Exception as e:
        status.update("情報入力", f"説明文入力失敗: {e}")

    time.sleep(1)


def upload_images(page: Page, output_dir: Path, status: UploadStatus):
    """スタンプ画像を1枚ずつアップロードする。"""
    stamp_files = sorted(output_dir.glob("[0-9][0-9].png"))
    total = len(stamp_files)
    status.update("画像アップ", f"スタンプ画像をアップロード中... ({total}枚)", 55)

    for i, stamp_file in enumerate(stamp_files, 1):
        progress = 55 + int((i / total) * 35)
        try:
            # file input を探す（非表示の場合もある）
            file_inputs = page.locator("input[type='file']")
            fi_count = file_inputs.count()

            if fi_count > 0:
                # 複数のfile inputがある場合、まだ値が設定されていないものを使う
                uploaded = False
                for fi_idx in range(fi_count):
                    fi = file_inputs.nth(fi_idx)
                    current_val = fi.input_value() if fi.is_visible() else ""
                    if not current_val:
                        fi.set_input_files(str(stamp_file))
                        uploaded = True
                        break

                if not uploaded:
                    # 全てに値がある場合は最初のものを使う
                    file_inputs.first.set_input_files(str(stamp_file))

                status.update("画像アップ", f"[{i}/{total}] {stamp_file.name} OK", progress)
                time.sleep(3)
            else:
                # file inputがない場合、アップロードボタンを探す
                upload_btns = page.locator(
                    "button:has-text('アップロード'), "
                    "button:has-text('Upload'), "
                    "a:has-text('アップロード'), "
                    "[class*='upload']"
                )
                if upload_btns.count() > 0:
                    upload_btns.first.click()
                    time.sleep(1)
                    # クリック後にfile inputが出てくる場合
                    file_inputs = page.locator("input[type='file']")
                    if file_inputs.count() > 0:
                        file_inputs.first.set_input_files(str(stamp_file))
                        status.update("画像アップ", f"[{i}/{total}] {stamp_file.name} OK", progress)
                        time.sleep(3)
                    else:
                        status.update("画像アップ", f"[{i}/{total}] {stamp_file.name} - ファイル入力が見つかりません", progress)
                else:
                    status.update("画像アップ", f"[{i}/{total}] {stamp_file.name} - アップロード手段が見つかりません", progress)

        except Exception as e:
            status.update("画像アップ", f"[{i}/{total}] {stamp_file.name} 失敗: {e}", progress)

    status.update("画像アップ", "画像アップロード完了！", 95)


def upload_to_line(
    output_dir: Path,
    title: str = "ペットスタンプ",
    description: str = "かわいいペットのスタンプです",
    interactive: bool = True,
    status_file: Optional[Path] = None,
) -> bool:
    """LINE Creators Marketにスタンプを自動登録する。

    Args:
        output_dir: 01.png〜08.pngが格納されたディレクトリ
        title: スタンプセットのタイトル
        description: スタンプセットの説明文
        interactive: Trueの場合、ターミナルでのinput()待ちを有効にする
        status_file: ステータスを書き出すファイルパス（Web UI用）
    """
    output_dir = Path(output_dir)
    stamp_files = sorted(output_dir.glob("[0-9][0-9].png"))
    status = UploadStatus(status_file)

    if not stamp_files:
        status.update("エラー", f"{output_dir} にスタンプ画像が見つかりません")
        return False

    status.update("開始", f"スタンプ {len(stamp_files)}枚 / タイトル: {title}", 0)

    with sync_playwright() as p:
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
            if not wait_for_login(page, status):
                return False

            # Step 2: スタンプ作成ページへ遷移
            if not navigate_to_new_sticker(page, status):
                status.update("警告", "スタンプ作成ページの自動遷移に失敗。手動で移動してください。")
                if interactive:
                    print("  手動でスタンプ作成ページに移動してください。")
                    print("  移動したらEnterを押してください...")
                    input()
                else:
                    # Web実行時: 30秒待って再チェック
                    status.update("警告", "30秒待機中... 手動でスタンプ作成ページに移動してください。")
                    time.sleep(30)
                    if page.locator("input[type='text'], textarea, input[type='file']").count() == 0:
                        status.update("エラー", "スタンプ作成ページに到達できませんでした。")
                        return False

            # Step 3: スタンプ情報入力
            fill_sticker_info(page, title, description, status)

            # Step 4: 画像アップロード
            upload_images(page, output_dir, status)

            status.update("完了", "自動登録完了！ブラウザで内容を確認してください。", 100)

            if interactive:
                print("\n  以下を確認してください:")
                print("  1. アップロード画像の確認")
                print("  2. 販売価格の設定")
                print("  3.「リクエスト」ボタンで審査提出")
                print("\n  ブラウザを閉じるにはEnterを押してください...")
                input()
            else:
                # Web実行時: ブラウザを開いたまま5分待機
                status.update("完了", "ブラウザで内容を確認し、「リクエスト」を押して審査に出してください。5分後にブラウザが閉じます。", 100)
                time.sleep(300)

            return True

        except KeyboardInterrupt:
            status.update("中断", "ユーザーにより中断されました。")
            return False
        except Exception as e:
            status.update("エラー", f"予期しないエラー: {e}")
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

    upload_to_line(out_dir, args.title, args.desc, interactive=True)
