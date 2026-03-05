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

    def update(self, step: str, message: str, progress: int = 0):
        self.logs.append(f"[{step}] {message}")
        print(f"  [{step}] {message}")
        if self.status_file:
            data = {
                "step": step,
                "message": message,
                "progress": progress,
                "logs": self.logs[-100:],
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

def _minimize_browser(page: Page, status: UploadStatus):
    """Chromiumブラウザウィンドウを最小化する。
    画像アップロード完了後、ユーザーにはWebアプリの待機画面だけ見せるために使用。
    CDP (Chrome DevTools Protocol) でウィンドウを最小化する。
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
        status.update("デバッグ", f"ブラウザ最小化失敗（動作に影響なし）: {e}")


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
    url = page.url
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


def _try_navigate_to_dashboard(page: Page, status: UploadStatus) -> bool:
    """/signup/line_auth に遷移し、ログイン済みならダッシュボードにリダイレクトされるか確認。
    ダッシュボードに到達できたらTrue。"""
    try:
        page.goto(LOGIN_URL, timeout=15000)
        _wait_for_page_ready(page)
        new_url = page.url
        status.update("ログイン", f"リダイレクト先: {new_url[:100]}", 15)
        return "/my/" in new_url
    except Exception as e:
        status.update("デバッグ", f"リダイレクト試行失敗: {e}")
        return False


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
            url = p.url
            # ダッシュボードに直接いるページ（_is_on_dashboardで厳密判定）
            if _is_on_dashboard(p):
                status.update("ログイン", f"ダッシュボード検出: {url[:100]}", 20)
                return p
            # /my/ 配下だがダッシュボードではないページ（スタンプ編集ページ等）
            # → creator.line.me にいるページとして記録（後でリダイレクト試行）
            if "/my/" in url:
                status.update("ログイン", f"/my/配下検出（ダッシュボード以外）: {url[:100]}", 18)
                creator_page = p
            # creator.line.me にいるページ（ログインページ以外）を記録
            elif _is_on_creator_site(url) and "/signup/" not in url:
                creator_page = p
            # OAuth コールバック中のページ
            if "/signup/line_callback" in url:
                status.update("ログイン", f"OAuthコールバック検出", 15)
                try:
                    p.wait_for_url(lambda u: "/my/" in u or (_is_on_creator_site(u) and "/signup/" not in u), timeout=15000)
                    if _is_on_dashboard(p):
                        return p
                    creator_page = p
                except PwTimeout:
                    pass
        except Exception:
            continue

    # creator.line.me にいるがダッシュボードではないページがある場合、リダイレクト試行
    if creator_page:
        status.update("ログイン", "creator.line.me検出。ダッシュボードへリダイレクト試行...", 15)
        if _try_navigate_to_dashboard(creator_page, status):
            return creator_page
        # リダイレクト失敗 → access.line.me に戻された場合はまだ未ログイン
        if _is_on_login_page(creator_page.url):
            return None

    return None


def wait_for_login(page: Page, status: UploadStatus, timeout: int = 300) -> Optional[Page]:
    """ユーザーがログインするまで待機。ログイン後のページを返す。失敗時はNone。"""
    context = page.context

    # 新しいタブ/ポップアップを検知
    context.on("page", lambda new_page: status.update(
        "デバッグ", f"新しいタブ検出: {(new_page.url or '(loading)')[:80]}"
    ))

    # LINE OAuth ログインページにアクセス
    status.update("ログイン", "LINE Creators Marketにアクセス中...", 5)
    page.goto(LOGIN_URL, timeout=30000)
    _wait_for_page_ready(page)
    page.bring_to_front()

    status.save_screenshot(page, "01_login_page")

    # 既にダッシュボードにリダイレクトされた（セッション有効）
    if _is_on_dashboard(page):
        status.update("ログイン", "ログイン済み（セッション有効）", 20)
        return page

    # creator.line.me にいる場合（トップページ等）→ リダイレクトでダッシュボードに行けるか試す
    if _is_on_creator_site(page.url) and "/signup/" not in page.url:
        if _try_navigate_to_dashboard(page, status):
            status.update("ログイン", "ログイン済み（ダッシュボードにリダイレクト成功）", 20)
            return page

    # ログインページにいる → ユーザーにログインを促す
    # ブラウザウィンドウにバナーを表示して目立たせる
    try:
        page.evaluate("""
            (() => {
                const banner = document.createElement('div');
                banner.id = 'pw-login-banner';
                banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;' +
                    'background:#e74c3c;color:white;padding:16px;text-align:center;' +
                    'font-size:18px;font-weight:bold;box-shadow:0 2px 10px rgba(0,0,0,0.3);';
                banner.textContent = '⚠️ このブラウザでLINEにログインしてください ⚠️';
                document.body.prepend(banner);
            })()
        """)
    except Exception:
        pass

    page.bring_to_front()
    status.update("ログイン", f"⚠️ 新しく開いたブラウザウィンドウでログインしてください（{timeout}秒以内）", 10)

    last_urls: dict[int, str] = {}
    elapsed = 0
    interval = 3
    while elapsed < timeout:
        time.sleep(interval)
        elapsed += interval

        # 全ページのURL変化を監視
        for p in context.pages:
            try:
                pid = id(p)
                current_url = p.url
                prev_url = last_urls.get(pid, "")
                if current_url != prev_url:
                    status.update("ログイン", f"URL変化: {current_url[:100]}", 10)
                    last_urls[pid] = current_url
            except Exception:
                continue

        # ダッシュボードに到達したページがあるか確認
        dashboard_page = _find_dashboard_page(context, status)
        if dashboard_page:
            _wait_for_page_ready(dashboard_page)
            status.update("ログイン", "ログイン完了！", 20)
            status.save_screenshot(dashboard_page, "02_login_done")
            return dashboard_page

        # 30秒ごとにリマインド
        if elapsed % 30 == 0 and elapsed < timeout:
            remaining = timeout - elapsed
            status.update("ログイン", f"⚠️ ブラウザでログインしてください（残り{remaining}秒）", 10)
            # ブラウザを前面に持ってくる
            try:
                page.bring_to_front()
            except Exception:
                pass

    # タイムアウト
    status.update("エラー", "タイムアウト: ログインが完了しませんでした。")
    status.update("デバッグ", f"最終ページ数: {len(context.pages)}")
    for i, p in enumerate(context.pages):
        try:
            status.update("デバッグ", f"  Page[{i}] URL: {p.url[:100]}")
        except Exception:
            pass
    status.save_screenshot(page, "02_login_timeout")
    return None


# ============================================================
# ダッシュボード操作
# ============================================================

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
    """
    match = re.search(r"(/my/[^/]+)", page.url)
    if match:
        return match.group(1)
    return None


def _ensure_on_dashboard(page: Page, status: UploadStatus) -> bool:
    """ダッシュボードにいることを確認し、いなければ戻る。"""
    if _is_on_dashboard(page):
        return True
    status.update("ページ遷移", "ダッシュボードに移動中...", 25)
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
            status.update("ページ遷移", f"「新規登録」href={href}", 28)
            page.goto(href, timeout=15000)
            _wait_for_page_ready(page)
            if page.url != original_url:
                status.update("ページ遷移", f"page.goto()遷移成功: {page.url}", 30)
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
            status.update("ページ遷移", f"Playwrightクリック遷移成功: {page.url}", 30)
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
            status.update("ページ遷移", f"JSクリック遷移成功: {page.url}", 30)
            return True
    except Exception as e:
        status.update("デバッグ", f"JSクリック失敗: {e}")

    status.update("デバッグ", f"新規登録クリック全戦略失敗。URL変化なし: {page.url}")
    return False


def navigate_to_new_sticker(page: Page, status: UploadStatus) -> bool:
    """スタンプ新規作成ページに遷移する。"""
    status.update("ページ遷移", f"新規登録ページに移動中... 現在URL: {page.url[:100]}", 25)

    # ダッシュボードにいない場合は移動
    if not _ensure_on_dashboard(page, status):
        status.update("エラー", f"ダッシュボードに到達できませんでした。URL: {page.url[:100]}")
        status.save_screenshot(page, "03_dashboard_fail")
        return False

    # モーダルを閉じる（最大5回試行、閉じたことを確認するまで繰り返す）
    _wait_for_page_ready(page)
    time.sleep(2)  # モーダル表示完了を待つ

    for attempt in range(5):
        # モーダルが存在するか確認（可視の「閉じる」ボタンのみカウント）
        # 証拠(session 8a2f7dad-2回目): 不可視の「閉じる」(rect:0,0,0,0)で無限ループ
        modal_exists = False
        try:
            close_locator = page.get_by_text("閉じる", exact=True)
            close_count = close_locator.count()
            # 可視のもののみカウント
            visible_count = 0
            for ci in range(close_count):
                try:
                    if close_locator.nth(ci).is_visible(timeout=500):
                        visible_count += 1
                except Exception:
                    pass
            modal_exists = visible_count > 0
            if close_count > 0 and visible_count == 0:
                status.update("デバッグ", f"「閉じる」{close_count}個あるが全て不可視 → スキップ")
        except Exception:
            pass

        if not modal_exists:
            status.update("デバッグ", f"モーダルなし（試行{attempt + 1}回目）")
            break

        status.update("デバッグ", f"モーダル検出、閉じ試行 {attempt + 1}/5")
        _dismiss_modals(page, status)
        time.sleep(2)  # 閉じアニメーション完了を待つ

    status.save_screenshot(page, "03_dashboard")
    status.dump_page_info(page, "ダッシュボード")

    # ---- 「新規登録」ボタンをクリック ----
    status.update("ページ遷移", "「新規登録」ボタンを探しています...", 27)

    if not _click_new_registration(page, status):
        status.save_screenshot(page, "03_new_reg_failed")
        status.dump_page_info(page, "新規登録失敗")
        status.update("エラー", "「新規登録」ボタンが見つかりません。デバッグスクリーンショットを確認してください。")
        return False

    # 404ページに飛んでしまった場合はダッシュボードに戻ってリトライ
    page_text = (page.text_content("body") or "")[:500]
    if "存在しません" in page_text or "404" in page.title():
        status.update("ページ遷移", "404ページに到達。ダッシュボードに戻ります...", 27)
        if _ensure_on_dashboard(page, status):
            _dismiss_modals(page, status)
            time.sleep(1)
        else:
            return False

    status.save_screenshot(page, "04_after_new_reg_click")
    status.update("ページ遷移", f"新規登録クリック後: {page.url}", 30)
    status.dump_page_info(page, "新規登録後")

    # ---- スタンプタイプ選択 ----
    # /create ページでは スタンプ/絵文字/着せかえ の3つのボタンが表示される
    # 証拠: debug_04_after_new_reg_click.png で確認済み
    # 重要: サイドバーの「LINE PRスタンプ」(href含むpromotion/stickers)を誤クリックしないよう除外
    time.sleep(2)
    type_select_url = page.url
    try:
        result = page.evaluate("""
            (() => {
                // まずボタン要素（<button>、<a>）から「スタンプ」テキストを探す
                // サイドバーのリンクを除外するため：
                // - hrefにpromotion/guideline/review/statsを含むものはスキップ
                // - テキストが「スタンプ」のみ（「LINE PRスタンプ」等の長いテキストは除外）
                const candidates = document.querySelectorAll('a[href], button');
                const excluded = ['promotion', 'guideline', 'review', 'stats', 'studio'];
                for (const el of candidates) {
                    const text = (el.textContent || '').trim();
                    const href = el.href || el.getAttribute('href') || '';
                    // テキストが正確に「スタンプ」のみ（サイドバーの長い名前を除外）
                    if (text === 'スタンプ') {
                        // 除外パターンチェック
                        const isExcluded = excluded.some(ex => href.includes(ex));
                        if (!isExcluded) {
                            el.click();
                            return {text: text, href: href, tag: el.tagName};
                        }
                    }
                }
                return null;
            })()
        """)
        if result:
            status.update("ページ遷移", f"「スタンプ」を選択: {result}", 32)
            time.sleep(3)
            _wait_for_page_ready(page)
            status.save_screenshot(page, "05_after_sticker_select")
        else:
            status.update("デバッグ", "「スタンプ」ボタン未検出（既にスタンプ作成ページの可能性）")
    except Exception as e:
        status.update("デバッグ", f"スタンプタイプ選択エラー: {e}")

    # スタンプタイプ選択後にURL変化チェック
    if page.url != type_select_url:
        status.update("ページ遷移", f"タイプ選択後URL: {page.url}", 33)
    # 誤ナビゲーション検知（promotionページに飛んでいないか）
    if "promotion" in page.url:
        status.update("デバッグ", "⚠️ promotionページに誤遷移！/createに戻ります")
        page.go_back()
        _wait_for_page_ready(page)
        time.sleep(2)

    current_url = page.url
    status.update("ページ遷移", f"最終URL: {current_url}", 35)

    form_count = page.locator("input[type='text'], textarea, input[type='file'], select").count()
    if form_count > 0:
        status.update("ページ遷移", f"スタンプ作成ページに到着（フォーム要素: {form_count}個）", 35)
        status.save_screenshot(page, "05_form_found")
        return True

    status.save_screenshot(page, "05_no_form")
    status.dump_page_info(page, "フォーム未検出")
    status.update("ページ遷移", "フォームが見つかりません。デバッグ情報を記録しました。", 30)
    return False


# ============================================================
# フォーム入力・画像アップロード
# ============================================================

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
                    inp.fill("fus")
                    status.update("情報入力", "コピーライト入力: fus", 52)
                else:
                    status.update("情報入力", f"コピーライト既入力: {current_val}", 52)
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
            status.update("情報入力", f"AI使用: {ai_checked}", 53)
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
    status.update("フォーム送信", "フォームを保存中...", 55)

    url_before = page.url

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
            status.update("フォーム送信", f"「保存」ボタンクリック: {result}", 57)
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
                status.update("フォーム送信", "Playwright get_by_text('保存') クリック", 57)
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
                status.update("フォーム送信", "submitボタンをクリック", 57)
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
                status.update("フォーム送信", "form.submit()を実行（405の可能性あり）", 57)
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
            status.update("フォーム送信", "確認ダイアログ「OK」クリック", 58)
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
                    status.update("フォーム送信", f"確認ダイアログ「OK」クリック (role={role})", 58)
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
                status.update("フォーム送信", f"確認ダイアログクリック(JS): {js_ok}", 58)
                ok_clicked = True
        except Exception as e:
            status.update("デバッグ", f"OK検索失敗(JS): {e}")

    if ok_clicked:
        status.update("フォーム送信", "確認ダイアログを承認。ページ遷移を待機中...", 58)

    # ページ遷移を待つ
    time.sleep(5)
    _wait_for_page_ready(page)

    new_url = page.url

    # 405 Method Not Allowed 検知 → GETで再読み込み
    # 証拠(session d1f7b5d4): form.submit() → POST → 405
    # しかしURLにスタンプIDが含まれている = スタンプ自体は作成済み
    try:
        page_title = page.title()
        if "405" in page_title or "Method Not Allowed" in page_title:
            status.update("フォーム送信", f"405検知。GETで再読み込み: {new_url}", 58)
            page.goto(new_url, timeout=15000)
            _wait_for_page_ready(page)
            new_url = page.url
            status.update("フォーム送信", f"再読み込み後: {new_url}", 59)
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
        status.update("フォーム送信", f"ページ遷移成功: {new_url}", 60)
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

    # QRコードモーダルを強制削除（画像アップロードを妨害するため）
    # 証拠(session b45b0b87): FnOaQrcodeモーダルが全クリックをブロック
    _force_dismiss_qr_modal(page, status)
    _dismiss_modals(page, status)
    time.sleep(1)

    # 「スタンプ画像」タブに切り替え
    # 証拠(session b45b0b87): 保存後は表示情報タブのままでfile input数=0
    try:
        # SPAナビゲーション: URLフラグメントで#/imageに遷移
        current_url = page.url
        if "#/image" not in current_url:
            # まずタブクリックを試行
            tab_clicked = False
            for tab_text in ["スタンプ画像", "Sticker Images"]:
                try:
                    tab = page.get_by_text(tab_text, exact=True)
                    if tab.count() > 0 and tab.first.is_visible(timeout=2000):
                        tab.first.click()
                        tab_clicked = True
                        status.update("画像アップ", f"「{tab_text}」タブをクリック")
                        time.sleep(2)
                        break
                except Exception:
                    continue
            if not tab_clicked:
                # URLフラグメントで直接遷移
                base_url = current_url.split("#")[0]
                page.goto(f"{base_url}#/image", timeout=15000)
                status.update("画像アップ", "#/image に直接遷移")
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
                                status.update("画像アップ", f"「{edit_text}」ボタンをクリック (tag={tag})")
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
                current_url = page.url
                base_url = current_url.split("#")[0]
                page.goto(f"{base_url}#/image/edit", timeout=15000)
                status.update("画像アップ", "#/image/edit に直接遷移")
                time.sleep(3)
                _force_dismiss_qr_modal(page, status)
            except Exception as e:
                status.update("デバッグ", f"編集モード遷移失敗: {e}")
    except Exception as e:
        status.update("デバッグ", f"編集ボタンクリック失敗: {e}")

    # デバッグ: 現在のページ構造を記録
    status.save_screenshot(page, "09_upload_page")
    status.dump_page_info(page, "画像アップロードページ")

    # main.png / tab.png の存在確認
    main_file = output_dir / "main.png"
    tab_file = output_dir / "tab.png"
    has_main = main_file.exists()
    has_tab = tab_file.exists()
    status.update("画像アップ", f"main.png: {'あり' if has_main else 'なし'}, tab.png: {'あり' if has_tab else 'なし'}")

    # 方法1: 複数のfile inputがある場合（一度に全てセット可能）
    file_inputs = page.locator("input[type='file']")
    fi_count = file_inputs.count()
    status.update("画像アップ", f"file input数: {fi_count}", 66)

    if fi_count >= total:
        # file inputをaccept属性とコンテキストで分類
        # 証拠(session ecc8cc27): nth(0)がZIP用inputの可能性あり
        # → accept属性やラベルで判別する
        fi_map = _classify_file_inputs(page, fi_count, status)

        # main.pngをアップロード
        if has_main and fi_map.get("main") is not None:
            try:
                file_inputs.nth(fi_map["main"]).set_input_files(str(main_file))
                status.update("画像アップ", f"main.png → input[{fi_map['main']}] OK", 66)
                time.sleep(1)
            except Exception as e:
                status.update("画像アップ", f"main.png 失敗: {e}")

        # tab.pngをアップロード
        if has_tab and fi_map.get("tab") is not None:
            try:
                file_inputs.nth(fi_map["tab"]).set_input_files(str(tab_file))
                status.update("画像アップ", f"tab.png → input[{fi_map['tab']}] OK", 67)
                time.sleep(1)
            except Exception as e:
                status.update("画像アップ", f"tab.png 失敗: {e}")

        # スタンプ画像のfile inputインデックスリスト
        sticker_indices = fi_map.get("stickers", [])

        # スタンプ画像を各inputに1枚ずつ
        for i, stamp_file in enumerate(stamp_files):
            try:
                if i < len(sticker_indices):
                    idx = sticker_indices[i]
                else:
                    # フォールバック: 分類外のinputを順番に使う
                    idx = i + (fi_count - total)
                file_inputs.nth(idx).set_input_files(str(stamp_file))
                status.update("画像アップ", f"[{i+1}/{total}] {stamp_file.name} → input[{idx}] OK", 65 + int((i+1)/total*25))
                time.sleep(1)
            except Exception as e:
                status.update("画像アップ", f"[{i+1}/{total}] {stamp_file.name} 失敗: {e}")
        status.save_screenshot(page, "10_after_upload")
        status.update("画像アップ", "画像アップロード完了！", 95)
        return

    # 方法2: file inputが1つ → multiple属性でまとめてアップロード
    if fi_count == 1:
        try:
            fi = file_inputs.first
            file_paths = [str(f) for f in stamp_files]
            fi.set_input_files(file_paths)
            status.update("画像アップ", f"一括アップロード: {total}枚", 85)
            time.sleep(5)
            status.save_screenshot(page, "10_after_upload")
            status.update("画像アップ", "画像アップロード完了！", 95)
            return
        except Exception as e:
            status.update("デバッグ", f"一括アップロード失敗: {e}")

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
                        status.update("画像アップ", f"[{i+1}/{total}] スロットクリック: {selector[:40]}")
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

                status.update("画像アップ", f"[{i+1}/{total}] {stamp_file.name} OK", progress)
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
                            status.update("画像アップ", f"[{i+1}/{total}] {stamp_file.name} OK (filechooser)", progress)
                            uploaded_count += 1
                            time.sleep(3)
                        else:
                            status.update("画像アップ", f"[{i+1}/{total}] アップロード手段なし", progress)
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
                            status.update("画像アップ", f"[{i+1}/{total}] filechooser待機タイムアウト", progress)
                except Exception as e:
                    status.update("画像アップ", f"[{i+1}/{total}] {stamp_file.name} 失敗: {e}", progress)

        except Exception as e:
            status.update("画像アップ", f"[{i+1}/{total}] {stamp_file.name} 失敗: {e}", progress)

    status.save_screenshot(page, "10_after_upload")
    status.update("画像アップ", f"画像アップロード完了！({uploaded_count}/{total}枚成功)", 95)


def _submit_review_request(page: Page, status: UploadStatus):
    """画像アップロード後、審査リクエストボタンを押して審査提出する。
    画像編集ページからスタンプ詳細ページに戻り、「リクエスト」ボタンをクリックする。
    """
    status.update("審査リクエスト", "審査リクエスト送信準備中...", 96)

    # 画像アップロード後のページURLからsticker IDを取得
    # 例: /my/{userId}/sticker/{stickerId}/...
    match = re.search(r"/sticker/(\d+)", page.url)
    if not match:
        status.update("エラー", f"スタンプIDが取得できません。URL: {page.url[:100]}")
        return

    sticker_id = match.group(1)
    status.update("審査リクエスト", f"スタンプID: {sticker_id}")

    # Step 1: 画像編集モードを抜ける（「戻る」ボタンまたはURL遷移）
    # 編集ページ(.../sticker/{id}/image#/image/edit)からスタンプ詳細ページに戻る
    user_path = _extract_user_path(page)
    if user_path:
        detail_url = f"https://creator.line.me{user_path}/sticker/{sticker_id}"
        status.update("審査リクエスト", f"スタンプ詳細ページに遷移: {detail_url[:80]}")
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
                    status.update("審査リクエスト", f"「{back_text}」で詳細ページへ戻りました")
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
                        status.update("審査リクエスト", f"「{request_text}」クリック: tag={tag}, text='{text}'")
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
                status.update("審査リクエスト", f"JSクリック成功: {result}")
                request_clicked = True
                time.sleep(3)
        except Exception as e:
            status.update("デバッグ", f"JSクリック失敗: {e}")

    if not request_clicked:
        status.update("エラー", "「リクエスト」ボタンが見つかりません。手動で押してください。")
        status.save_screenshot(page, "11_request_button_not_found")
        return

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
                        status.update("審査リクエスト", f"チェックボックスをチェック ({ci+1}/{cb_count})")
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
                                status.update("審査リクエスト", f"「{confirm_text}」クリック (確認ダイアログ)")
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
    status.update("審査リクエスト", "審査リクエスト送信完了！", 99)


# ============================================================
# メインフロー
# ============================================================

def upload_to_line(
    output_dir: Path,
    title: str = "Pet Stickers",
    description: str = "Cute pet stickers",
    interactive: bool = True,
    status_file: Optional[Path] = None,
) -> bool:
    """LINE Creators Marketにスタンプを自動登録する。"""
    output_dir = Path(output_dir)
    stamp_files = sorted(output_dir.glob("[0-9][0-9].png"))

    debug_dir = output_dir / "debug"
    status = UploadStatus(status_file, debug_dir)

    if not stamp_files:
        status.update("エラー", f"{output_dir} にスタンプ画像が見つかりません")
        return False

    # タイトルの一意性を確保（LINE Creators Marketは重複タイトルを拒否する）
    timestamp = datetime.now().strftime("%m%d%H%M")
    title = f"{title} {timestamp}"

    status.update("開始", f"スタンプ {len(stamp_files)}枚 / タイトル: {title}", 0)

    # 永続的なブラウザプロファイル（ログインセッション保存）
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

        # persistent context はデフォルトで空ページを1つ作る。
        # 不要な空ページを閉じてから新しいページを作る。
        for existing_page in context.pages:
            if existing_page.url in ("about:blank", "chrome://newtab/"):
                try:
                    existing_page.close()
                except Exception:
                    pass

        page = context.new_page()

        try:
            # Step 1: ログイン
            logged_in_page = wait_for_login(page, status)
            if logged_in_page is None:
                return False
            page = logged_in_page

            # ログイン完了 → ブラウザを最小化してバックグラウンド処理に移行
            status.update("自動処理中", "ログイン完了！スタンプを自動登録しています。このページはそのままでお待ちください...", 22)
            _minimize_browser(page, status)

            # Step 2: ダッシュボード表示後の初期モーダル閉じ
            time.sleep(3)  # ダッシュボード + モーダル表示完了を待つ
            status.save_screenshot(page, "02b_before_modal_dismiss")
            _dismiss_modals(page, status)
            time.sleep(2)
            status.save_screenshot(page, "02c_after_modal_dismiss")

            # Step 3: スタンプ作成ページへ遷移（最大2回リトライ）
            sticker_page_reached = False
            for nav_attempt in range(2):
                if navigate_to_new_sticker(page, status):
                    sticker_page_reached = True
                    break
                # リトライ前にダッシュボードに戻ってモーダルを確実に閉じる
                status.update("ページ遷移", f"リトライ {nav_attempt + 1}/2: ダッシュボードに戻ります...", 25)
                if _ensure_on_dashboard(page, status):
                    time.sleep(2)
                    _dismiss_modals(page, status)
                    time.sleep(2)

            if not sticker_page_reached:
                status.update("警告", "自動遷移に失敗。手動で操作してください。")
                if interactive:
                    print("  手動でスタンプ作成ページに移動してください。")
                    print("  移動したらEnterを押してください...")
                    input()
                else:
                    status.update("警告", "30秒待機中... 手動でスタンプ作成ページに移動してください。")
                    time.sleep(30)
                    form_count = page.locator("input[type='text'], textarea, input[type='file']").count()
                    if form_count == 0:
                        status.update("エラー", "スタンプ作成ページに到達できませんでした。")
                        status.save_screenshot(page, "99_final_error")
                        status.dump_page_info(page, "最終状態")
                        return False

            # Step 4: スタンプ情報入力
            fill_sticker_info(page, title, description, status)

            # Step 5: フォーム保存（新規登録フォームを送信→編集ページへ遷移）
            form_saved = submit_creation_form(page, status)
            if form_saved:
                status.update("フォーム送信", "編集ページに遷移しました", 62)
                time.sleep(3)
                _wait_for_page_ready(page)
            else:
                status.update("警告", "フォーム保存に失敗。現在のページで画像アップロードを試みます。", 60)

            # Step 6: 画像アップロード
            upload_images(page, output_dir, status)

            # Step 7: 審査リクエスト送信（バックグラウンドで進行）
            # 画像アップロード完了をユーザーに通知し、審査リクエストは裏で進める
            status.update("審査準備中", "画像アップロード完了！審査リクエストを自動送信しています...", 95)

            _submit_review_request(page, status)

            status.update("完了", "審査リクエスト完了！スタンプが審査に提出されました。LINEの審査には数日かかります。", 100)

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
            status.update("エラー", f"予期しないエラー: {e}")
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
