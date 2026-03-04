"""
ZIP出力モジュール

01.png〜08.png を line_stamp.zip にパッケージングする。
"""

from __future__ import annotations

import zipfile
from pathlib import Path


def create_zip(output_dir, zip_path=None) -> Path:
    """スタンプ画像をZIPファイルにまとめる。

    Args:
        output_dir: 01.png〜08.pngが格納されたディレクトリ
        zip_path: ZIPファイルの保存先（省略時は output_dir/line_stamp.zip）

    Returns:
        ZIPファイルのパス
    """
    output_dir = Path(output_dir)
    if zip_path is None:
        zip_path = output_dir / "line_stamp.zip"
    else:
        zip_path = Path(zip_path)

    stamp_files = sorted(output_dir.glob("[0-9][0-9].png"))

    if len(stamp_files) == 0:
        raise FileNotFoundError(f"{output_dir} にスタンプ画像が見つかりません。")

    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        for stamp_file in stamp_files:
            zf.write(str(stamp_file), stamp_file.name)

    size_kb = zip_path.stat().st_size / 1024
    print(f"\nZIP生成完了: {zip_path} ({size_kb:.1f} KB)")
    print(f"  含まれるファイル: {', '.join(f.name for f in stamp_files)}")
    return zip_path
