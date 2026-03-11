"""
LINE Creators Market 自動アップロードスクリプト

Playwrightでブラウザを開き、ログイン→スタンプ作成→画像アップロードまで自動化。
ログインはユーザーが手動で行い、その後の操作をすべて自動化する。

永続的なブラウザプロファイルを使用するため、一度ログインすれば次回以降は
セッションが保持される。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PwTimeout


# LINE OAuth ログインエントリーポイント
LOGIN_URL = "https://creator.line.me/signup/line_auth"


class UploadStatus:
    """Web UIにステータスを伝えるためのファイルベースのステータス管理。"""

    def __init__(self, status_file: Optional[Path] = None, debug_dir: Optional[Path] = None):
        self.status_file = status_file
        self.debug_dir = debug_dir
        self.logs: list[str] = []

    def update(self, step: str, message: str, progress: int = 0, extra: dict = None):
        print(f"  [{step}] {message}")
        # 「デバッグ」「警告」ステップはターミナルのみ表示（お客様には見せない）
        if step in ("デバッグ", "警告"):
            return
        # お客様に見せるログのみ蓄積（デバッグ・警告は除外済み）
        self.logs.append(f"[{step}] {message}")
        if self.status_file:
            data = {
                "step": step,
                "message": message,
                "progress": progress,
                "logs": self.logs[-100:],
            }
            if extra:
                data.update(extra)
            self.status_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def save_screenshot(self, page: Page, name: str):
        """デバッグ用スクリーンショットを保存する。
        ブラウザ最小化時はスクリーンショットがタイムアウトするため、
        短いタイムアウト(5秒)を設定して処理を止めないようにする。
        """
        if self.debug_dir:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            path = self.debug_dir / f"debug_{name}.png"
            try:
                page.screenshot(path=str(path), full_page=False, timeout=5000)
                self.update("デバッグ", f"スクリーンショット保存: {path.name}")
            except Exception:
                # ブラウザ最小化時等、スクリーンショットが取れない場合は静かにスキップ
                pass

    def dump_page_info(self, page: Page, label: str):
        """ページのURL、タイトル、主要要素をログに記録する。"""
        try:
            url = page.evaluate("window.location.href")
            title = page.title()
            self.update("デバッグ", f"[{label}] URL={url}")
            self.update("デバッグ", f"[{label}] Title={title}")

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


# ============================================================
# ページ状態判定ヘルパー
# ============================================================

def _hide_browser(page: Page, status: UploadStatus):
    """Chromiumブラウザウィンドウを完全に隠す（Dock最小化）。
    Playwrightの操作はCDP経由のため、最小化中でも動作する。
    click(force=True), fill(), set_input_files(), evaluate() は全てCDPベース。
    """
    try:
        cdp = page.context.new_cdp_session(page)
        window = cdp.send("Browser.getWindowForTarget")
        window_id = window["windowId"]
        cdp.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {"windowState": "minimized"}
        })
        cdp.detach()
        status.update("デバッグ", "ブラウザを最小化しました")
    except Exception as e:
        status.update("デバッグ", f"ブラウザ非表示失敗（動作に影響なし）: {e}")


def _capture_qr_code(page: Page, status: UploadStatus) -> bool:
    """access.line.meのログインページからQRコードをキャプチャしてセッションディレクトリに保存。
    スマホユーザーがLINEアプリでスキャンしてログインできるようにする。

    CDP Page.captureScreenshot を使用するため、ブラウザが最小化・画面外でも動作する。
    戻り値: QRコード画像の保存に成功したらTrue。
    """
    import base64

    if not status.status_file:
        return False

    qr_path = status.status_file.parent / "qr_code.png"

    try:
        # Step 1: QRコード要素の位置を JavaScript で取得
        # access.line.me のQRログインページの要素構造を調査
        qr_bounds = page.evaluate("""() => {
            // QRコード要素を探す（複数パターン対応）
            const selectors = [
                'div.qr_code canvas',
                'div.qr_code img',
                'div[class*="qr"] canvas',
                'div[class*="qr"] img',
                '#qr_code canvas',
                '#qr_code img',
                'canvas[class*="qr"]',
                'img[alt*="QR"]',
                'img[src*="qr"]',
                // LINE login page specific
                'div[class*="QR"] canvas',
                'div[class*="QR"] img',
                '[data-testid*="qr"]',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 10 && rect.height > 10) {
                        return {x: rect.x, y: rect.y, w: rect.width, h: rect.height, sel: sel};
                    }
                }
            }
            return null;
        }""")

        # Step 2: CDP でスクリーンショットを取得（最小化・画面外でも動作）
        cdp = page.context.new_cdp_session(page)

        if qr_bounds:
            # QRコード要素のみをクリップしてキャプチャ
            status.update("デバッグ", f"QRコード要素発見: {qr_bounds['sel']} ({qr_bounds['w']}x{qr_bounds['h']})")
            # 余白を少し追加
            margin = 10
            result = cdp.send("Page.captureScreenshot", {
                "format": "png",
                "clip": {
                    "x": max(0, qr_bounds["x"] - margin),
                    "y": max(0, qr_bounds["y"] - margin),
                    "width": qr_bounds["w"] + margin * 2,
                    "height": qr_bounds["h"] + margin * 2,
                    "scale": 1,
                },
            })
        else:
            # フォールバック: ページ全体をキャプチャ
            status.update("デバッグ", "QRコード要素が特定できないため、ページ全体をCDPキャプチャ")
            result = cdp.send("Page.captureScreenshot", {"format": "png"})

        cdp.detach()

        # 画像を保存
        img_data = base64.b64decode(result["data"])
        qr_path.write_bytes(img_data)
        status.update("デバッグ", f"QRコード画像保存: {len(img_data)} bytes")
        return True

    except Exception as e:
        status.update("デバッグ", f"QRコード取得失敗: {e}")
        return False


def _capture_verification_screen(page: Page, status: UploadStatus) -> bool:
    """LINE本人確認画面（認証番号）をスクリーンショットしてQRコード画像として保存する。
    ユーザーのスマホにこの画像を表示し、LINEアプリで認証番号を入力してもらう。
    """
    import base64
    if not status.status_file:
        return False
    # QRコード画像と同じパスに保存（フロントエンドが同じ /api/qr-code/ で表示する）
    img_path = status.status_file.parent / "qr_code.png"
    try:
        cdp = page.context.new_cdp_session(page)
        result = cdp.send("Page.captureScreenshot", {"format": "png"})
        cdp.detach()
        img_data = base64.b64decode(result["data"])
        img_path.write_bytes(img_data)
        status.update("デバッグ", f"本人確認画面キャプチャ: {len(img_data)} bytes")
        return True
    except Exception as e:
        status.update("デバッグ", f"本人確認画面キャプチャ失敗: {e}")
        return False


def _is_on_verification_page(page: Page) -> bool:
    """LINE本人確認（2段階認証）画面にいるか判定する。"""
    try:
        text = page.evaluate("document.body ? document.body.innerText : ''")
        verification_keywords = ["本人確認", "認証番号", "確認コード", "verification", "Enter code", "PinCode"]
        return any(kw.lower() in text.lower() for kw in verification_keywords)
    except Exception:
        return False


def _extract_verification_code(page: Page) -> str:
    """LINE本人確認画面から認証番号（4桁の数字）を抽出する。"""
    try:
        code = page.evaluate("""() => {
            const allElements = document.querySelectorAll('*');
            for (const el of allElements) {
                const text = el.textContent.trim();
                if (/^\\d{4}$/.test(text)) {
                    const style = window.getComputedStyle(el);
                    const fontSize = parseFloat(style.fontSize);
                    if (fontSize >= 20) return text;
                }
            }
            const bodyText = document.body.innerText;
            const match = bodyText.match(/\\b(\\d{4})\\b/);
            return match ? match[1] : '';
        }""")
        return code or ""
    except Exception:
        return ""


def _fill_login_form(page: Page, email: str, password: str, status: UploadStatus) -> bool:
    """access.line.me のログインフォームにメール/パスワードを自動入力してログインする。"""
    try:
        # メール入力欄を探す
        email_input = page.locator('input[type="email"], input[name="tid"], input[placeholder*="メール"], input[placeholder*="email" i]').first
        if not email_input.is_visible(timeout=5000):
            status.update("デバッグ", "メール入力欄が見つかりません")
            return False

        # パスワード入力欄を探す
        password_input = page.locator('input[type="password"]').first
        if not password_input.is_visible(timeout=3000):
            status.update("デバッグ", "パスワード入力欄が見つかりません")
            return False

        # 入力
        email_input.fill(email)
        time.sleep(0.5)
        password_input.fill(password)
        time.sleep(0.5)

        # ログインボタンをクリック
        login_btn = page.locator('button[type="submit"], button:has-text("ログイン"), button:has-text("Log in")').first
        if login_btn.is_visible(timeout=3000):
            login_btn.click()
            status.update("ログイン", "🔐 ログイン中...", 12)
            time.sleep(3)
            _wait_for_page_ready(page)
            return True
        else:
            status.update("デバッグ", "ログインボタンが見つかりません")
            return False
    except Exception as e:
        status.update("デバッグ", f"自動ログイン失敗: {e}")
        return False



def _switch_to_qr_login(page: Page, status: UploadStatus) -> bool:
    """access.line.me のログインページで「QRコードログイン」に切り替える。
    デフォルトはメール/パスワード表示のため、QRコード表示にはクリックが必要。
    """
    try:
        # 「QRコードログイン」リンク/ボタンを探してクリック
        qr_btn = page.get_by_text("QRコードログイン")
        if qr_btn.first.is_visible(timeout=5000):
            qr_btn.first.click()
            status.update("デバッグ", "「QRコードログイン」をクリック")
            time.sleep(2)
            _wait_for_page_ready(page)
            return True
    except Exception as e:
        status.update("デバッグ", f"QRコード画面切り替え失敗: {e}")

    # フォールバック: テキストが異なるパターン
    try:
        for text in ["QRコードでログイン", "QR Code Login", "QRコード"]:
            btn = page.get_by_text(text)
            if btn.first.is_visible(timeout=1000):
                btn.first.click()
                status.update("デバッグ", f"「{text}」をクリック")
                time.sleep(2)
                _wait_for_page_ready(page)
                return True
    except Exception:
        pass

    status.update("デバッグ", "QRコードログインボタンが見つかりません")
    return False


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
    time.sleep(1)


def _is_on_dashboard(page: Page) -> bool:
    """ダッシュボード（/my/配下のアイテム一覧ページ）にいるか判定。
    証拠(session 8a2f7dad-2回目):
    - /my/{userId}/sticker/43339793/update はスタンプ編集ページ
    - /my/ を含むが、ダッシュボードではない
    - ダッシュボード = /my/{userId}/sticker/ (一覧ページ)
    """
    url = _get_real_url(page)
    if "/my/" not in url:
        return False
    # スタンプ/絵文字/着せかえの個別ページは除外
    # 例: /sticker/43339793 や /sticker/43339793/update
    if re.search(r"/sticker/\d+", url):
        return False
    if re.search(r"/emoji/\d+", url):
        return False
    if re.search(r"/theme/\d+", url):
        return False
    return True


def _is_on_login_page(url: str) -> bool:
    """LINEログインページにいるか判定。"""
    return "access.line.me" in url


def _is_on_creator_site(url: str) -> bool:
    """creator.line.me（ログインページ以外）にいるか判定。"""
    return "creator.line.me" in url and "access.line.me" not in url


def _is_on_creator_signup_page(url: str) -> bool:
    """初回ユーザーのクリエイター登録ページにいるか判定。
    除外: /signup/line_auth, /signup/line_callback (OAuth関連)
    除外: /signup/complete, /signup/verify, /signup/confirm (ポスト登録ページ)
    """
    if "creator.line.me" not in url:
        return False
    if "/signup/" not in url:
        return False
    # OAuth関連URLは登録ページではない
    if "/signup/line_auth" in url or "/signup/line_callback" in url:
        return False
    # 登録完了・確認系ページは登録フォームではない
    if any(kw in url for kw in ["/signup/complete", "/signup/verify", "/signup/confirm", "/signup/done"]):
        return False
    return True


def _get_real_url(page: Page) -> str:
    """ページの実際のURLを取得する。
    page.url はPlaywrightのキャッシュ値で、OAuth等のリダイレクト後に
    古いURLを返すことがある。JavaScriptでブラウザ側の実URLを取得する。
    """
    try:
        return page.evaluate("window.location.href")
    except Exception:
        try:
            return page.url
        except Exception:
            return ""


def _try_navigate_to_dashboard(page: Page, status: UploadStatus) -> bool:
    """/signup/line_auth に遷移し、ログイン済みならダッシュボードにリダイレクトされるか確認。
    ダッシュボードに到達できたらTrue。"""
    try:
        page.goto(LOGIN_URL, timeout=15000)
        _wait_for_page_ready(page)
        new_url = _get_real_url(page)
        status.update("デバッグ", f"リダイレクト先: {new_url[:100]}")
        return "/my/" in new_url
    except Exception as e:
        status.update("デバッグ", f"リダイレクト試行失敗: {e}")
        return False


# ============================================================
# 初回クリエイター登録
# ============================================================

def _handle_creator_registration(page: Page, title: str, status: UploadStatus) -> bool:
    """初回ユーザーのクリエイター登録フォームを自動入力して送信する。

    LINE Creators Market の登録フローを自動処理:
    1. 利用規約への同意
    2. 事業形態の選択（個人）
    3. 申込者氏名・屋号の入力
    4. 登録ボタンのクリック

    戻り値: 登録フォーム送信に成功したらTrue。
    """
    # 既に登録ページでなければスキップ
    url = _get_real_url(page)
    if not _is_on_creator_signup_page(url):
        status.update("デバッグ", "登録ページではなくなった — スキップ")
        return True

    status.update("クリエイター登録", "📝 初回クリエイター登録を行っています...", 15)
    _wait_for_page_ready(page)
    status.save_screenshot(page, "signup_01_initial")

    # --- Step 1: 利用規約の同意ボタンがあればクリック ---
    try:
        for agree_text in ["同意する", "同意して次へ", "Agree", "Accept"]:
            btn = page.get_by_text(agree_text, exact=False)
            if btn.count() > 0 and btn.first.is_visible(timeout=3000):
                # 同意チェックボックスがあれば先にチェック
                checkboxes = page.locator("input[type='checkbox']")
                for i in range(checkboxes.count()):
                    cb = checkboxes.nth(i)
                    try:
                        if cb.is_visible(timeout=1000) and not cb.is_checked():
                            cb.check(force=True)
                            status.update("デバッグ", f"同意チェックボックスをチェック ({i + 1})")
                            time.sleep(0.5)
                    except Exception:
                        continue
                btn.first.click(force=True)
                status.update("デバッグ", f"「{agree_text}」をクリック")
                time.sleep(3)
                _wait_for_page_ready(page)
                break
    except Exception as e:
        status.update("デバッグ", f"同意ボタン処理: {e}")

    status.save_screenshot(page, "signup_02_after_agree")

    # 同意後にダッシュボードへ遷移した場合（既に登録済みだった）
    new_url = _get_real_url(page)
    if "/my/" in new_url:
        status.update("デバッグ", "同意後にダッシュボードへ遷移 — 既に登録済み")
        return True
    if not _is_on_creator_signup_page(new_url):
        status.update("デバッグ", f"同意後に別ページへ遷移: {new_url[:100]}")
        return True

    # --- Step 2: 事業形態を「個人」に設定 ---
    try:
        selects = page.locator("select")
        for i in range(selects.count()):
            sel = selects.nth(i)
            try:
                if not sel.is_visible(timeout=1000):
                    continue
                name = (sel.get_attribute("name") or "").lower()
                sel_id = sel.get_attribute("id") or ""
                label_text = ""
                if sel_id:
                    label = page.locator(f"label[for='{sel_id}']")
                    if label.count() > 0:
                        label_text = (label.first.text_content() or "").lower()

                if any(kw in name for kw in ["business", "type"]) or \
                   any(kw in label_text for kw in ["事業形態", "事業", "business"]):
                    # 「個人」を部分一致で検索（「個人（日本国内居住者）」等にも対応）
                    try:
                        sel.select_option(label="個人")
                    except Exception:
                        # 完全一致が失敗したら、option要素を走査して部分一致
                        options = sel.locator("option")
                        selected = False
                        for oi in range(options.count()):
                            opt_text = options.nth(oi).text_content() or ""
                            if "個人" in opt_text or "individual" in opt_text.lower():
                                opt_val = options.nth(oi).get_attribute("value")
                                if opt_val is not None:
                                    sel.select_option(value=opt_val)
                                    selected = True
                                    status.update("デバッグ", f"事業形態: {opt_text.strip()}")
                                    break
                        if not selected:
                            status.update("デバッグ", "事業形態: 個人オプションが見つかりません")
                    else:
                        status.update("デバッグ", "事業形態: 個人を選択")
                    time.sleep(0.5)
                    break
            except Exception:
                continue
    except Exception as e:
        status.update("デバッグ", f"事業形態選択: {e}")

    # --- Step 3: 全フィールドのオートフィルをクリア & 申込者氏名・屋号を入力 ---
    # 安全策: まず全テキスト入力フィールドのオートフィル値をクリアしてから、
    # 必要なフィールドのみ値を設定する
    creator_name = title if title else "Creator"
    try:
        # 3a: 全てのテキスト入力フィールドのオートフィル値を一括クリア
        all_inputs = page.locator("input[type='text'], input[type='tel'], input[type='email']:not([name*='login'])")
        total_inputs = all_inputs.count()
        cleared_count = 0
        for i in range(total_inputs):
            inp = all_inputs.nth(i)
            try:
                if not inp.is_visible(timeout=500):
                    continue
                current_val = inp.input_value()
                if current_val.strip():
                    inp.fill("")
                    cleared_count += 1
            except Exception:
                continue
        if cleared_count > 0:
            status.update("デバッグ", f"オートフィル値を{cleared_count}フィールドからクリア")

        # 3b: 必要なフィールドに値を入力
        inputs = page.locator("input[type='text']")
        count = inputs.count()
        name_filled = False
        trade_filled = False
        last_name_filled = False
        first_name_filled = False
        for i in range(count):
            inp = inputs.nth(i)
            try:
                if not inp.is_visible(timeout=500):
                    continue
                name_attr = (inp.get_attribute("name") or "").lower()
                placeholder = (inp.get_attribute("placeholder") or "").lower()
                inp_id = inp.get_attribute("id") or ""
                label_text = ""
                if inp_id:
                    label = page.locator(f"label[for='{inp_id}']")
                    if label.count() > 0:
                        label_text = (label.first.text_content() or "")

                # 姓フィールド（姓名分割フォーム対応）
                is_last_name = any(kw in name_attr for kw in ["last_name", "family_name", "surname", "sei"]) or \
                    any(kw in label_text for kw in ["姓", "ラストネーム"])
                if is_last_name and not last_name_filled:
                    inp.fill(creator_name)
                    last_name_filled = True
                    status.update("デバッグ", f"姓入力: {creator_name}")
                    continue

                # 名フィールド（姓名分割フォーム対応）
                is_first_name = any(kw in name_attr for kw in ["first_name", "given_name", "mei"]) or \
                    any(kw in label_text for kw in ["名", "ファーストネーム"])
                if is_first_name and not first_name_filled:
                    inp.fill(creator_name)
                    first_name_filled = True
                    status.update("デバッグ", f"名入力: {creator_name}")
                    continue

                # 氏名フィールド（単一フィールド）
                is_name_field = any(kw in name_attr for kw in ["name", "applicant"]) or \
                    any(kw in label_text for kw in ["氏名", "名前", "申込者"]) or \
                    any(kw in placeholder for kw in ["氏名", "名前", "name"])
                if is_name_field and not name_filled and not last_name_filled:
                    inp.fill(creator_name)
                    name_filled = True
                    status.update("デバッグ", f"氏名入力: {creator_name}")
                    continue

                # 屋号フィールド
                is_trade_field = any(kw in name_attr for kw in ["trade", "creator"]) or \
                    any(kw in label_text for kw in ["屋号", "ペンネーム", "クリエイター名"]) or \
                    any(kw in placeholder for kw in ["屋号", "trade", "クリエイター"])
                if is_trade_field and not trade_filled:
                    inp.fill(creator_name)
                    trade_filled = True
                    status.update("デバッグ", f"屋号入力: {creator_name}")
                    continue
            except Exception:
                continue
    except Exception as e:
        status.update("デバッグ", f"名前入力: {e}")

    # --- Step 4: 居住国が未選択なら「日本」を選択 ---
    try:
        selects = page.locator("select")
        for i in range(selects.count()):
            sel = selects.nth(i)
            try:
                if not sel.is_visible(timeout=1000):
                    continue
                name = (sel.get_attribute("name") or "").lower()
                sel_id = sel.get_attribute("id") or ""
                label_text = ""
                if sel_id:
                    label = page.locator(f"label[for='{sel_id}']")
                    if label.count() > 0:
                        label_text = (label.first.text_content() or "").lower()

                if any(kw in name for kw in ["country", "region", "居住"]) or \
                   any(kw in label_text for kw in ["居住国", "国", "country", "region"]):
                    try:
                        sel.select_option(label="日本")
                        status.update("デバッグ", "居住国: 日本を選択")
                    except Exception:
                        try:
                            sel.select_option(label="Japan")
                            status.update("デバッグ", "居住国: Japanを選択")
                        except Exception:
                            pass
                    break
            except Exception:
                continue
    except Exception as e:
        status.update("デバッグ", f"居住国選択: {e}")

    # --- Step 5: 残りのチェックボックスを全てチェック ---
    try:
        checkboxes = page.locator("input[type='checkbox']")
        for i in range(checkboxes.count()):
            cb = checkboxes.nth(i)
            try:
                if cb.is_visible(timeout=1000) and not cb.is_checked():
                    cb.check(force=True)
                    status.update("デバッグ", f"チェックボックスをチェック ({i + 1})")
                    time.sleep(0.3)
            except Exception:
                continue
    except Exception:
        pass

    status.save_screenshot(page, "signup_03_filled")

    # --- Step 6: 登録ボタンをクリック ---
    registered = False
    for submit_text in ["登録", "Register", "送信", "Submit", "次へ", "確認"]:
        try:
            btn = page.get_by_text(submit_text, exact=True)
            if btn.count() > 0 and btn.first.is_visible(timeout=2000):
                btn.first.click(force=True)
                status.update("デバッグ", f"「{submit_text}」をクリック")
                registered = True
                time.sleep(3)
                _wait_for_page_ready(page)
                break
        except Exception:
            continue

    if not registered:
        # フォールバック: submit ボタンを探す
        try:
            submit_btn = page.locator("button[type='submit'], input[type='submit']").first
            if submit_btn.is_visible(timeout=2000):
                submit_btn.click(force=True)
                status.update("デバッグ", "submitボタンをクリック")
                registered = True
                time.sleep(3)
                _wait_for_page_ready(page)
        except Exception:
            pass

    status.save_screenshot(page, "signup_04_submitted")

    if not registered:
        status.update("警告", "クリエイター登録フォームの送信ボタンが見つかりませんでした")
        return False

    # 送信後にバリデーションエラーが表示されているか確認
    try:
        error_elements = page.locator(".error, .alert-danger, .validation-error, [class*='error'], [class*='Error']")
        if error_elements.count() > 0:
            for ei in range(min(error_elements.count(), 3)):
                err_text = error_elements.nth(ei).text_content() or ""
                if err_text.strip():
                    status.update("デバッグ", f"フォームエラー検出: {err_text.strip()[:80]}")
    except Exception:
        pass

    # 送信後にまだ同じ登録ページにいるか確認（ダッシュボードに遷移したら成功）
    post_url = _get_real_url(page)
    if "/my/" in post_url:
        status.update("デバッグ", "登録後にダッシュボードへ遷移 — 登録成功")
        return True

    # 送信後の確認ページで追加の確認ボタンがある場合
    try:
        for confirm_text in ["確認", "OK", "完了", "送信", "登録する", "同意して登録"]:
            btn = page.get_by_text(confirm_text, exact=False)
            if btn.count() > 0 and btn.first.is_visible(timeout=3000):
                btn.first.click(force=True)
                status.update("デバッグ", f"確認ボタン「{confirm_text}」をクリック")
                time.sleep(3)
                _wait_for_page_ready(page)
                break
    except Exception:
        pass

    status.save_screenshot(page, "signup_05_confirmed")
    status.update("クリエイター登録", "📧 登録処理中... メール確認が必要な場合があります", 17)
    return True


# ============================================================
# ログイン検出
# ============================================================

def _find_dashboard_page(context: BrowserContext, status: UploadStatus) -> Optional[Page]:
    """コンテキスト内の全ページから、ダッシュボードにいるページを探す。
    見つからない場合、creator.line.meにいるページから /signup/line_auth でリダイレクトを試みる。
    """
    all_pages = context.pages
    creator_page = None

    for p in all_pages:
        try:
            url = _get_real_url(p)
            # ダッシュボードに直接いるページ（_is_on_dashboardで厳密判定）
            if _is_on_dashboard(p):
                status.update("デバッグ", f"ダッシュボード検出: {url[:100]}")
                return p
            # /my/ 配下だがダッシュボードではないページ（スタンプ編集ページ等）
            # → creator.line.me にいるページとして記録（後でリダイレクト試行）
            if "/my/" in url:
                status.update("デバッグ", f"/my/配下検出: {url[:100]}")
                creator_page = p
            # creator.line.me にいるページ（ログインページ・登録ページ以外）を記録
            elif _is_on_creator_site(url) and not _is_on_creator_signup_page(url) and "/signup/line_auth" not in url:
                creator_page = p
            # OAuth コールバック中のページ
            if "/signup/line_callback" in url:
                status.update("デバッグ", "OAuthコールバック検出")
                try:
                    p.wait_for_url(
                        lambda u: "/my/" in u or _is_on_creator_signup_page(u) or (_is_on_creator_site(u) and "/signup/" not in u),
                        timeout=15000,
                    )
                    url_after = _get_real_url(p)
                    if _is_on_dashboard(p):
                        return p
                    if _is_on_creator_signup_page(url_after):
                        status.update("デバッグ", "初回ユーザー: クリエイター登録ページ検出（OAuthコールバック後）")
                    creator_page = p
                except PwTimeout:
                    pass
        except Exception:
            continue

    # creator.line.me にいるがダッシュボードではないページがある場合、リダイレクト試行
    if creator_page:
        status.update("デバッグ", "creator.line.me検出。ダッシュボードへリダイレクト試行...")
        if _try_navigate_to_dashboard(creator_page, status):
            return creator_page
        # リダイレクト失敗 → access.line.me に戻された場合はまだ未ログイン
        if _is_on_login_page(_get_real_url(creator_page)):
            return None

    return None


def wait_for_login(page: Page, status: UploadStatus, timeout: int = 300, email: str = "", password: str = "", title: str = "") -> Optional[Page]:
    """ユーザーがログインするまで待機。ログイン済みのダッシュボードページを返す。"""
    context = page.context

    # 新しいタブ/ポップアップを追跡するリスト
    new_pages: list[Page] = []

    def _on_new_page(new_page: Page):
        """新しいタブやポップアップが開かれた時のハンドラ"""
        new_pages.append(new_page)
        try:
            url = _get_real_url(new_page)
            status.update("デバッグ", f"新しいページ検出: {url[:120]}")
        except Exception:
            pass

    context.on("page", _on_new_page)

    def _check_all_pages_for_dashboard() -> Optional[Page]:
        """全ページを確認してダッシュボードのページを返す。
        page.urlではなくJavaScriptで実URLを取得し、キャッシュ問題を回避。
        """
        for p in context.pages:
            try:
                url = _get_real_url(p)
                if "/my/" in url and "access.line.me" not in url:
                    return p
            except Exception:
                continue
        return None

    def _check_all_pages_for_creator_site() -> Optional[Page]:
        """creator.line.meにいるページを返す（ログインページ・登録ページ除外）。
        page.urlではなくJavaScriptで実URLを取得。
        """
        for p in context.pages:
            try:
                url = _get_real_url(p)
                if _is_on_creator_site(url) and not _is_on_creator_signup_page(url) and "/signup/line_auth" not in url:
                    return p
            except Exception:
                continue
        return None

    def _check_all_pages_for_signup() -> Optional[Page]:
        """初回クリエイター登録ページにいるページを返す。"""
        for p in context.pages:
            try:
                url = _get_real_url(p)
                if _is_on_creator_signup_page(url):
                    return p
            except Exception:
                continue
        return None

    # === Step 1: LOGIN_URLにアクセス ===
    status.update("ログイン", "LINE Creators Marketにアクセス中...", 5)
    try:
        page.goto(LOGIN_URL, timeout=30000)
    except Exception as e:
        status.update("デバッグ", f"LOGIN_URL遷移エラー: {e}")

    _wait_for_page_ready(page)

    current = _get_real_url(page)
    status.update("デバッグ", f"遷移後URL: {current[:120]}")

    # === Step 2: 現在のページ状態を判定 ===

    # ケース1: 直接ダッシュボードにリダイレクトされた（ログイン済み）
    dashboard = _check_all_pages_for_dashboard()
    if dashboard:
        status.update("ログイン", "✅ 前回のログインが有効です（自動ログイン）", 20)
        return dashboard

    # ケース2: creator.line.meにいるがダッシュボードではない → ダッシュボードへ誘導
    creator = _check_all_pages_for_creator_site()
    if creator:
        status.update("デバッグ", "creator.line.me検出。ダッシュボードへ誘導中...")
        if _try_navigate_to_dashboard(creator, status):
            status.update("ログイン", "✅ ログイン済み", 20)
            return creator

    # ケース3: ログインページ → メール/パスワードで自動ログイン
    if email and password:
        status.update("ログイン", "🔐 メール/パスワードで自動ログイン中...", 10)
        login_ok = _fill_login_form(page, email, password, status)
        if login_ok:
            # ログイン後のリダイレクトを待つ
            time.sleep(5)
            _wait_for_page_ready(page)

            # ダッシュボードに到達したか確認
            dashboard = _check_all_pages_for_dashboard()
            if dashboard:
                status.update("ログイン", "✅ ログイン完了！", 20)
                return dashboard
            # creator.line.meにいる場合
            creator = _check_all_pages_for_creator_site()
            if creator:
                if _try_navigate_to_dashboard(creator, status):
                    status.update("ログイン", "✅ ログイン完了！", 20)
                    return creator

            # 初回ユーザー: クリエイター登録ページに遷移した場合
            signup_page = _check_all_pages_for_signup()
            if signup_page:
                status.update("デバッグ", "初回ユーザー検出: クリエイター登録ページ")
                if _handle_creator_registration(signup_page, title, status):
                    # 登録後、メール確認またはダッシュボード遷移を待つ
                    email_wait = 0
                    while email_wait < 120:
                        time.sleep(5)
                        email_wait += 5
                        # ダッシュボードに到達した？
                        dashboard = _check_all_pages_for_dashboard()
                        if dashboard:
                            status.update("ログイン", "✅ クリエイター登録完了！", 20)
                            return dashboard
                        # 登録ページから別ページへ遷移した？
                        current = _get_real_url(signup_page)
                        if "/my/" in current:
                            status.update("ログイン", "✅ クリエイター登録完了！", 20)
                            return signup_page
                        if not _is_on_creator_signup_page(current) and _is_on_creator_site(current):
                            # 登録完了後の別ページ → ダッシュボードへ誘導
                            if _try_navigate_to_dashboard(signup_page, status):
                                status.update("ログイン", "✅ クリエイター登録完了！", 20)
                                return signup_page
                        if email_wait % 30 == 0:
                            remaining = 120 - email_wait
                            status.update("クリエイター登録", f"📧 メールのリンクをクリックして登録を完了してください（残り{remaining}秒）", 17)
                    # タイムアウト → リダイレクト試行
                    if _try_navigate_to_dashboard(signup_page, status):
                        status.update("ログイン", "✅ クリエイター登録完了！", 20)
                        return signup_page
                    status.update("エラー", "⏰ クリエイター登録のメール確認がタイムアウトしました。メール内のリンクをクリックしてから、もう一度お試しください。", 0)
                    return None

            # 本人確認画面（2段階認証）を検出
            if _is_on_verification_page(page):
                status.update("デバッグ", "本人確認画面を検出 → 認証番号を抽出中")
                _capture_verification_screen(page, status)
                code = _extract_verification_code(page)
                if code:
                    status.update("本人確認", f"🔢 認証番号: {code}  ← LINEアプリに入力してください", 12, extra={"verification_code": code})
                else:
                    status.update("本人確認", "📱 LINEアプリで本人確認を行ってください", 12)

                # 本人確認完了を待機（最大120秒）
                verify_elapsed = 0
                verify_interval = 3
                verify_timeout = 120
                while verify_elapsed < verify_timeout:
                    time.sleep(verify_interval)
                    verify_elapsed += verify_interval

                    # 認証番号が更新される場合に備えて再取得
                    if verify_elapsed % 15 < verify_interval:
                        new_code = _extract_verification_code(page)
                        if new_code and new_code != code:
                            code = new_code
                            status.update("本人確認", f"🔢 認証番号: {code}  ← LINEアプリに入力してください", 12, extra={"verification_code": code})
                        _capture_verification_screen(page, status)

                    # ダッシュボードに到達した？
                    dashboard = _check_all_pages_for_dashboard()
                    if dashboard:
                        status.update("ログイン", "✅ 本人確認完了！ログインしました", 20)
                        return dashboard
                    creator = _check_all_pages_for_creator_site()
                    if creator:
                        if _try_navigate_to_dashboard(creator, status):
                            status.update("ログイン", "✅ 本人確認完了！ログインしました", 20)
                            return creator

                    # 初回ユーザー: signupページに遷移した？
                    signup_page = _check_all_pages_for_signup()
                    if signup_page:
                        status.update("デバッグ", "本人確認完了 → クリエイター登録ページ検出")
                        if _handle_creator_registration(signup_page, title, status):
                            # 登録後、ダッシュボード遷移を待つ
                            reg_wait = 0
                            while reg_wait < 120:
                                time.sleep(5)
                                reg_wait += 5
                                dashboard = _check_all_pages_for_dashboard()
                                if dashboard:
                                    status.update("ログイン", "✅ クリエイター登録完了！", 20)
                                    return dashboard
                                current = _get_real_url(signup_page)
                                if "/my/" in current:
                                    status.update("ログイン", "✅ クリエイター登録完了！", 20)
                                    return signup_page
                                if not _is_on_creator_signup_page(current) and _is_on_creator_site(current):
                                    if _try_navigate_to_dashboard(signup_page, status):
                                        status.update("ログイン", "✅ クリエイター登録完了！", 20)
                                        return signup_page
                                if reg_wait % 30 == 0:
                                    remaining = 120 - reg_wait
                                    status.update("クリエイター登録", f"📧 メールのリンクをクリックして登録を完了してください（残り{remaining}秒）", 17)
                            if _try_navigate_to_dashboard(signup_page, status):
                                status.update("ログイン", "✅ クリエイター登録完了！", 20)
                                return signup_page
                            status.update("エラー", "⏰ クリエイター登録のメール確認がタイムアウトしました。", 0)
                            return None

                    # まだ本人確認ページにいるか？
                    current_url = _get_real_url(page)
                    if not _is_on_verification_page(page) and not _is_on_login_page(current_url):
                        # 別のページに遷移 → ログイン成功の可能性
                        status.update("デバッグ", f"本人確認後に別ページへ遷移: {current_url[:100]}")
                        time.sleep(3)
                        _wait_for_page_ready(page)
                        dashboard = _check_all_pages_for_dashboard()
                        if dashboard:
                            status.update("ログイン", "✅ ログイン完了！", 20)
                            return dashboard
                        # signupページの再チェック（ページ遷移後）
                        signup_page = _check_all_pages_for_signup()
                        if signup_page:
                            status.update("デバッグ", "本人確認後 → クリエイター登録ページ検出（遷移後）")
                            if _handle_creator_registration(signup_page, title, status):
                                # 登録後、ダッシュボード遷移を最大120秒待つ
                                reg_wait = 0
                                while reg_wait < 120:
                                    time.sleep(5)
                                    reg_wait += 5
                                    dashboard = _check_all_pages_for_dashboard()
                                    if dashboard:
                                        status.update("ログイン", "✅ クリエイター登録完了！", 20)
                                        return dashboard
                                    if reg_wait % 30 == 0:
                                        status.update("クリエイター登録", f"📧 メール確認をお願いします（残り{120 - reg_wait}秒）", 18)
                                # 最後にダッシュボードへの直接遷移を試行
                                if _try_navigate_to_dashboard(signup_page, status):
                                    status.update("ログイン", "✅ クリエイター登録完了！", 20)
                                    return signup_page

                status.update("エラー", "⏰ 本人確認がタイムアウトしました。もう一度お試しください。", 0)
                return None

            # まだログインページにいる場合（パスワード間違い等）
            current_url = _get_real_url(page)
            if _is_on_login_page(current_url):
                status.update("エラー", "❌ ログインに失敗しました。メールアドレスまたはパスワードを確認してください。", 0)
                return None
    else:
        # メール/パスワードが提供されていない場合はQRコードにフォールバック
        status.update("デバッグ", "ログインページ検出 → QRコード画面に切り替えます")
        _switch_to_qr_login(page, status)
        qr_captured = _capture_qr_code(page, status)
        if qr_captured:
            status.update("QRコード", "📱 LINEアプリでQRコードをスキャンしてログインしてください", 10)
        else:
            status.update("QRコード", "🔓 LINEにログインしてください（QRコード取得中...）", 10)

    # === Step 3: ポーリングで待機 ===
    elapsed = 0
    interval = 3
    login_detected = False
    registration_attempted = False  # _handle_creator_registration()の重複呼び出し防止
    pages_empty_count = 0
    last_qr_capture = 0  # 最後のQRキャプチャ時刻（elapsed秒）
    QR_REFRESH_INTERVAL = 30  # QRコード更新間隔（秒）
    while elapsed < timeout:
        time.sleep(interval)
        elapsed += interval

        # QRコードを定期的に再取得（有効期限切れ対策）
        if elapsed - last_qr_capture >= QR_REFRESH_INTERVAL:
            try:
                current_url = _get_real_url(page)
                if _is_on_login_page(current_url):
                    # 90秒ごとにページをリロードしてQRコードを更新
                    # （LINE QRコードは約2分で期限切れになる）
                    if elapsed > 0 and elapsed % 90 < interval:
                        status.update("デバッグ", "QRコード期限切れ → ページリロード")
                        page.reload(timeout=15000)
                        _wait_for_page_ready(page)
                        _switch_to_qr_login(page, status)
                        time.sleep(1)
                    _capture_qr_code(page, status)
                    last_qr_capture = elapsed
                    status.update("デバッグ", f"QRコード再取得（{elapsed}秒経過）")
            except Exception:
                pass

        # ページ一覧を安全に取得
        all_pages = []
        try:
            all_pages = context.pages
        except Exception:
            pass

        # ブラウザページが0件の場合のリカバリー
        if len(all_pages) == 0:
            pages_empty_count += 1
            status.update("デバッグ", f"ブラウザページが0件（{pages_empty_count}回目）")
            # 新しいページを作成してリカバリー
            if pages_empty_count <= 3:
                try:
                    recovery_page = context.new_page()
                    recovery_page.goto(LOGIN_URL, timeout=15000)
                    _wait_for_page_ready(recovery_page)
                    page = recovery_page
                    status.update("デバッグ", "ページをリカバリーしました")
                    # リカバリー後にQRコードを再取得
                    _capture_qr_code(page, status)
                    last_qr_capture = elapsed
                    continue
                except Exception as e:
                    status.update("デバッグ", f"リカバリー失敗: {e}")
            if pages_empty_count >= 10:
                status.update("エラー", "ブラウザとの接続が切れました。もう一度お試しください。")
                return None
            continue

        pages_empty_count = 0

        # 全ページのURLを安全に確認（ナビゲートしない）
        for p in all_pages:
            try:
                url = _get_real_url(p)
                # ダッシュボードに到達した
                if "/my/" in url and "access.line.me" not in url:
                    _wait_for_page_ready(p)
                    status.update("ログイン", "✅ ログイン完了！", 20)
                    return p
                # 初回ユーザー: クリエイター登録ページ
                if _is_on_creator_signup_page(url):
                    if not login_detected:
                        login_detected = True
                        status.update("デバッグ", f"初回ユーザー登録ページ検出: {url[:100]}")
                    if not registration_attempted:
                        registration_attempted = True
                        _handle_creator_registration(p, title, status)
                    # 登録後の遷移を待つ（次のポーリングで確認）
                    continue
                # creator.line.meにいる（ログイン後リダイレクト）がダッシュボードではない
                if _is_on_creator_site(url) and not _is_on_creator_signup_page(url) and "access.line.me" not in url:
                    if not login_detected:
                        login_detected = True
                        status.update("デバッグ", f"ログイン検出: {url[:100]}")
                    # ダッシュボードへナビゲート（1回だけ）
                    try:
                        p.goto("https://creator.line.me/my/sticker/", timeout=15000)
                        _wait_for_page_ready(p)
                        new_url = _get_real_url(p)
                        if "/my/" in new_url:
                            status.update("ログイン", "✅ ログイン完了！", 20)
                            return p
                    except Exception as e:
                        status.update("デバッグ", f"ダッシュボード遷移失敗: {e}")
            except Exception:
                continue

        # ステータス更新（15秒ごと）
        if elapsed % 15 == 0:
            remaining = timeout - elapsed
            status.update("QRコード", f"📱 QRコードをスキャンしてログインしてください（残り{remaining}秒）", 10)

    status.update("エラー", "⏰ ログインがタイムアウトしました。もう一度お試しください。")
    return None


# ============================================================
# ダッシュボード操作
# ============================================================

def _dismiss_all_modals(page: Page, status: UploadStatus, max_attempts: int = 10):
    """ダッシュボード上の全モーダルを確実に閉じる。

    重要: JavaScript el.click() は isTrusted=false のため Vue が無視する。
    必ず Playwright のネイティブ .click(force=True) を使うこと。
    """
    MODAL_SELECTORS = [
        '[role="dialog"]', '.MdPOP01Modal',
        '[class*="modal" i]', '[class*="Modal"]',
        '[class*="popup" i]', '[class*="Popup"]',
    ]

    def _has_visible_modal() -> bool:
        """可視モーダルがあるか確認"""
        try:
            for sel in MODAL_SELECTORS:
                locator = page.locator(sel)
                for i in range(locator.count()):
                    try:
                        if locator.nth(i).is_visible(timeout=300):
                            return True
                    except Exception:
                        pass
            return False
        except Exception:
            return False

    for attempt in range(max_attempts):
        time.sleep(1.5)

        if not _has_visible_modal():
            status.update("デバッグ", "モーダル処理完了")
            return

        status.update("デバッグ", f"モーダル検出 ({attempt+1}/{max_attempts}回目)")

        # === 戦略1: Playwright ネイティブクリック（isTrusted=true）===
        closed = False

        # 1a: 「今後表示しない」チェックボックスをPlaywrightでチェック
        try:
            checkboxes = page.locator("input[type='checkbox']")
            for i in range(checkboxes.count()):
                cb = checkboxes.nth(i)
                try:
                    if not cb.is_visible(timeout=300):
                        continue
                    parent_text = ""
                    try:
                        parent_text = cb.locator("xpath=..").text_content(timeout=500) or ""
                    except Exception:
                        pass
                    if "表示しない" in parent_text or "今後" in parent_text:
                        if not cb.is_checked():
                            cb.check(force=True, timeout=2000)
                            status.update("デバッグ", "「今後表示しない」をチェック")
                except Exception:
                    pass
        except Exception as e:
            status.update("デバッグ", f"チェックボックス処理エラー: {e}")

        # 1b: 「閉じる」ボタンをPlaywrightでクリック
        for close_text in ["閉じる", "Close", "OK"]:
            if closed:
                break
            try:
                locator = page.get_by_text(close_text, exact=True)
                count = locator.count()
                # 後ろから（最前面のモーダル優先）
                for ci in range(count - 1, -1, -1):
                    candidate = locator.nth(ci)
                    try:
                        if candidate.is_visible(timeout=500):
                            candidate.click(force=True, timeout=3000)
                            status.update("デバッグ", f"「{close_text}」をクリック ({attempt+1}回目)")
                            closed = True
                            time.sleep(2)
                            break
                    except Exception:
                        continue
            except Exception:
                continue

        if closed:
            continue  # 次のモーダルがあるかチェック

        # === 戦略2: ボタンroleで検索してクリック ===
        try:
            buttons = page.get_by_role("button")
            for i in range(buttons.count() - 1, -1, -1):
                btn = buttons.nth(i)
                try:
                    if not btn.is_visible(timeout=300):
                        continue
                    text = (btn.text_content(timeout=500) or "").strip()
                    if text in ("閉じる", "Close", "OK", "はい"):
                        btn.click(force=True, timeout=3000)
                        status.update("デバッグ", f"ボタン「{text}」をクリック ({attempt+1}回目)")
                        closed = True
                        time.sleep(2)
                        break
                except Exception:
                    continue
        except Exception:
            pass

        if closed:
            continue

        # === 戦略3: Escapeキー ===
        try:
            page.keyboard.press("Escape")
            time.sleep(1.5)
            if not _has_visible_modal():
                status.update("デバッグ", f"Escapeでモーダルを閉じました ({attempt+1}回目)")
                continue
        except Exception:
            pass

        # === 戦略4 (3回目以降): DOM強制削除 ===
        if attempt >= 2:
            try:
                removed = page.evaluate("""
                    (() => {
                        let count = 0;
                        const sels = ['[role="dialog"]', '.MdPOP01Modal', '[class*="modal" i]',
                                       '[class*="Modal"]', '[class*="popup" i]', '[class*="Popup"]'];
                        for (const s of sels) {
                            for (const el of document.querySelectorAll(s)) {
                                const r = el.getBoundingClientRect();
                                if (r.width > 100 && r.height > 100) {
                                    el.remove(); count++;
                                }
                            }
                        }
                        const bgSels = ['.ExBackdrop', '[class*="backdrop" i]',
                                        '[class*="overlay" i]', '[class*="mask" i]'];
                        for (const s of bgSels) {
                            for (const el of document.querySelectorAll(s)) {
                                el.remove(); count++;
                            }
                        }
                        document.body.style.overflow = '';
                        document.documentElement.style.overflow = '';
                        return count;
                    })()
                """)
                if removed > 0:
                    status.update("デバッグ", f"モーダルを強制削除 ({removed}要素)")
                    time.sleep(1)
                    continue
            except Exception as e:
                status.update("デバッグ", f"強制削除エラー: {e}")

    status.update("デバッグ", f"モーダル処理: {max_attempts}回試行完了")


def _dismiss_modals(page: Page, status: UploadStatus):
    """ダッシュボード上のモーダルポップアップ（キャンペーン告知等）を閉じる。
    重要: ページ遷移を起こさないよう、各クリック後にURL変化を検証する。"""

    url_before = page.url

    # デバッグ: ページ上の「閉じる」を含む要素を全てスキャン
    try:
        debug_info = page.evaluate("""
            (() => {
                const results = [];
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    let directText = '';
                    for (const node of el.childNodes) {
                        if (node.nodeType === 3) directText += node.textContent;
                    }
                    directText = directText.trim();
                    if (directText.includes('閉じる') || directText === 'Close') {
                        const rect = el.getBoundingClientRect();
                        results.push({
                            tag: el.tagName,
                            text: directText.slice(0, 30),
                            class: (el.className?.toString() || '').slice(0, 80),
                            href: el.getAttribute('href') || '',
                            visible: rect.width > 0 && rect.height > 0,
                            rect: {x: Math.round(rect.x), y: Math.round(rect.y),
                                   w: Math.round(rect.width), h: Math.round(rect.height)}
                        });
                    }
                }
                return results;
            })()
        """)
        for item in debug_info:
            status.update("デバッグ", f"閉じる要素スキャン: {item}")
    except Exception as e:
        status.update("デバッグ", f"スキャンエラー: {e}")

    # 「今後、この画面を表示しない」チェックボックスを先にチェック
    _check_dont_show_again(page, status)

    # 方法1: Playwright get_by_text（可視の要素のみクリック）
    # 証拠(session 8a2f7dad-2回目): .lastが不可視の background-overlay を掴む
    for exact in [True, False]:
        try:
            locator = page.get_by_text("閉じる", exact=exact)
            count = locator.count()
            status.update("デバッグ", f"get_by_text('閉じる', exact={exact}): {count}個")
            if count > 0:
                # 可視の要素を後ろから探す（モーダル優先）
                target = None
                for ci in range(count - 1, -1, -1):
                    candidate = locator.nth(ci)
                    try:
                        if candidate.is_visible(timeout=500):
                            target = candidate
                            break
                    except Exception:
                        continue
                if target is None:
                    status.update("デバッグ", f"可視の「閉じる」なし (exact={exact})")
                    continue
                tag = target.evaluate("e => e.tagName")
                text = target.text_content().strip()[:30]
                status.update("デバッグ", f"クリック対象: tag={tag}, text='{text}'")
                target.click(force=True)
                time.sleep(2)
                # URL変化チェック: 誤ナビゲーション防止
                if page.url != url_before:
                    status.update("デバッグ", f"⚠️ URL変化検出！ {url_before} → {page.url} 戻ります")
                    page.goto(url_before, timeout=10000)
                    _wait_for_page_ready(page)
                    continue  # 次のexact値で再試行
                status.update("デバッグ", "「閉じる」クリック成功 (get_by_text)")
                return
        except Exception as e:
            status.update("デバッグ", f"get_by_text失敗 (exact={exact}): {e}")

    # 方法2: get_by_role でボタン/リンクとして検索
    for role in ["button", "link"]:
        try:
            locator = page.get_by_role(role, name="閉じる")
            if locator.count() > 0:
                locator.last.click(force=True)
                time.sleep(2)
                if page.url != url_before:
                    status.update("デバッグ", f"⚠️ URL変化 (role={role})！戻ります")
                    page.goto(url_before, timeout=10000)
                    _wait_for_page_ready(page)
                    continue
                status.update("デバッグ", f"「閉じる」クリック成功 (role={role})")
                return
        except Exception as e:
            status.update("デバッグ", f"get_by_role({role})失敗: {e}")

    # 方法3: JavaScript - 「閉じる」テキストのみ持つ最末尾要素をクリック（hrefなし限定）
    try:
        closed = page.evaluate("""
            (() => {
                const all = document.querySelectorAll('a, button, [role="button"], span, div');
                const reversed = Array.from(all).reverse();
                for (const el of reversed) {
                    // hrefがある要素はスキップ（ナビゲーション防止）
                    if (el.tagName === 'A' && el.getAttribute('href')) continue;
                    let directText = '';
                    for (const node of el.childNodes) {
                        if (node.nodeType === 3) directText += node.textContent;
                    }
                    directText = directText.trim();
                    if (directText === '閉じる' || directText === 'Close') {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            el.click();
                            return {tag: el.tagName, text: directText};
                        }
                    }
                }
                return null;
            })()
        """)
        if closed:
            status.update("デバッグ", f"JSクリック成功: {closed}")
            time.sleep(2)
            return
    except Exception as e:
        status.update("デバッグ", f"JSクリック失敗: {e}")

    # 方法4: Escapeキー
    try:
        page.keyboard.press("Escape")
        status.update("デバッグ", "Escキー試行")
        time.sleep(1)
    except Exception:
        pass

    status.update("デバッグ", "モーダル閉じ: 全方法失敗")


def _force_dismiss_qr_modal(page: Page, status: UploadStatus):
    """QRコードモーダル(FnOaQrcode)をJavaScriptで強制的に閉じる。
    証拠(session b45b0b87): QRモーダルが画像アップロードのクリックをブロック:
    - <div role="dialog" class="MdPOP01Modal ... FnOaQrcode"> intercepts pointer events
    - 毎回の画像アップロード試行で30秒タイムアウト
    証拠(session b45b0b87-2回目): JS削除してもVue再レンダリングで復活
    → CSSで永続的に非表示にする
    """
    try:
        removed = page.evaluate("""
            (() => {
                // CSS注入: QRモーダルを永続的に非表示（Vue再レンダリング対策）
                if (!document.getElementById('__dismiss_qr_style')) {
                    const style = document.createElement('style');
                    style.id = '__dismiss_qr_style';
                    style.textContent = `
                        .FnOaQrcode, .MdPop24OAQRcode,
                        .FnOaQrcode .ExBackdrop,
                        [role="dialog"].FnOaQrcode {
                            display: none !important;
                            pointer-events: none !important;
                        }
                    `;
                    document.head.appendChild(style);
                }
                // 既存のモーダルも削除
                const modals = document.querySelectorAll('.FnOaQrcode, .MdPop24OAQRcode');
                let removed = 0;
                for (const modal of modals) {
                    const dialog = modal.closest('[role="dialog"]') || modal;
                    dialog.remove();
                    removed++;
                }
                const backdrops = document.querySelectorAll('.ExBackdrop');
                for (const bd of backdrops) {
                    bd.remove();
                    removed++;
                }
                return removed;
            })()
        """)
        if removed and removed > 0:
            status.update("デバッグ", f"QRモーダル強制削除+CSS非表示: {removed}要素")
        else:
            status.update("デバッグ", "QRモーダルCSS非表示設定済み")
        time.sleep(0.5)
    except Exception as e:
        status.update("デバッグ", f"QRモーダル強制削除失敗: {e}")


def _classify_file_inputs(page: Page, fi_count: int, status: UploadStatus) -> dict:
    """file inputをaccept属性・親要素のテキストで分類する。
    証拠(session ecc8cc27): fi_count=11でnth(0)がZIP用inputの可能性
    → main/tabにスタンプ画像が入りError

    Returns:
        {"main": int|None, "tab": int|None, "stickers": [int,...], "zip": int|None}
    """
    result = {"main": None, "tab": None, "stickers": [], "zip": None}
    try:
        info = page.evaluate(f"""
            (() => {{
                const inputs = document.querySelectorAll('input[type="file"]');
                const result = [];
                for (let i = 0; i < inputs.length; i++) {{
                    const inp = inputs[i];
                    const accept = inp.getAttribute('accept') || '';
                    // 親要素のテキストでスロット種別を判定
                    let parentText = '';
                    let el = inp.parentElement;
                    for (let j = 0; j < 5 && el; j++) {{
                        const label = el.querySelector('span, p, div, label');
                        if (label) {{
                            parentText = label.textContent.trim().slice(0, 50);
                            break;
                        }}
                        el = el.parentElement;
                    }}
                    // data属性やクラスも収集
                    const cls = (inp.className || '') + ' ' + (inp.parentElement?.className || '');
                    result.push({{
                        index: i,
                        accept: accept,
                        parentText: parentText,
                        cls: cls.slice(0, 100)
                    }});
                }}
                return result;
            }})()
        """)
        status.update("デバッグ", f"file input分類: {info}")

        for item in info:
            idx = item["index"]
            accept = item.get("accept", "")
            parent = item.get("parentText", "")

            # ZIP用 or 画像以外のinput判定
            # 証拠(session 9d8295be): index 0 は accept='', cls='mdBtn mdBtnLabel'
            # → ZIPアップロードボタンだがacceptが空文字
            if ".zip" in accept or "zip" in accept.lower():
                result["zip"] = idx
                continue
            # accept属性がimage/pngでない場合はスキップ（ZIP等の非画像input）
            if accept and "image" not in accept:
                result["zip"] = idx
                continue
            if not accept and not parent:
                # accept属性もラベルもない → ZIPボタン等の非画像input
                status.update("デバッグ", f"input[{idx}] スキップ: accept='{accept}', parent='{parent}'")
                result["zip"] = idx
                continue

            # ラベルテキストでmain/tab判定
            lower_parent = parent.lower()
            if "main" in lower_parent or "メイン" in parent:
                result["main"] = idx
            elif "tab" in lower_parent or "タブ" in parent or "トークルーム" in parent:
                result["tab"] = idx
            else:
                # 番号付きスタンプスロット
                result["stickers"].append(idx)

        # main/tabが見つからない場合、stickersの先頭2つをmain/tabとして使う
        if result["main"] is None and len(result["stickers"]) > len(info) - 3:
            result["main"] = result["stickers"].pop(0)
        if result["tab"] is None and len(result["stickers"]) > len(info) - 3:
            result["tab"] = result["stickers"].pop(0)

        status.update("デバッグ", f"分類結果: main={result['main']}, tab={result['tab']}, "
                       f"stickers={result['stickers']}, zip={result['zip']}")
    except Exception as e:
        status.update("デバッグ", f"file input分類失敗: {e}")
        # フォールバック: 従来のオフセット方式
        result["main"] = 0
        result["tab"] = 1
        result["stickers"] = list(range(2, fi_count))

    return result


def _check_dont_show_again(page: Page, status: UploadStatus):
    """「今後、この画面を表示しない」チェックボックスがあればチェック。"""
    try:
        cb = page.locator("input[type='checkbox']")
        for i in range(cb.count()):
            el = cb.nth(i)
            if el.is_visible(timeout=500):
                el.check(force=True)
                status.update("デバッグ", "「今後表示しない」チェックボックスをチェック")
                time.sleep(0.5)
                return
    except Exception:
        pass


def _extract_user_path(page: Page) -> Optional[str]:
    """ダッシュボードURLからユーザーパスを抽出する。
    例: /my/dSCMSb2IRvLYU4oe/sticker/ → /my/dSCMSb2IRvLYU4oe
    page.url はキャッシュ値で古い可能性があるため _get_real_url() を使用。
    """
    url = _get_real_url(page)
    match = re.search(r"(/my/[^/]+)", url)
    if match:
        return match.group(1)
    return None


def _ensure_on_dashboard(page: Page, status: UploadStatus) -> bool:
    """ダッシュボードにいることを確認し、いなければ戻る。"""
    if _is_on_dashboard(page):
        return True
    status.update("自動処理中", "ダッシュボードに移動中...", 25)
    return _try_navigate_to_dashboard(page, status)


def _click_new_registration(page: Page, status: UploadStatus) -> bool:
    """「新規登録」ボタンをクリックする。
    証拠: ログより href=/my/{userId}/create が正しいURL。
    el.click()はSPAルーティングで失敗する可能性があるため、page.goto(href)を最優先。"""

    original_url = page.url

    # 戦略1（最優先）: hrefを抽出して page.goto() で直接遷移
    # el.click()はSPAのisTrustedチェックで失敗する可能性があるため、goto()が最も確実
    try:
        href = page.evaluate("""
            (() => {
                const elements = document.querySelectorAll('a[href]');
                for (const el of elements) {
                    const text = (el.textContent || '').trim();
                    if (text.includes('新規登録')) {
                        return el.href;  // 絶対URLが返る
                    }
                }
                return null;
            })()
        """)
        if href:
            status.update("デバッグ", f"「新規登録」href={href}")
            page.goto(href, timeout=15000)
            _wait_for_page_ready(page)
            if page.url != original_url:
                status.update("デバッグ", f"page.goto()遷移成功: {page.url}")
                return True
            status.update("デバッグ", "goto後もURL変化なし")
    except Exception as e:
        status.update("デバッグ", f"href遷移失敗: {e}")

    # 戦略2: Playwrightのforce=Trueクリック
    try:
        btn = page.get_by_text("新規登録", exact=False).first
        btn.click(force=True)
        time.sleep(3)
        _wait_for_page_ready(page)
        if page.url != original_url:
            status.update("デバッグ", f"Playwrightクリック遷移成功: {page.url}")
            return True
    except Exception as e:
        status.update("デバッグ", f"Playwrightクリック失敗: {e}")

    # 戦略3: JavaScriptで直接クリック
    try:
        page.evaluate("""
            (() => {
                const elements = document.querySelectorAll('a, button, [role="button"]');
                for (const el of elements) {
                    const text = (el.textContent || '').trim();
                    if (text.includes('新規登録')) {
                        el.click();
                        return;
                    }
                }
            })()
        """)
        time.sleep(3)
        _wait_for_page_ready(page)
        if page.url != original_url:
            status.update("デバッグ", f"JSクリック遷移成功: {page.url}")
            return True
    except Exception as e:
        status.update("デバッグ", f"JSクリック失敗: {e}")

    status.update("デバッグ", f"新規登録クリック全戦略失敗。URL変化なし: {page.url}")
    return False


def navigate_to_new_sticker(page: Page, status: UploadStatus, create_url: str = None) -> bool:
    """スタンプ新規作成ページに遷移する。

    Args:
        page: Playwrightページ
        status: ステータス管理
        create_url: ダッシュボードから取得した新規登録URL（/my/{userId}/create）
    """
    status.update("自動処理中", "新規登録ページに移動中...", 25)

    on_create_page = False

    # 方法1: ダッシュボードから取得した正しいURLで直接遷移
    # 重要: /my/sticker/new/ は存在しないURLでダッシュボードにリダイレクトされる
    # 正しいURLは /my/{userId}/create の形式
    target_url = create_url
    if not target_url:
        # ユーザーパスからURLを構築
        user_path = _extract_user_path(page)
        if user_path:
            target_url = f"https://creator.line.me{user_path}/create"

    if target_url:
        try:
            status.update("デバッグ", f"作成ページに直接遷移: {target_url}")
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
            _wait_for_page_ready(page)
            actual_url = _get_real_url(page)
            status.save_screenshot(page, "03_direct_nav")
            status.update("デバッグ", f"直接遷移後URL: {actual_url}")
            if "/create" in actual_url or "/new" in actual_url:
                on_create_page = True
            else:
                status.update("デバッグ", f"直接遷移失敗（リダイレクト先: {actual_url[:80]}）、ダッシュボード経由に切り替え")
        except Exception as e:
            status.update("デバッグ", f"直接遷移失敗: {e}")

    # 方法2: ダッシュボード経由
    if not on_create_page:
        if not _ensure_on_dashboard(page, status):
            status.update("エラー", "ダッシュボードに到達できませんでした。もう一度お試しください。")
            return False

        _wait_for_page_ready(page)
        time.sleep(2)
        _dismiss_all_modals(page, status)
        status.save_screenshot(page, "03_dashboard")

        status.update("自動処理中", "新規登録の準備中...", 27)
        if not _click_new_registration(page, status):
            status.save_screenshot(page, "03_new_reg_failed")
            status.update("エラー", "新規登録ページに移動できませんでした。もう一度お試しください。")
            return False

        time.sleep(3)
        _wait_for_page_ready(page)

    status.save_screenshot(page, "04_after_new_reg_click")
    status.update("デバッグ", f"作成ページURL: {_get_real_url(page)}")

    # ---- スタンプタイプ選択 ----
    # /create ページでは画面下部にスタンプ/絵文字/着せかえの3つの緑ボタンが表示される
    # 証拠(ユーザースクリーンショット): 3つの緑ボタンがページ下部に配置
    # 最も確実な方法: ボタンのhrefを取得してpage.goto()で直接遷移
    # （el.click()はSPAのisTrustedチェックで失敗する可能性があるためgoto()を優先）
    time.sleep(2)
    type_select_url = _get_real_url(page)
    sticker_type_selected = False

    # 戦略1（最優先）: hrefを抽出してpage.goto()で直接遷移
    try:
        stamp_href = page.evaluate("""
            (() => {
                const links = document.querySelectorAll('a[href]');
                const excluded = ['promotion', 'guideline', 'review', 'stats', 'studio',
                                  'pr-sticker', 'prsticker', 'line-pr'];
                for (const el of links) {
                    const text = (el.textContent || '').trim();
                    const href = el.href || '';
                    const hrefLower = href.toLowerCase();
                    // サイドバーやフッターのリンクを除外
                    if (excluded.some(ex => hrefLower.includes(ex))) continue;
                    // テキストが正確に「スタンプ」のみ（「LINE PRスタンプ」等を除外）
                    if (text === 'スタンプ' || text === 'Stickers') {
                        return href;
                    }
                }
                return null;
            })()
        """)
        if stamp_href:
            status.update("デバッグ", f"スタンプボタンhref: {stamp_href}")
            page.goto(stamp_href, timeout=30000)
            time.sleep(3)
            _wait_for_page_ready(page)
            new_url = _get_real_url(page)
            if new_url != type_select_url:
                sticker_type_selected = True
                status.update("自動処理中", "スタンプの種類を選択中...", 32)
            else:
                status.update("デバッグ", "goto後もURL変化なし")
    except Exception as e:
        status.update("デバッグ", f"href遷移失敗: {e}")

    # 戦略2: Playwrightネイティブクリック（スクロール+force=True）
    if not sticker_type_selected:
        try:
            stamp_btn = page.get_by_text("スタンプ", exact=True)
            stamp_count = stamp_btn.count()
            status.update("デバッグ", f"「スタンプ」ボタン候補: {stamp_count}個")
            for idx in range(stamp_count):
                btn = stamp_btn.nth(idx)
                try:
                    if not btn.is_visible(timeout=2000):
                        continue
                    # サイドバーリンクを除外（親要素のテキストで判定）
                    parent_text = btn.evaluate(
                        "e => (e.closest('a') || e.closest('li') || e).textContent || ''"
                    ).strip()
                    if len(parent_text) > 10:
                        status.update("デバッグ", f"除外（サイドバー要素）: '{parent_text[:30]}'")
                        continue
                    # ボタンが画面内に見えるようスクロール
                    btn.scroll_into_view_if_needed()
                    time.sleep(0.5)
                    btn.click(force=True)
                    time.sleep(3)
                    _wait_for_page_ready(page)
                    new_url = _get_real_url(page)
                    if new_url != type_select_url:
                        sticker_type_selected = True
                        status.update("自動処理中", "スタンプの種類を選択中...", 32)
                        break
                    status.update("デバッグ", f"クリック後URL変化なし: idx={idx}")
                except Exception as e:
                    status.update("デバッグ", f"ボタン[{idx}]クリック失敗: {e}")
                    continue
        except Exception as e:
            status.update("デバッグ", f"Playwrightクリック失敗: {e}")

    # 戦略3: JavaScript el.click() フォールバック
    if not sticker_type_selected:
        try:
            result = page.evaluate("""
                (() => {
                    const links = document.querySelectorAll('a[href], button, [role="button"]');
                    const excluded = ['promotion', 'guideline', 'review', 'stats', 'studio'];
                    for (const el of links) {
                        const text = (el.textContent || '').trim();
                        const href = el.href || el.getAttribute('href') || '';
                        if ((text === 'スタンプ' || text === 'Stickers') &&
                            !excluded.some(ex => href.includes(ex))) {
                            el.scrollIntoView({block: 'center'});
                            el.click();
                            return {text, href, tag: el.tagName};
                        }
                    }
                    return null;
                })()
            """)
            if result:
                status.update("デバッグ", f"JSクリック: {result}")
                time.sleep(3)
                _wait_for_page_ready(page)
                new_url = _get_real_url(page)
                if new_url != type_select_url:
                    sticker_type_selected = True
        except Exception as e:
            status.update("デバッグ", f"JSクリック失敗: {e}")

    if sticker_type_selected:
        status.save_screenshot(page, "05_after_sticker_select")
    else:
        status.update("デバッグ", "「スタンプ」ボタンをクリックできませんでした")
        status.save_screenshot(page, "05_sticker_btn_failed")

        # ★重要: タイプ選択ページから動けていない場合は即座に失敗
        # 証拠: session 34147374 等で全スクリーンショットが同じ（タイプ選択ページのまま）
        # → コードが先に進んでフォーム入力・保存を試みるが全て失敗し、偽の「完了」になる
        still_on_type_select = False
        try:
            still_on_type_select = page.evaluate("""
                (() => {
                    const links = document.querySelectorAll('a[href]');
                    for (const el of links) {
                        const text = (el.textContent || '').trim();
                        if (text === 'スタンプ' || text === 'Stickers') return true;
                    }
                    // 3つの緑ボタンがある = まだタイプ選択ページ
                    const buttons = document.querySelectorAll('a');
                    let typeButtonCount = 0;
                    for (const el of buttons) {
                        const text = (el.textContent || '').trim();
                        if (['スタンプ', '絵文字', '着せかえ', 'Stickers', 'Emoji', 'Themes'].includes(text)) {
                            typeButtonCount++;
                        }
                    }
                    return typeButtonCount >= 2;
                })()
            """)
        except Exception:
            pass

        if still_on_type_select:
            status.update("デバッグ", "スタンプタイプ選択ページから遷移できませんでした。即座に失敗を返します。")
            return False

    # 誤ナビゲーション検知（promotionページに飛んでいないか）
    current_url = _get_real_url(page)
    if "promotion" in current_url:
        status.update("デバッグ", "⚠️ promotionページに誤遷移！作成ページに戻ります")
        page.go_back()
        _wait_for_page_ready(page)
        time.sleep(2)
        current_url = _get_real_url(page)

    status.update("デバッグ", f"タイプ選択後URL: {current_url}")

    # スタンプ作成フォームの検出（最大5回リトライ）
    # フォームにはtextarea（説明文欄）やtext input（タイトル欄）が存在する
    # 成功セッション(a2b9c710)の証拠: 新規登録フォームには
    # - textarea（スタンプ説明文）
    # - 複数のinput[type='text']（タイトル、コピーライト等）
    # - ラジオボタン（スタンプのタイプ選択）
    # - 「保存」「キャンセル」ボタン
    for retry in range(5):
        textarea_count = page.locator("textarea").count()
        text_input_count = page.locator("input[type='text']").count()
        radio_count = page.locator("input[type='radio']").count()
        current_url = _get_real_url(page)

        # URLベースの判定
        is_create_url = "/create" in current_url or "/new" in current_url or "/edit" in current_url

        status.update("デバッグ", f"フォーム検出[{retry+1}/5]: textarea={textarea_count}, "
                       f"input[text]={text_input_count}, radio={radio_count}, is_create_url={is_create_url}")

        # フォーム検出条件（厳格版）:
        # 証拠(成功セッションa2b9c710のdebug_05): フォームにはtextarea+text input+radioが揃っている
        # 条件1: textareaがある = 説明文欄がある（フォーム確定）
        if textarea_count > 0:
            status.update("自動処理中", "スタンプ情報の入力を準備中...", 35)
            status.save_screenshot(page, "05_form_found")
            return True

        # 条件2: ラジオボタン（スタンプのタイプ選択）+ テキスト入力があるフォーム
        if radio_count > 0 and text_input_count >= 1:
            status.update("自動処理中", "スタンプ情報の入力を準備中...", 35)
            status.save_screenshot(page, "05_form_found")
            return True

        # 条件3: text inputが3個以上（タイトル + コピーライト + 言語等）
        # 注意: 2個では不十分（タイプ選択ページにも隠しinputがある可能性）
        if text_input_count >= 3:
            status.update("自動処理中", "スタンプ情報の入力を準備中...", 35)
            status.save_screenshot(page, "05_form_found")
            return True

        if retry < 4:
            status.update("デバッグ", f"フォーム待機中... ({retry+1}/5)")
            time.sleep(3)

    # 最終診断: ページの全要素をダンプ
    status.save_screenshot(page, "05_no_form")
    status.dump_page_info(page, "フォーム未検出")

    # /create URLにいるだけではフォームとみなさない
    # 理由: /create は作成タイプ選択ページ（イラスト表示）であり、
    # スタンプタイプ選択が完了するまでフォームは表示されない。
    # ここでTrueを返すと、フォームが無いのに入力・保存を試みて失敗する。
    status.update("デバッグ", "スタンプ作成フォームが表示されませんでした。")
    return False


# ============================================================
# フォーム入力・画像アップロード
# ============================================================

def fill_sticker_info(page: Page, title: str, description: str, status: UploadStatus, creator_name: str = ""):
    """スタンプのタイトル・説明文を入力する。"""
    status.update("自動処理中", "スタンプ情報を入力中...", 40)
    status.save_screenshot(page, "06_before_fill")

    inputs = page.locator("input[type='text']")
    textareas = page.locator("textarea")

    # タイトル入力
    try:
        count = inputs.count()
        status.update("デバッグ", f"テキスト入力欄: {count}個")
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
                status.update("デバッグ", f"タイトル入力: {title}")
                title_filled = True
                break

        if not title_filled and count > 0:
            for i in range(count):
                if inputs.nth(i).is_visible():
                    inputs.nth(i).fill(title)
                    status.update("デバッグ", f"タイトル（最初のinput）: {title}")
                    break
    except Exception as e:
        status.update("デバッグ", f"タイトル入力失敗: {e}")

    # 説明文入力
    try:
        ta_count = textareas.count()
        status.update("デバッグ", f"テキストエリア: {ta_count}個")
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
                status.update("デバッグ", f"説明入力: {description}")
                desc_filled = True
                break

        if not desc_filled and ta_count > 0:
            for i in range(ta_count):
                if textareas.nth(i).is_visible():
                    textareas.nth(i).fill(description)
                    status.update("デバッグ", f"説明（最初のtextarea）: {description}")
                    break
    except Exception as e:
        status.update("デバッグ", f"説明文入力失敗: {e}")

    # コピーライト入力（空なら入力）
    try:
        copyright_filled = False
        for i in range(inputs.count()):
            inp = inputs.nth(i)
            if not inp.is_visible():
                continue
            name = (inp.get_attribute("name") or "").lower()
            if "copyright" in name:
                current_val = inp.input_value()
                if not current_val.strip():
                    copyright_val = creator_name if creator_name else title.split()[0] if title else "Creator"
                    inp.fill(copyright_val)
                    status.update("デバッグ", f"コピーライト入力: {copyright_val}")
                else:
                    status.update("デバッグ", f"コピーライト既入力: {current_val}")
                copyright_filled = True
                break
        if not copyright_filled:
            status.update("デバッグ", "コピーライトフィールド未検出")
    except Exception as e:
        status.update("デバッグ", f"コピーライト入力失敗: {e}")

    # AIの使用チェック（AI生成スタンプなので「AIを使用しています」を選択）
    try:
        ai_checked = page.evaluate("""
            (() => {
                // ラジオボタンやチェックボックスで「AIを使用」を探す
                const radios = document.querySelectorAll('input[type="radio"]');
                for (const radio of radios) {
                    const label = radio.closest('label') || document.querySelector('label[for="' + radio.id + '"]');
                    const text = label ? label.textContent.trim() : '';
                    if (text.includes('AIを使用しています') || text.includes('AI')) {
                        if (!radio.checked) {
                            radio.click();
                            return 'checked: ' + text;
                        }
                        return 'already checked: ' + text;
                    }
                }
                // name属性で探す
                const aiRadio = document.querySelector('input[name*="ai"], input[name*="AI"]');
                if (aiRadio && !aiRadio.checked) {
                    aiRadio.click();
                    return 'checked by name';
                }
                return null;
            })()
        """)
        if ai_checked:
            status.update("デバッグ", f"AI使用: {ai_checked}")
        else:
            status.update("デバッグ", "AI使用フィールド未検出")
    except Exception as e:
        status.update("デバッグ", f"AI使用チェック失敗: {e}")

    time.sleep(1)
    status.save_screenshot(page, "07_after_fill")


def submit_creation_form(page: Page, status: UploadStatus) -> bool:
    """新規登録フォームの「保存」ボタンをクリックし、編集ページに遷移する。

    証拠:
    - session bd0a2751: サイドバー「新規登録」を誤クリック → /create に遷移
    - session d1f7b5d4: 保存ボタンがJS要素スキャンで検出されず → form.submit()実行
      → URL変化(sticker/43339793)で成功と判定したが、POST→405エラー
    - debug_07b_scrolled_bottom.png: 緑の「保存」ボタンが「キャンセル」横に確認済み

    対策:
    - 全要素タイプ(*) でテキスト「保存」を検索（button/a/input以外の可能性）
    - 「キャンセル」の隣の要素を探す
    - form.submit()後に405を検知したらGETで再読み込み
    """
    status.update("自動処理中", "スタンプ情報を保存中...", 55)

    # 重要: page.url はSPAでは古い値を返すことがある
    # → _get_real_url() (window.location.href) を使う
    url_before = _get_real_url(page)

    # ページ下部にスクロール
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)
    except Exception:
        pass

    status.save_screenshot(page, "07b_scrolled_bottom")

    # デバッグ: 「キャンセル」「保存」周辺の全要素をスキャン（タグ種類を問わない）
    try:
        all_elements = page.evaluate("""
            (() => {
                const results = [];
                // 全クリッカブル要素を広く検索
                const all = document.querySelectorAll('button, input, a, [role="button"], [onclick], [class*="btn"], [class*="button"]');
                for (const el of all) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    if (rect.x < 250) continue; // サイドバー除外
                    const text = (el.textContent || el.value || '').trim();
                    if (!text) continue;
                    // 長いツールチップテキストは除外（50文字以上）
                    if (text.length > 50) continue;
                    const tag = el.tagName;
                    const href = el.getAttribute('href') || '';
                    const type = el.getAttribute('type') || '';
                    const cls = (el.className?.toString() || '').slice(0, 60);
                    results.push({tag, text, href, type, cls, x: Math.round(rect.x), y: Math.round(rect.y)});
                }
                return results;
            })()
        """)
        for el in all_elements:
            status.update("デバッグ", f"[要素] {el['tag']} \"{el['text']}\" href={el['href']} type={el['type']} class={el['cls']} pos=({el['x']},{el['y']})")
    except Exception as e:
        status.update("デバッグ", f"要素スキャン失敗: {e}")

    save_clicked = False

    # 戦略1: 全要素から「保存」テキストを持つ要素をクリック
    # スクショで確認: 緑の「保存」ボタンが「キャンセル」の横にある
    try:
        result = page.evaluate("""
            (() => {
                // 非常に広いセレクタで全クリッカブル要素を検索
                const all = document.querySelectorAll('button, input, a, [role="button"], [onclick], [class*="btn"], [class*="button"], span, div');
                const reversed = Array.from(all).reverse();

                for (const el of reversed) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    if (rect.x < 250) continue;

                    // テキスト取得（input要素はvalueも確認）
                    let text = '';
                    if (el.tagName === 'INPUT') {
                        text = (el.value || '').trim();
                    } else {
                        // 直接のテキストノードのみ（子要素のテキストは含めない → ツールチップ回避）
                        text = '';
                        for (const node of el.childNodes) {
                            if (node.nodeType === 3) text += node.textContent;
                        }
                        text = text.trim();
                        // 直接テキストが空なら全テキストを使う
                        if (!text) text = (el.textContent || '').trim();
                    }

                    if (text === '保存' || text === 'Save') {
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return {text, tag: el.tagName, href: el.getAttribute('href') || '',
                                cls: (el.className?.toString() || '').slice(0, 60),
                                x: Math.round(rect.x), y: Math.round(rect.y)};
                    }
                }
                return null;
            })()
        """)
        if result:
            status.update("デバッグ", f"「保存」ボタンクリック: {result}")
            save_clicked = True
    except Exception as e:
        status.update("デバッグ", f"保存ボタン検索失敗: {e}")

    # 戦略2: Playwright locator で「保存」テキストを探す
    if not save_clicked:
        try:
            save_btn = page.get_by_text("保存", exact=True)
            if save_btn.count() > 0:
                save_btn.last.scroll_into_view_if_needed()
                save_btn.last.click()
                status.update("デバッグ", "Playwright get_by_text('保存') クリック")
                save_clicked = True
        except Exception as e:
            status.update("デバッグ", f"get_by_text('保存')失敗: {e}")

    # 戦略3: input[type=submit] / button[type=submit]
    if not save_clicked:
        try:
            submit_btn = page.locator("input[type='submit'], button[type='submit']")
            if submit_btn.count() > 0:
                submit_btn.first.scroll_into_view_if_needed()
                submit_btn.first.click()
                status.update("デバッグ", "submitボタンをクリック")
                save_clicked = True
        except Exception:
            pass

    # 戦略4（最終手段）: form.submit()
    # 注意: POST送信のため405になる可能性あり → 後で検知してGETリロード
    if not save_clicked:
        try:
            submitted = page.evaluate("""
                (() => {
                    const form = document.querySelector('form');
                    if (form) {
                        form.submit();
                        return true;
                    }
                    return false;
                })()
            """)
            if submitted:
                status.update("デバッグ", "form.submit()を実行（405の可能性あり）")
                save_clicked = True
        except Exception as e:
            status.update("デバッグ", f"form.submit()失敗: {e}")

    if not save_clicked:
        status.update("警告", "保存ボタンが見つかりません")
        status.save_screenshot(page, "07c_no_save_button")
        status.dump_page_info(page, "保存ボタン未検出")
        return False

    # 確認ダイアログ「保存しますか？」の処理
    # 証拠(session 8a2f7dad): 保存ボタン(SPAN)クリック後に確認モーダルが出現
    # 「キャンセル」「OK」ボタンが表示される → OKをクリックする必要がある
    time.sleep(2)
    status.save_screenshot(page, "07c_after_save_click")

    ok_clicked = False

    # 方法1: Playwright get_by_text で「OK」を探す
    try:
        ok_btn = page.get_by_text("OK", exact=True)
        if ok_btn.count() > 0:
            ok_btn.last.click()
            status.update("デバッグ", "確認ダイアログ「OK」クリック")
            ok_clicked = True
    except Exception as e:
        status.update("デバッグ", f"OK検索失敗(get_by_text): {e}")

    # 方法2: get_by_role で確認ボタンを探す
    if not ok_clicked:
        try:
            for role in ["button", "link"]:
                ok_role = page.get_by_role(role, name="OK")
                if ok_role.count() > 0:
                    ok_role.last.click()
                    status.update("デバッグ", f"確認ダイアログ「OK」クリック (role={role})")
                    ok_clicked = True
                    break
        except Exception as e:
            status.update("デバッグ", f"OK検索失敗(get_by_role): {e}")

    # 方法3: JSで確認ダイアログ内の「OK」ボタンを探してクリック
    if not ok_clicked:
        try:
            js_ok = page.evaluate("""
                (() => {
                    const all = document.querySelectorAll('button, a, [role="button"], span, div');
                    for (const el of Array.from(all).reverse()) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        let text = '';
                        for (const node of el.childNodes) {
                            if (node.nodeType === 3) text += node.textContent;
                        }
                        text = text.trim();
                        if (!text) text = (el.textContent || '').trim();
                        if (text === 'OK' || text === 'はい' || text === '確認') {
                            el.click();
                            return {text, tag: el.tagName};
                        }
                    }
                    return null;
                })()
            """)
            if js_ok:
                status.update("デバッグ", f"確認ダイアログクリック(JS): {js_ok}")
                ok_clicked = True
        except Exception as e:
            status.update("デバッグ", f"OK検索失敗(JS): {e}")

    if ok_clicked:
        status.update("自動処理中", "保存中...", 58)

    # ページ遷移を待つ
    time.sleep(5)
    _wait_for_page_ready(page)

    # 重要: page.url はSPAでは古い値を返すことがある
    # 証拠(session ff43d4f5, 18be03b0): 保存成功(sticker/43404922)なのに
    # page.urlが古いままで失敗と判定されていた
    new_url = _get_real_url(page)
    status.update("デバッグ", f"保存後URL: {new_url}")

    # 405 Method Not Allowed 検知 → GETで再読み込み
    # 証拠(session d1f7b5d4): form.submit() → POST → 405
    # しかしURLにスタンプIDが含まれている = スタンプ自体は作成済み
    try:
        page_title = page.title()
        if "405" in page_title or "Method Not Allowed" in page_title:
            status.update("デバッグ", f"405検知。GETで再読み込み: {new_url}")
            page.goto(new_url, timeout=15000)
            _wait_for_page_ready(page)
            new_url = _get_real_url(page)
            status.update("デバッグ", f"再読み込み後: {new_url}")
    except Exception as e:
        status.update("デバッグ", f"405リカバリ失敗: {e}")

    status.save_screenshot(page, "08_after_save")

    # URL変化確認
    if new_url != url_before:
        # /create に戻った場合は失敗
        if new_url.endswith("/create") and "/sticker/" not in new_url:
            status.update("警告", "⚠️ /create に戻りました")
            page.go_back()
            _wait_for_page_ready(page)
            return False
        status.update("自動処理中", "保存完了！画像アップロードの準備中...", 60)
        return True

    # URLが変わらない場合
    status.dump_page_info(page, "保存後")
    return False


def upload_images(page: Page, output_dir: Path, status: UploadStatus):
    """スタンプ画像をアップロードする。
    LINE Creators Marketの編集ページでは、個別のスタンプスロットに画像をアップする。
    ページ構造によって複数の方法を試す。"""
    stamp_files = sorted(output_dir.glob("[0-9][0-9].png"))
    total = len(stamp_files)
    status.update("画像アップ", f"スタンプ画像をアップロード中... ({total}枚)", 65)

    # モーダルを全て閉じる（QRコードモーダル含む）
    _force_dismiss_qr_modal(page, status)
    _dismiss_all_modals(page, status)
    time.sleep(1)

    # 「スタンプ画像」タブに切り替え
    # 証拠(session b45b0b87): 保存後は表示情報タブのままでfile input数=0
    try:
        # SPAナビゲーション: URLフラグメントで#/imageに遷移
        current_url = _get_real_url(page)
        if "#/image" not in current_url:
            # まずタブクリックを試行
            tab_clicked = False
            for tab_text in ["スタンプ画像", "Sticker Images"]:
                try:
                    tab = page.get_by_text(tab_text, exact=True)
                    if tab.count() > 0 and tab.first.is_visible(timeout=2000):
                        tab.first.click()
                        tab_clicked = True
                        status.update("画像アップ", "スタンプ画像を準備中...")
                        time.sleep(2)
                        break
                except Exception:
                    continue
            if not tab_clicked:
                # URLフラグメントで直接遷移
                base_url = current_url.split("#")[0]
                page.goto(f"{base_url}#/image", timeout=15000)
                status.update("画像アップ", "スタンプ画像を準備中...")
                time.sleep(2)
            # 遷移後にモーダルが再出現する可能性
            _force_dismiss_qr_modal(page, status)
    except Exception as e:
        status.update("デバッグ", f"スタンプ画像タブ遷移失敗: {e}")

    # 「編集」ボタンをクリックして編集モードに入る
    # 証拠(session b45b0b87-2回目): スタンプ画像タブは閲覧モードでfile input=0
    # 「編集」ボタンを押さないとアップロードUIが出ない
    try:
        edit_clicked = False
        for edit_text in ["編集", "Edit"]:
            try:
                edit_btn = page.get_by_text(edit_text, exact=True)
                count = edit_btn.count()
                for idx in range(count):
                    btn = edit_btn.nth(idx)
                    try:
                        if btn.is_visible(timeout=2000):
                            # ボタンまたはリンクであることを確認
                            tag = btn.evaluate("e => e.tagName")
                            if tag in ["BUTTON", "A", "SPAN", "DIV"]:
                                btn.click()
                                edit_clicked = True
                                status.update("画像アップ", "画像の編集モードに移行中...")
                                time.sleep(3)
                                # 編集モード遷移後にモーダル対策
                                _force_dismiss_qr_modal(page, status)
                                break
                    except Exception:
                        continue
                if edit_clicked:
                    break
            except Exception:
                continue
        if not edit_clicked:
            # URLに#/editを付与して試行
            try:
                current_url = _get_real_url(page)
                base_url = current_url.split("#")[0]
                page.goto(f"{base_url}#/image/edit", timeout=15000)
                status.update("画像アップ", "画像の編集モードに移行中...")
                time.sleep(3)
                _force_dismiss_qr_modal(page, status)
            except Exception as e:
                status.update("デバッグ", f"編集モード遷移失敗: {e}")
    except Exception as e:
        status.update("デバッグ", f"編集ボタンクリック失敗: {e}")

    # デバッグ: 現在のページ構造を記録
    status.save_screenshot(page, "09_upload_page")
    status.dump_page_info(page, "画像アップロードページ")

    # ZIPファイルで一括アップロード（最も確実な方法）
    zip_file = output_dir / "line_stamp.zip"
    status.update("画像アップ", "画像をアップロードしています...", 66)

    if zip_file.exists():
        # ZIPアップロード用のfile inputを探す
        file_inputs = page.locator("input[type='file']")
        fi_count = file_inputs.count()
        status.update("デバッグ", f"file input数: {fi_count}")

        zip_uploaded = False
        for i in range(fi_count):
            try:
                accept = file_inputs.nth(i).get_attribute("accept") or ""
                # ZIP用input: accept属性が空 or .zipを含む
                if ".zip" in accept or not accept or "image" not in accept:
                    file_inputs.nth(i).set_input_files(str(zip_file))
                    status.update("画像アップ", "ZIPファイルをアップロード中...", 80)
                    zip_uploaded = True
                    # アップロード処理完了を待つ
                    time.sleep(5)
                    break
            except Exception as e:
                status.update("デバッグ", f"ZIP input[{i}] 失敗: {e}")
                continue

        if zip_uploaded:
            # アップロード後にエラーがないか確認
            time.sleep(3)
            status.save_screenshot(page, "10_after_upload")
            # エラー表示を確認
            error_text = ""
            try:
                error_el = page.locator(".mdAlert, .error, [class*='error']").first
                if error_el.is_visible(timeout=2000):
                    error_text = error_el.inner_text()
            except Exception:
                pass
            if error_text:
                status.update("デバッグ", f"アップロードエラー: {error_text}")
            else:
                status.update("画像アップ", "画像アップロード完了！", 95)
            return

    # フォールバック: ZIPが無い場合は個別アップロード
    status.update("デバッグ", "ZIPアップロード不可、個別アップロードに切り替え")

    # 方法3: スタンプスロットをクリックしてfile inputを出現させる
    # 編集ページではスタンプアイテムの空スロットをクリックするとfile inputが出る
    uploaded_count = 0
    for i, stamp_file in enumerate(stamp_files):
        progress = 65 + int((i + 1) / total * 25)
        try:
            # 毎回モーダルを強制削除（ページ操作中に再出現するため）
            _force_dismiss_qr_modal(page, status)

            # 空のスタンプスロット/追加ボタンを探す
            slot_clicked = False

            # クリッカブルなスロット要素を探す
            slot_selectors = [
                "[class*='sticker-item'] [class*='add'], [class*='sticker-item'] [class*='empty']",
                "[class*='stamp'] [class*='add'], [class*='stamp'] [class*='empty']",
                "[class*='upload-area'], [class*='drop-zone']",
                "button:has-text('追加'), button:has-text('アップロード'), button:has-text('Upload')",
                "a:has-text('追加'), a:has-text('アップロード')",
            ]
            for selector in slot_selectors:
                try:
                    slots = page.locator(selector)
                    if slots.count() > 0:
                        slots.first.click()
                        slot_clicked = True
                        status.update("画像アップ", f"スタンプ画像をアップロード中... ({i+1}/{total}枚)")
                        time.sleep(2)
                        break
                except Exception:
                    continue

            # file inputを再チェック
            file_inputs = page.locator("input[type='file']")
            fi_count = file_inputs.count()

            if fi_count > 0:
                # 空のfile inputを探す
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
                    file_inputs.last.set_input_files(str(stamp_file))

                status.update("画像アップ", f"スタンプ画像をアップロード中... ({i+1}/{total}枚)", progress)
                uploaded_count += 1
                time.sleep(3)
            else:
                # file inputがない場合、filechooserイベントを使う
                try:
                    if not slot_clicked:
                        # クリック対象を探す
                        clickable = page.locator(
                            "[class*='add'], [class*='plus'], [class*='upload']"
                        )
                        if clickable.count() > 0:
                            with page.expect_file_chooser(timeout=5000) as fc_info:
                                clickable.first.click()
                            file_chooser = fc_info.value
                            file_chooser.set_files(str(stamp_file))
                            status.update("画像アップ", f"スタンプ画像をアップロード中... ({i+1}/{total}枚)", progress)
                            uploaded_count += 1
                            time.sleep(3)
                        else:
                            status.update("デバッグ", f"[{i+1}/{total}] アップロード手段なし")
                    else:
                        # スロットはクリックしたがfile inputが出ない
                        try:
                            with page.expect_file_chooser(timeout=5000) as fc_info:
                                pass  # 既にクリック済み
                            file_chooser = fc_info.value
                            file_chooser.set_files(str(stamp_file))
                            uploaded_count += 1
                            time.sleep(3)
                        except Exception:
                            status.update("デバッグ", f"[{i+1}/{total}] filechooser待機タイムアウト")
                except Exception as e:
                    status.update("デバッグ", f"[{i+1}/{total}] {stamp_file.name} 失敗: {e}")

        except Exception as e:
            status.update("デバッグ", f"[{i+1}/{total}] {stamp_file.name} 失敗: {e}")

    status.save_screenshot(page, "10_after_upload")
    status.update("画像アップ", f"画像アップロード完了！({uploaded_count}/{total}枚成功)", 95)


def _submit_review_request(page: Page, status: UploadStatus) -> bool:
    """画像アップロード後、審査リクエストボタンを押して審査提出する。
    画像編集ページからスタンプ詳細ページに戻り、「リクエスト」ボタンをクリックする。
    Returns: True if review request was submitted, False otherwise.
    """
    status.update("自動処理中", "審査リクエストを準備中...", 96)

    # 画像アップロード後のページURLからsticker IDを取得
    # 例: /my/{userId}/sticker/{stickerId}/...
    match = re.search(r"/sticker/(\d+)", _get_real_url(page))
    if not match:
        status.update("エラー", "スタンプの登録に問題が発生しました。もう一度お試しください。")
        return False

    sticker_id = match.group(1)
    status.update("デバッグ", f"スタンプID: {sticker_id}")

    # Step 1: 画像編集モードを抜ける（「戻る」ボタンまたはURL遷移）
    # 編集ページ(.../sticker/{id}/image#/image/edit)からスタンプ詳細ページに戻る
    user_path = _extract_user_path(page)
    if user_path:
        detail_url = f"https://creator.line.me{user_path}/sticker/{sticker_id}"
        status.update("デバッグ", f"スタンプ詳細ページに遷移: {detail_url[:80]}")
        page.goto(detail_url, timeout=15000)
        _wait_for_page_ready(page)
        time.sleep(3)
    else:
        # URLから直接ユーザーパスが取れない場合は「戻る」ボタンを試す
        try:
            for back_text in ["戻る", "Back"]:
                btn = page.get_by_text(back_text, exact=True)
                if btn.count() > 0 and btn.first.is_visible(timeout=2000):
                    btn.first.click()
                    time.sleep(3)
                    _wait_for_page_ready(page)
                    status.update("デバッグ", f"「{back_text}」で詳細ページへ戻りました")
                    break
        except Exception as e:
            status.update("デバッグ", f"戻るボタン失敗: {e}")

    # QRモーダル対策
    _force_dismiss_qr_modal(page, status)
    time.sleep(1)

    status.save_screenshot(page, "11_before_request")

    # Step 2: 「リクエスト」ボタンを探してクリック
    # 証拠(過去ログ): ✓ [BUTTON] "リクエスト" href= が存在
    request_clicked = False

    # 戦略1: Playwright get_by_text
    for request_text in ["リクエスト", "Request"]:
        try:
            locator = page.get_by_text(request_text, exact=True)
            count = locator.count()
            status.update("デバッグ", f"「{request_text}」ボタン: {count}個")
            for idx in range(count):
                btn = locator.nth(idx)
                try:
                    if btn.is_visible(timeout=2000):
                        tag = btn.evaluate("e => e.tagName")
                        text = btn.text_content().strip()[:30]
                        status.update("デバッグ", f"「{request_text}」クリック: tag={tag}, text='{text}'")
                        btn.click()
                        request_clicked = True
                        time.sleep(3)
                        break
                except Exception:
                    continue
            if request_clicked:
                break
        except Exception as e:
            status.update("デバッグ", f"get_by_text('{request_text}')失敗: {e}")

    # 戦略2: JavaScript
    if not request_clicked:
        try:
            result = page.evaluate("""
                (() => {
                    const candidates = document.querySelectorAll('a, button, [role="button"], span');
                    for (const el of candidates) {
                        const text = (el.textContent || '').trim();
                        if (text === 'リクエスト' || text === 'Request') {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                el.click();
                                return {tag: el.tagName, text: text};
                            }
                        }
                    }
                    return null;
                })()
            """)
            if result:
                status.update("デバッグ", f"JSクリック成功: {result}")
                request_clicked = True
                time.sleep(3)
        except Exception as e:
            status.update("デバッグ", f"JSクリック失敗: {e}")

    if not request_clicked:
        status.update("エラー", "審査リクエストの送信に失敗しました。もう一度お試しください。")
        status.save_screenshot(page, "11_request_button_not_found")
        return False

    # Step 3: 確認ダイアログ対応
    # LINE Creators Marketでは「リクエスト」クリック後に確認ダイアログが出る可能性がある
    # 「OK」「確認」「はい」「同意」などのボタンをクリック
    time.sleep(2)
    _force_dismiss_qr_modal(page, status)

    # 確認ダイアログの検出と対応（最大3回試行）
    for attempt in range(3):
        confirmed = False

        # 同意チェックボックスがあればチェック
        try:
            checkboxes = page.locator("input[type='checkbox']")
            cb_count = checkboxes.count()
            for ci in range(cb_count):
                cb = checkboxes.nth(ci)
                try:
                    if cb.is_visible(timeout=1000) and not cb.is_checked():
                        cb.check(force=True)
                        status.update("デバッグ", f"チェックボックスをチェック ({ci+1}/{cb_count})")
                        time.sleep(0.5)
                except Exception:
                    continue
        except Exception:
            pass

        # 確認/OK/同意ボタンをクリック
        for confirm_text in ["OK", "リクエスト", "確認", "同意して送信", "同意", "はい", "送信", "Submit"]:
            try:
                btn = page.get_by_text(confirm_text, exact=True)
                if btn.count() > 0:
                    for bi in range(btn.count()):
                        candidate = btn.nth(bi)
                        try:
                            if candidate.is_visible(timeout=1000):
                                candidate.click()
                                confirmed = True
                                status.update("デバッグ", f"「{confirm_text}」クリック (確認ダイアログ)")
                                time.sleep(2)
                                break
                        except Exception:
                            continue
                if confirmed:
                    break
            except Exception:
                continue

        if not confirmed:
            status.update("デバッグ", f"確認ダイアログなし（試行{attempt+1}）")
            break

        time.sleep(2)

    status.save_screenshot(page, "12_after_request")
    status.update("自動処理中", "審査リクエスト送信完了！", 99)
    return True


# ============================================================
# メインフロー
# ============================================================

def upload_to_line(
    output_dir: Path,
    title: str = "Pet Stickers",
    description: str = "Cute pet stickers",
    interactive: bool = True,
    status_file: Optional[Path] = None,
    email: str = "",
    password: str = "",
) -> bool:
    """LINE Creators Marketにスタンプを自動登録する。"""
    output_dir = Path(output_dir)
    stamp_files = sorted(output_dir.glob("[0-9][0-9].png"))

    debug_dir = output_dir / "debug"
    status = UploadStatus(status_file, debug_dir)

    if not stamp_files:
        status.update("エラー", f"{output_dir} にスタンプ画像が見つかりません")
        return False

    # クリエイター名用に元のタイトルを保存（タイムスタンプ付加前）
    creator_name = title

    # タイトルの一意性を確保（LINE Creators Marketは重複タイトルを拒否する）
    timestamp = datetime.now().strftime("%m%d%H%M")
    title = f"{title} {timestamp}"

    status.update("開始", f"スタンプ {len(stamp_files)}枚 / タイトル: {title}", 0)

    # お客様ごとに独立したブラウザプロファイルを作成
    # これにより各お客様が自分のLINEアカウントでログイン・登録できる
    user_data_dir = output_dir.parent / "browser_data"
    user_data_dir.mkdir(exist_ok=True)

    def _cleanup_browser_locks(data_dir: Path):
        """ブラウザのロックファイルと残留プロセスを確実に削除する。"""
        import subprocess, os, signal

        # 1. .browser_data を使用している残留Chromiumプロセスを終了
        try:
            result = subprocess.run(
                ["lsof", "-t", str(data_dir / "SingletonLock")],
                capture_output=True, text=True, timeout=5,
            )
            for pid_str in result.stdout.strip().split():
                try:
                    os.kill(int(pid_str), signal.SIGTERM)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
        except Exception:
            pass

        # 2. pkill でブラウザプロセスも念のため終了
        try:
            subprocess.run(["pkill", "-f", str(data_dir)], capture_output=True, timeout=5)
        except Exception:
            pass

        time.sleep(1)

        # 3. ロックファイルを削除（シンボリックリンク対応）
        import os as _os
        for lock_name in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            lock_full = data_dir / lock_name
            try:
                _os.unlink(str(lock_full))
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _launch_browser(p, data_dir: Path, status: UploadStatus):
        """ブラウザを起動する。失敗時はロック削除してリトライ。"""
        launch_kwargs = {
            "user_data_dir": str(data_dir),
            "headless": True,  # ヘッドレスモード：PCにChromeウィンドウを一切表示しない
            "locale": "ja-JP",
            "viewport": {"width": 1280, "height": 800},
            "args": [
                "--disable-blink-features=AutomationControlled",  # bot検知回避
                "--no-sandbox",
                # オートフィル完全無効化: システムChromeの個人情報（住所等）がフォームに漏洩するのを防止
                "--disable-features=Autofill,AutofillServerCommunication,AutofillCreditCardAuthentication",
                "--disable-sync",  # Googleアカウント同期を無効化
                "--disable-extensions",  # システムChromeの拡張機能によるデータ送信を防止
                "--disable-component-extensions-with-background-pages",
                "--disable-default-apps",
            ],
        }

        for attempt in range(3):
            _cleanup_browser_locks(data_dir)
            # Playwright Chromiumを優先（ローカルデータなし = オートフィル漏洩リスクゼロ）
            try:
                return p.chromium.launch_persistent_context(**launch_kwargs)
            except Exception:
                pass
            # Playwright Chromiumが使えない場合はシステムChromeにフォールバック
            try:
                return p.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
            except Exception as e:
                error_msg = str(e)
                status.update("デバッグ", f"ブラウザ起動試行{attempt+1}/3 失敗: {error_msg[:100]}")
                if attempt < 2:
                    time.sleep(2)
                else:
                    raise RuntimeError(f"ブラウザを起動できません: {error_msg[:200]}")

    _cleanup_browser_locks(user_data_dir)

    status.update("開始", "ブラウザを起動中...", 2)


    with sync_playwright() as p:
        context = _launch_browser(p, user_data_dir, status)
        status.update("デバッグ", "ブラウザ起動成功")

        # persistent context はデフォルトで空ページを1つ作る。
        # 不要な空ページを閉じてから新しいページを作る。
        for existing_page in context.pages:
            if existing_page.url in ("about:blank", "chrome://newtab/"):
                try:
                    existing_page.close()
                except Exception:
                    pass

        page = context.new_page()
        status.update("デバッグ", "新しいページ作成完了")

        # ヘッドレスモードのため、ウィンドウ管理は不要。

        # ============================================================
        # 全ページに対してモーダル自動非表示CSS/JSを注入
        # これにより、どのページでもモーダルは表示されず操作をブロックしない
        # add_init_script はページ遷移のたびに自動で再実行される
        # ============================================================
        # QRコードモーダルのみ非表示にする（重要: 全ダイアログを非表示にすると
        # スタンプ作成フォーム等の必要なUIが隠れてしまうため、対象を限定する）
        context.add_init_script("""
            (function() {
                if (!window.location.hostname.includes('creator.line.me')) return;

                const style = document.createElement('style');
                style.id = '__auto_qr_hide__';
                style.textContent = `
                    /* QRコードモーダルのみ非表示（他のダイアログには影響しない） */
                    .FnOaQrcode,
                    .MdPop24OAQRcode,
                    [role="dialog"].FnOaQrcode,
                    [role="dialog"].MdPop24OAQRcode {
                        display: none !important;
                        visibility: hidden !important;
                        pointer-events: none !important;
                    }
                    /* QRモーダルの背景オーバーレイも非表示 */
                    .FnOaQrcode + .ExBackdrop,
                    .MdPop24OAQRcode + .ExBackdrop {
                        display: none !important;
                        pointer-events: none !important;
                    }
                `;
                if (document.head) {
                    document.head.appendChild(style);
                } else {
                    document.addEventListener('DOMContentLoaded', function() {
                        document.head.appendChild(style);
                    });
                }
            })();
        """)

        try:
            # Step 1: ログイン
            logged_in_page = wait_for_login(page, status, email=email, password=password, title=creator_name)
            if logged_in_page is None:
                return False
            page = logged_in_page

            # ダッシュボードで「新規登録」のhrefを取得
            status.update("自動処理中", "ログイン完了！新規登録ページを探しています...", 22)

            # ダッシュボードから「新規登録」リンクのhrefを取得して直接遷移
            # URLは /my/{userId}/create の形式（ユーザーIDが入る）
            create_url = None
            try:
                create_url = page.evaluate("""
                    (() => {
                        const links = document.querySelectorAll('a[href]');
                        for (const el of links) {
                            const text = (el.textContent || '').trim();
                            if (text.includes('新規登録')) {
                                return el.href;
                            }
                        }
                        return null;
                    })()
                """)
                if create_url:
                    status.update("デバッグ", f"新規登録URL取得: {create_url}")
            except Exception as e:
                status.update("デバッグ", f"新規登録URL取得失敗: {e}")

            status.update("自動処理中", "スタンプを自動登録しています。このページはそのままでお待ちください...", 25)

            # Step 2: スタンプ作成ページへ遷移
            # create_url を使って正しいURLへ直接遷移する。
            # 旧: /my/sticker/new/ → 存在しないURLでダッシュボードにリダイレクト
            # 新: /my/{userId}/create → 正しい作成ページURL
            sticker_page_reached = False
            for nav_attempt in range(3):
                status.update("自動処理中", "スタンプ作成ページに移動中...", 25)
                try:
                    if navigate_to_new_sticker(page, status, create_url=create_url):
                        sticker_page_reached = True
                        break
                except Exception as e:
                    status.update("デバッグ", f"navigate_to_new_sticker失敗 ({nav_attempt+1}/3): {e}")
                if nav_attempt < 2:
                    time.sleep(3)

            if not sticker_page_reached:
                status.update("エラー", "スタンプ作成ページに到達できませんでした。もう一度お試しください。")
                status.save_screenshot(page, "99_final_error")
                return False

            # Step 4: スタンプ情報入力
            fill_sticker_info(page, title, description, status, creator_name=creator_name)

            # Step 5: フォーム保存（新規登録フォームを送信→編集ページへ遷移）
            form_saved = submit_creation_form(page, status)

            if not form_saved:
                # 保存ボタンが見つからない = フォームが正しく表示されていない
                # 失敗したまま続行すると「完了」と表示されるだけで実際には登録されない
                status.update("エラー", "スタンプの保存に失敗しました。フォームが正しく表示されていない可能性があります。もう一度お試しください。")
                status.save_screenshot(page, "99_save_failed")
                return False

            # 保存成功の検証: URLにスタンプIDが含まれているか確認
            save_url = _get_real_url(page)
            sticker_id_match = re.search(r"/sticker/(\d+)", save_url)
            if not sticker_id_match:
                status.update("エラー", "スタンプの作成に失敗しました。保存後のページが正しくありません。もう一度お試しください。")
                status.save_screenshot(page, "99_no_sticker_id")
                status.update("デバッグ", f"保存後URL: {save_url}")
                return False

            sticker_id = sticker_id_match.group(1)
            status.update("デバッグ", f"✅ スタンプ作成成功: ID={sticker_id}")
            status.update("自動処理中", "画像アップロードの準備中...", 62)
            time.sleep(3)
            _wait_for_page_ready(page)

            # Step 6: 画像アップロード
            upload_images(page, output_dir, status)

            # Step 7: 審査リクエスト送信
            status.update("審査準備中", "画像アップロード完了！審査リクエストを自動送信しています...", 95)

            review_submitted = _submit_review_request(page, status)

            # Step 8: 最終検証 - ステータスが「審査待ち」に変わったか確認
            time.sleep(3)
            final_url = _get_real_url(page)
            status.update("デバッグ", f"最終URL: {final_url}")

            # 審査リクエストが送信されなかった場合はエラー
            if not review_submitted:
                status.update("エラー", "スタンプは作成されましたが、審査リクエストの送信に失敗しました。LINE Creators Marketで手動で審査リクエストを送信してください。")
                status.save_screenshot(page, "99_review_failed")
                if interactive:
                    print("\n  審査リクエスト送信に失敗しました。")
                    print("  LINE Creators Marketで手動で確認してください。")
                    print("\n  ブラウザを閉じるにはEnterを押してください...")
                    input()
                else:
                    time.sleep(300)
                return False

            # ページ内のステータステキストを確認
            try:
                page_text = page.inner_text("body")
                if "審査待ち" in page_text or "リクエスト済み" in page_text:
                    status.update("完了", "スタンプが審査に提出されました。LINEの審査には数日かかります。", 100)
                elif "編集中" in page_text:
                    # 審査リクエストが送信されなかった可能性
                    status.update("エラー", "スタンプは作成されましたが、審査リクエストの送信を確認してください。LINE Creators Marketで手動で確認できます。")
                    status.save_screenshot(page, "99_still_editing")
                    if interactive:
                        print("\n  審査リクエストの送信状態を確認してください。")
                        print("\n  ブラウザを閉じるにはEnterを押してください...")
                        input()
                    else:
                        time.sleep(300)
                    return False
                else:
                    # 審査リクエストボタンはクリックできたが、最終確認ができない
                    # スタンプIDが確認済みなので、登録自体は成功している可能性が高い
                    status.update("完了", "スタンプの登録処理が完了しました。LINE Creators Marketで状態を確認してください。", 100)
            except Exception:
                status.update("完了", "スタンプの登録処理が完了しました。LINE Creators Marketで状態を確認してください。", 100)

            if interactive:
                print("\n  審査リクエスト完了！")
                print("  LINE Creators Marketで審査状況を確認できます。")
                print("\n  ブラウザを閉じるにはEnterを押してください...")
                input()
            else:
                time.sleep(300)

            return True

        except KeyboardInterrupt:
            status.update("中断", "ユーザーにより中断されました。")
            return False
        except Exception as e:
            status.update("エラー", "予期しないエラーが発生しました。もう一度お試しください。")
            status.save_screenshot(page, "99_exception")
            return False
        finally:
            context.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="LINE Creators Marketにスタンプを自動登録")
    parser.add_argument("output_dir", nargs="?", default=None, help="スタンプ画像のディレクトリ")
    parser.add_argument("--title", default="Pet Stickers", help="スタンプタイトル")
    parser.add_argument("--desc", default="Cute pet stickers", help="スタンプ説明文")
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
