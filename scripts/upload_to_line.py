"""
LINE Creators Market 自動アップロードスクリプト

Playwrightでブラウザを開き、ログイン→スタンプ作成→画像アップロードまで自動化。
ログインはユーザーが手動で行い、その後の操作をすべて自動化する。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, TimeoutError as PwTimeout


# LINE OAuth ログインエントリーポイント
LOGIN_URL = "https://creator.line.me/signup/line_auth"


class UploadStatus:
    """Web UIにステータスを伝えるためのファイルベースのステータス管理。"""

    def __init__(self, status_file: Optional[Path] = None, debug_dir: Optional[Path] = None):
        self.status_file = status_file
        self.debug_dir = debug_dir
        self.logs: list[str] = []

    def update(self, step: str, message: str, progress: int = 0):
        self.logs.append(f"[{step}] {message}")
        print(f"  [{step}] {message}")
        if self.status_file:
            data = {
                "step": step,
                "message": message,
                "progress": progress,
                "logs": self.logs[-20:],
            }
            self.status_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def save_screenshot(self, page: Page, name: str):
        """デバッグ用スクリーンショットを保存する。"""
        if self.debug_dir:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            path = self.debug_dir / f"debug_{name}.png"
            try:
                page.screenshot(path=str(path), full_page=False)
                self.update("デバッグ", f"スクリーンショット保存: {path.name}")
            except Exception as e:
                self.update("デバッグ", f"スクリーンショット保存失敗: {e}")

    def dump_page_info(self, page: Page, label: str):
        """ページのURL、タイトル、主要要素をログに記録する。"""
        try:
            url = page.url
            title = page.title()
            self.update("デバッグ", f"[{label}] URL={url}")
            self.update("デバッグ", f"[{label}] Title={title}")

            # リンクとボタンを全て記録
            elements = []
            for el in page.locator("a[href], button").all()[:40]:
                try:
                    text = (el.text_content() or "").strip().replace("\n", " ")[:40]
                    href = el.get_attribute("href") or ""
                    tag = el.evaluate("e => e.tagName")
                    visible = el.is_visible()
                    if text:
                        elements.append(f"{'✓' if visible else '✗'} [{tag}] \"{text}\" href={href}")
                except Exception:
                    continue

            for i in range(0, len(elements), 5):
                batch = elements[i:i + 5]
                self.update("デバッグ", f"[{label}] 要素: " + " | ".join(batch))

        except Exception as e:
            self.update("デバッグ", f"[{label}] ページ情報取得失敗: {e}")


def _wait_for_page_ready(page: Page, timeout: int = 10):
    """ページの読み込み完了を待つ。"""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout * 1000)
    except PwTimeout:
        pass
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except PwTimeout:
        pass
    time.sleep(2)


def _is_on_dashboard(page: Page) -> bool:
    """ダッシュボード（/my/配下）にいるか判定。"""
    return "/my/" in page.url


def _is_logged_in_by_elements(page: Page) -> bool:
    """ページ上の要素でログイン済みか判定する（URLに依存しない）。"""
    selectors = [
        # ダッシュボード上のナビ要素
        "a:has-text('アイテム管理')",
        "a:has-text('新規登録')",
        "a:has-text('アカウント設定')",
        "a:has-text('ログアウト')",
        "a:has-text('Log Out')",
        "a[href*='/my/']",
        # トップページ上のナビ要素（ログイン済み時のみ表示）
        "a:has-text('マイページ')",
        "a:has-text('My Page')",
    ]
    for sel in selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=500):
                return True
        except Exception:
            continue
    return False


def _is_on_creator_top_page(url: str) -> bool:
    """creator.line.meのトップページにいるか判定。"""
    if "creator.line.me" not in url:
        return False
    if "access.line.me" in url:
        return False
    if "/my/" in url:
        return False
    if "/signup/" in url:
        return False
    return True


def _find_logged_in_page(context, status: UploadStatus) -> Optional[Page]:
    """コンテキスト内の全ページをスキャンし、ログイン済みのページを探す。

    OAuth ログイン後、元のページにリダイレクトされる場合もあれば、
    新しいタブやポップアップが開く場合もある。全ページを確認する。
    """
    all_pages = context.pages
    if len(all_pages) > 1:
        status.update("デバッグ", f"コンテキスト内のページ数: {len(all_pages)}")

    for p in all_pages:
        try:
            url = p.url
            # ダッシュボードにいるページを発見
            if "/my/" in url:
                status.update("ログイン", f"ダッシュボードページ発見: {url[:100]}", 20)
                return p

            # creator.line.me にいる（ログインページではない）
            if "creator.line.me" in url and "access.line.me" not in url:
                _wait_for_page_ready(p)
                if _is_logged_in_by_elements(p):
                    status.update("ログイン", f"ログイン済みページ発見: {url[:100]}", 20)
                    return p

                # トップページにいる場合、/signup/line_auth に再遷移してダッシュボードリダイレクトを試みる
                if _is_on_creator_top_page(url):
                    status.update("ログイン", f"トップページ検出。ダッシュボードへリダイレクト試行...", 15)
                    try:
                        p.goto(LOGIN_URL, timeout=15000)
                        _wait_for_page_ready(p)
                        new_url = p.url
                        status.update("ログイン", f"リダイレクト先: {new_url[:100]}", 15)
                        if "/my/" in new_url:
                            return p
                        # まだログインページに飛ばされた → 未ログイン
                        if "access.line.me" in new_url:
                            status.update("ログイン", "未ログイン状態。ログインページに戻りました。", 10)
                            continue
                        # creator.line.me のどこかにいる
                        if _is_logged_in_by_elements(p):
                            return p
                    except Exception as e:
                        status.update("デバッグ", f"リダイレクト試行失敗: {e}")

            # OAuth コールバック中のページ
            if "/signup/line_callback" in url:
                status.update("ログイン", f"OAuth コールバック中のページ発見: {url[:80]}", 15)
                # コールバック完了を少し待つ
                try:
                    p.wait_for_url(lambda u: "/my/" in u or ("creator.line.me" in u and "access.line.me" not in u and "/signup/" not in u), timeout=15000)
                    return p
                except PwTimeout:
                    pass
        except Exception:
            continue
    return None


def wait_for_login(page: Page, status: UploadStatus, timeout: int = 300) -> Optional[Page]:
    """ユーザーがログインするまで待機。ログイン後のページを返す（新しいタブの場合もある）。
    失敗時はNoneを返す。"""
    status.update("ログイン", "Playwrightブラウザが開きます。そのブラウザでLINEにログインしてください。", 5)

    context = page.context

    # 新しいページ（ポップアップ・タブ）が開いたら記録する
    new_pages: list[Page] = []

    def on_new_page(new_page: Page):
        url = new_page.url or "(loading)"
        status.update("デバッグ", f"新しいタブ/ポップアップ検出: {url[:100]}")
        new_pages.append(new_page)

    context.on("page", on_new_page)

    # LINE OAuth ログインページに直接アクセス
    page.goto(LOGIN_URL, timeout=30000)
    _wait_for_page_ready(page)

    status.save_screenshot(page, "01_login_page")

    # 既にログイン済みか確認（リダイレクトでダッシュボードに飛んだ場合）
    if _is_on_dashboard(page):
        status.update("ログイン", "既にログイン済みです。", 15)
        return page

    # creator.line.me のトップにリダイレクトされた場合
    if "creator.line.me" in page.url and "access.line.me" not in page.url:
        if _is_logged_in_by_elements(page):
            status.update("ログイン", "既にログイン済みです。", 15)
            return page

    status.update("ログイン", f"⚠️ 新しく開いたブラウザウィンドウ（Chromium）でログインしてください（{timeout}秒以内）", 10)

    last_urls: dict[int, str] = {}
    elapsed = 0
    interval = 3
    while elapsed < timeout:
        time.sleep(interval)
        elapsed += interval

        # 全ページの URL を確認（新しいタブやポップアップも含む）
        for p in context.pages:
            try:
                pid = id(p)
                current_url = p.url
                prev_url = last_urls.get(pid, "")

                if current_url != prev_url:
                    status.update("ログイン", f"URL変化 (page {pid % 1000}): {current_url[:100]}", 10)
                    last_urls[pid] = current_url
            except Exception:
                continue

        # 全ページからログイン済みのものを探す
        logged_in_page = _find_logged_in_page(context, status)
        if logged_in_page:
            _wait_for_page_ready(logged_in_page)
            status.update("ログイン", "ログイン完了！", 20)
            status.save_screenshot(logged_in_page, "02_login_done")

            # ダッシュボードにいない場合、/signup/line_auth に再遷移してダッシュボードリダイレクトを試行
            if "/my/" not in logged_in_page.url:
                status.update("ログイン", "ダッシュボードへ移動中...", 20)
                try:
                    logged_in_page.goto(LOGIN_URL, timeout=15000)
                    _wait_for_page_ready(logged_in_page)
                    status.update("ログイン", f"リダイレクト先: {logged_in_page.url[:100]}", 20)
                except Exception as e:
                    status.update("デバッグ", f"ダッシュボード遷移失敗: {e}")
            return logged_in_page

        # 30秒ごとにリマインド
        if elapsed % 30 == 0 and elapsed < timeout:
            remaining = timeout - elapsed
            status.update("ログイン", f"⚠️ Chromiumブラウザでログインしてください（残り{remaining}秒）", 10)

    # タイムアウト - 最終状態をログに記録
    status.update("エラー", f"タイムアウト: ログインが完了しませんでした。")
    status.update("デバッグ", f"最終ページ数: {len(context.pages)}")
    for i, p in enumerate(context.pages):
        try:
            status.update("デバッグ", f"  Page[{i}] URL: {p.url[:100]}")
        except Exception:
            status.update("デバッグ", f"  Page[{i}] (closed)")
    status.save_screenshot(page, "02_login_timeout")
    return None


def _extract_user_path(page: Page) -> Optional[str]:
    """ダッシュボードURLからユーザーパスを抽出する。
    例: /my/dSCMSb2IRvLYU4oe/sticker/ → /my/dSCMSb2IRvLYU4oe
    """
    match = re.search(r"(/my/[^/]+)", page.url)
    if match:
        return match.group(1)
    return None


def _dismiss_modals(page: Page, status: UploadStatus):
    """ダッシュボード上のモーダルポップアップ（キャンペーン告知等）を閉じる。"""
    close_selectors = [
        "button:has-text('閉じる')",
        "button:has-text('Close')",
        "button.modal-close",
        "[class*='modal'] button",
        "[class*='dialog'] button:has-text('閉じる')",
        "[class*='overlay'] button:has-text('閉じる')",
    ]
    for sel in close_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1000):
                btn.click()
                status.update("デバッグ", f"モーダルを閉じました: {sel}")
                time.sleep(1)
                return
        except Exception:
            continue


def navigate_to_new_sticker(page: Page, status: UploadStatus) -> bool:
    """スタンプ新規作成ページに遷移する。"""
    status.update("ページ遷移", f"新規登録ページに移動中... 現在URL: {page.url[:100]}", 25)

    # ダッシュボードにいない場合、マイページリンクをクリックして移動
    if not _is_on_dashboard(page):
        status.update("ページ遷移", "ダッシュボードに移動中...", 25)
        try:
            mypage_link = page.locator("a[href*='/my/'], a:has-text('マイページ')").first
            if mypage_link.is_visible(timeout=3000):
                mypage_link.click()
                time.sleep(3)
                _wait_for_page_ready(page)
        except Exception:
            pass

        # それでもダッシュボードにいない場合、ログインし直す
        if not _is_on_dashboard(page):
            status.update("ページ遷移", "ログインページからやり直します...", 25)
            page.goto(LOGIN_URL, timeout=30000)
            _wait_for_page_ready(page)
            try:
                page.wait_for_url("**/my/**", timeout=120000)
                _wait_for_page_ready(page)
            except PwTimeout:
                status.update("エラー", f"ダッシュボードに到達できませんでした。URL: {page.url[:100]}")
                status.save_screenshot(page, "03_dashboard_fail")
                return False

    # ページ完全読み込みを待つ（サイドバーのレンダリング完了まで）
    _wait_for_page_ready(page)
    time.sleep(3)

    status.save_screenshot(page, "03_dashboard")
    status.dump_page_info(page, "ダッシュボード")

    # ---- 戦略1: 「新規登録」ボタンを直接クリック ----
    status.update("ページ遷移", "「新規登録」ボタンを探しています...", 27)

    new_reg_clicked = False
    new_reg_href = None

    # まずhrefを取得してから判断する
    try:
        links = page.locator("a").all()
        for link in links:
            try:
                text = (link.text_content() or "").strip()
                href = link.get_attribute("href") or ""
                if "新規登録" in text and link.is_visible():
                    new_reg_href = href
                    status.update("ページ遷移", f"「新規登録」発見: text=\"{text}\" href=\"{href}\"", 28)
                    link.click()
                    time.sleep(3)
                    _wait_for_page_ready(page)
                    new_reg_clicked = True
                    break
            except Exception:
                continue
    except Exception as e:
        status.update("デバッグ", f"リンク探索エラー: {e}")

    if not new_reg_clicked:
        # ---- 戦略2: ユーザーパスから新規登録URLを推測して直接遷移 ----
        user_path = _extract_user_path(page)
        if user_path:
            candidate_urls = [
                f"https://creator.line.me{user_path}/sticker/new/",
                f"https://creator.line.me{user_path}/new/",
                f"https://creator.line.me{user_path}/sticker/new",
            ]
            for url in candidate_urls:
                status.update("ページ遷移", f"URL直接遷移を試行: {url}", 28)
                page.goto(url, timeout=15000)
                _wait_for_page_ready(page)
                # 404やエラーページでないか確認
                page_text = page.text_content("body") or ""
                if "存在しません" not in page_text and "404" not in page.title():
                    form_count = page.locator("input, textarea, select").count()
                    if form_count > 0:
                        new_reg_clicked = True
                        status.update("ページ遷移", f"URL直接遷移成功: {url}", 30)
                        break

    if not new_reg_clicked:
        status.save_screenshot(page, "03_new_reg_failed")
        status.dump_page_info(page, "新規登録失敗")
        status.update("エラー", "「新規登録」ボタンが見つかりません。デバッグスクリーンショットを確認してください。")
        return False

    status.save_screenshot(page, "04_after_new_reg_click")
    status.update("ページ遷移", f"新規登録クリック後: {page.url}", 30)
    status.dump_page_info(page, "新規登録後")

    # ---- スタンプタイプ選択 ----
    # 「スタンプ」を選ぶ必要がある場合（スタンプ/絵文字/着せかえ選択画面）
    time.sleep(2)
    try:
        sticker_link = page.locator("a").filter(has_text="スタンプ").first
        if sticker_link.is_visible(timeout=3000):
            href = sticker_link.get_attribute("href") or ""
            text = (sticker_link.text_content() or "").strip()
            # 「スタンプ」タブ（既に選択済み）でなく、遷移リンクの場合のみクリック
            if href and "sticker" in href.lower():
                status.update("ページ遷移", f"「{text}」を選択 (href={href})", 32)
                sticker_link.click()
                time.sleep(3)
                _wait_for_page_ready(page)
                status.save_screenshot(page, "05_after_sticker_select")
    except Exception:
        pass

    current_url = page.url
    status.update("ページ遷移", f"最終URL: {current_url}", 35)

    # フォーム要素があれば成功
    form_count = page.locator("input[type='text'], textarea, input[type='file'], select").count()
    if form_count > 0:
        status.update("ページ遷移", f"スタンプ作成ページに到着（フォーム要素: {form_count}個）", 35)
        status.save_screenshot(page, "05_form_found")
        return True

    # フォームがなくても、ページにコンテンツがあれば進む（SPAの場合）
    status.save_screenshot(page, "05_no_form")
    status.dump_page_info(page, "フォーム未検出")
    status.update("ページ遷移", "フォームが見つかりません。デバッグ情報を記録しました。", 30)
    return False


def fill_sticker_info(page: Page, title: str, description: str, status: UploadStatus):
    """スタンプのタイトル・説明文を入力する。"""
    status.update("情報入力", "スタンプ情報を入力中...", 40)
    status.save_screenshot(page, "06_before_fill")

    inputs = page.locator("input[type='text']")
    textareas = page.locator("textarea")

    # タイトル入力
    try:
        count = inputs.count()
        status.update("情報入力", f"テキスト入力欄: {count}個", 42)
        title_filled = False
        for i in range(count):
            inp = inputs.nth(i)
            if not inp.is_visible():
                continue
            placeholder = (inp.get_attribute("placeholder") or "").lower()
            name = (inp.get_attribute("name") or "").lower()
            inp_id = inp.get_attribute("id") or ""

            label_text = ""
            if inp_id:
                label = page.locator(f"label[for='{inp_id}']")
                if label.count() > 0:
                    label_text = (label.first.text_content() or "").lower()

            status.update("デバッグ", f"input[{i}] name={name} placeholder={placeholder} label={label_text}")

            if any(kw in name for kw in ["title", "name"]) or \
               any(kw in placeholder for kw in ["title", "タイトル", "名前"]) or \
               any(kw in label_text for kw in ["title", "タイトル"]):
                inp.fill(title)
                status.update("情報入力", f"タイトル入力: {title}", 45)
                title_filled = True
                break

        if not title_filled and count > 0:
            # 最初の visible input に入力
            for i in range(count):
                if inputs.nth(i).is_visible():
                    inputs.nth(i).fill(title)
                    status.update("情報入力", f"タイトル（最初のinput）: {title}", 45)
                    break
    except Exception as e:
        status.update("情報入力", f"タイトル入力失敗: {e}")

    # 説明文入力
    try:
        ta_count = textareas.count()
        status.update("情報入力", f"テキストエリア: {ta_count}個", 47)
        desc_filled = False
        for i in range(ta_count):
            ta = textareas.nth(i)
            if not ta.is_visible():
                continue
            placeholder = (ta.get_attribute("placeholder") or "").lower()
            name = (ta.get_attribute("name") or "").lower()

            if any(kw in name for kw in ["desc", "detail", "explain"]) or \
               any(kw in placeholder for kw in ["desc", "説明", "詳細"]):
                ta.fill(description)
                status.update("情報入力", f"説明入力: {description}", 50)
                desc_filled = True
                break

        if not desc_filled and ta_count > 0:
            for i in range(ta_count):
                if textareas.nth(i).is_visible():
                    textareas.nth(i).fill(description)
                    status.update("情報入力", f"説明（最初のtextarea）: {description}", 50)
                    break
    except Exception as e:
        status.update("情報入力", f"説明文入力失敗: {e}")

    time.sleep(1)
    status.save_screenshot(page, "07_after_fill")


def upload_images(page: Page, output_dir: Path, status: UploadStatus):
    """スタンプ画像を1枚ずつアップロードする。"""
    stamp_files = sorted(output_dir.glob("[0-9][0-9].png"))
    total = len(stamp_files)
    status.update("画像アップ", f"スタンプ画像をアップロード中... ({total}枚)", 55)

    for i, stamp_file in enumerate(stamp_files, 1):
        progress = 55 + int((i / total) * 35)
        try:
            file_inputs = page.locator("input[type='file']")
            fi_count = file_inputs.count()
            status.update("画像アップ", f"[{i}/{total}] file input数: {fi_count}", progress)

            if fi_count > 0:
                uploaded = False
                for fi_idx in range(fi_count):
                    fi = file_inputs.nth(fi_idx)
                    try:
                        current_val = fi.input_value()
                    except Exception:
                        current_val = ""
                    if not current_val:
                        fi.set_input_files(str(stamp_file))
                        uploaded = True
                        break

                if not uploaded:
                    file_inputs.first.set_input_files(str(stamp_file))

                status.update("画像アップ", f"[{i}/{total}] {stamp_file.name} OK", progress)
                time.sleep(3)
            else:
                # file inputがない場合、アップロードボタンを探す
                upload_btns = page.locator(
                    "button:has-text('アップロード'), "
                    "button:has-text('Upload'), "
                    "a:has-text('アップロード')"
                )
                if upload_btns.count() > 0:
                    upload_btns.first.click()
                    time.sleep(2)
                    file_inputs = page.locator("input[type='file']")
                    if file_inputs.count() > 0:
                        file_inputs.first.set_input_files(str(stamp_file))
                        status.update("画像アップ", f"[{i}/{total}] {stamp_file.name} OK", progress)
                        time.sleep(3)
                    else:
                        status.update("画像アップ", f"[{i}/{total}] ファイル入力が見つかりません", progress)
                else:
                    status.update("画像アップ", f"[{i}/{total}] アップロード手段なし", progress)

        except Exception as e:
            status.update("画像アップ", f"[{i}/{total}] {stamp_file.name} 失敗: {e}", progress)

    status.save_screenshot(page, "08_after_upload")
    status.update("画像アップ", "画像アップロード完了！", 95)


def upload_to_line(
    output_dir: Path,
    title: str = "ペットスタンプ",
    description: str = "かわいいペットのスタンプです",
    interactive: bool = True,
    status_file: Optional[Path] = None,
) -> bool:
    """LINE Creators Marketにスタンプを自動登録する。"""
    output_dir = Path(output_dir)
    stamp_files = sorted(output_dir.glob("[0-9][0-9].png"))

    # デバッグ用ディレクトリ
    debug_dir = output_dir / "debug"
    status = UploadStatus(status_file, debug_dir)

    if not stamp_files:
        status.update("エラー", f"{output_dir} にスタンプ画像が見つかりません")
        return False

    status.update("開始", f"スタンプ {len(stamp_files)}枚 / タイトル: {title}", 0)

    # 永続的なブラウザプロファイルを使用（ログインセッションが保存される）
    project_root = Path(__file__).resolve().parent.parent
    user_data_dir = project_root / ".browser_data"
    user_data_dir.mkdir(exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            locale="ja-JP",
            no_viewport=True,
            args=["--start-maximized"],
        )
        page = context.new_page()

        try:
            # Step 1: ログイン（ログイン後のページが返る。新しいタブの場合もある）
            logged_in_page = wait_for_login(page, status)
            if logged_in_page is None:
                return False
            # ログイン後のページを使う（元のpageとは異なる場合がある）
            page = logged_in_page

            # Step 2: モーダルポップアップがあれば閉じる
            _dismiss_modals(page, status)

            # Step 3: スタンプ作成ページへ遷移
            if not navigate_to_new_sticker(page, status):
                status.update("警告", "自動遷移に失敗。手動で操作してください。")
                if interactive:
                    print("  手動でスタンプ作成ページに移動してください。")
                    print("  移動したらEnterを押してください...")
                    input()
                else:
                    status.update("警告", "60秒待機中... 手動でスタンプ作成ページに移動してください。")
                    time.sleep(60)
                    form_count = page.locator("input[type='text'], textarea, input[type='file']").count()
                    if form_count == 0:
                        status.update("エラー", "スタンプ作成ページに到達できませんでした。")
                        status.save_screenshot(page, "99_final_error")
                        status.dump_page_info(page, "最終状態")
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
                status.update("完了", "ブラウザで内容を確認し、「リクエスト」を押して審査に出してください。5分後にブラウザが閉じます。", 100)
                time.sleep(300)

            return True

        except KeyboardInterrupt:
            status.update("中断", "ユーザーにより中断されました。")
            return False
        except Exception as e:
            status.update("エラー", f"予期しないエラー: {e}")
            status.save_screenshot(page, "99_exception")
            return False
        finally:
            context.close()


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
