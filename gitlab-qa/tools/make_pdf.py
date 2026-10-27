"""把学习计划 HTML 打印成 A4 PDF（与浏览器打印样式一致）。

用法（在 gitlab-qa/ 下）：
    python tools/make_pdf.py
    python tools/make_pdf.py "路径/任意.html" "路径/输出.pdf"
"""
from __future__ import annotations

import sys
from pathlib import Path

# tools/ 下：parents[0]=tools, [1]=gitlab-qa, [2]=仓库根
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

DEFAULT_SRC = ROOT / "GitLab CE测试框架-完整学习计划.html"
DEFAULT_DST = ROOT / "GitLab CE测试框架-完整学习计划.pdf"

from gqa.ui.browser import UISession  # noqa: E402


def html_to_pdf(src: Path, dst: Path, title: str | None = None) -> Path:
    session = UISession("http://localhost", headless=True, trace=False).start()
    print("channel:", session.channel)
    try:
        page = session.new_page("pdf")
        page.goto(src.as_uri(), wait_until="load")
        page.wait_for_timeout(2500)
        page.emulate_media(media="print")
        page.wait_for_timeout(800)
        page.pdf(
            path=str(dst),
            format="A4",
            print_background=True,
            margin={"top": "14mm", "bottom": "14mm",
                    "left": "12mm", "right": "12mm"},
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=(
                '<div style="width:100%;font-size:8pt;color:#888;'
                'padding:0 12mm;display:flex;justify-content:space-between;">'
                f"<span>{title or src.stem}</span>"
                '<span><span class="pageNumber"></span> / '
                '<span class="totalPages"></span></span></div>'
            ),
        )
    finally:
        session.teardown()
    return dst


def main() -> int:
    src = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_SRC
    dst = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else DEFAULT_DST
    if not src.exists():
        print(f"[!!] 源文件不存在：{src}")
        return 2
    out = html_to_pdf(src, dst)
    print("written:", out, out.stat().st_size, "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
