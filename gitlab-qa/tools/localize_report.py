"""把 Allure 报告界面切成中文。

Allure 2.x 前端用 i18next，自带 zh 语言包，但在 index.html 里默认写死了
``<html lang="en">``——**默认语种就是从这里取的**：

    defaults: language = document.documentElement.lang || "en"

用户在界面右下角手动切过语言后，选择会写进 localStorage 的
``ALLURE_REPORT_SETTINGS.language``，之后以它为准。

所以要让报告“打开就是中文”，做两件事就够了：

1. 把 ``<html lang>`` 改成目标语种 —— 决定默认显示语言；
2. 只在用户**还没手动选过**时，往 localStorage 预置一条 —— 保证首屏是中文，
   同时不剥夺之后在界面里切回英文的能力。要是无脑覆盖，用户每次刷新都会被改回去。

最后一点很关键：``allure generate`` 每次都会重写 index.html，
所以这一步必须放在生成**之后**，不能改一次就完事。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_LOCALE = "zh"

INJECT_BEGIN = "<!-- allure-locale-inject -->"
INJECT_END = "<!-- /allure-locale-inject -->"

# 匹配之前注入过的块，保证重复执行不会越堆越多
_INJECTED = re.compile(
    re.escape(INJECT_BEGIN) + r".*?" + re.escape(INJECT_END) + r"\s*",
    re.DOTALL,
)

_SCRIPT = """{begin}
<script data-allure-locale="{locale}">
(function () {{
  var LOCALE = "{locale}";
  var KEY = "ALLURE_REPORT_SETTINGS";
  try {{
    var raw = window.localStorage.getItem(KEY);
    var settings = raw ? JSON.parse(raw) : null;
    if (!settings || typeof settings !== "object") {{ settings = {{}}; }}
    // 只在用户没手动选过语言时预置，尊重此后的个人选择
    if (!settings.language) {{
      settings.language = LOCALE;
      window.localStorage.setItem(KEY, JSON.stringify(settings));
    }}
  }} catch (e) {{}}
}})();
</script>
{end}"""


def build_injection(locale: str) -> str:
    return _SCRIPT.format(begin=INJECT_BEGIN, end=INJECT_END, locale=locale)


def localize(report_dir: Path, locale: str = DEFAULT_LOCALE) -> Path:
    """把报告目录里的 index.html 切成 locale 语种，返回改动的文件路径。"""
    index = report_dir / "index.html"
    if not index.exists():
        raise FileNotFoundError(f"找不到 {index}，先跑 allure generate")

    html = index.read_text(encoding="utf-8")

    # 1. 默认语种：<html dir="ltr" lang="en"> -> lang="zh"
    #    写成整段匹配而不是只替换 lang="en"，避免以后 lang 变成别的串时静默失效
    new_html, n_lang = re.subn(
        r"(<html\b[^>]*?\blang\s*=\s*)(['\"])([^'\"]*)\2",
        lambda m: f"{m.group(1)}{m.group(2)}{locale}{m.group(2)}",
        html,
        count=1,
    )
    if n_lang == 0:
        new_html, n_lang = re.subn(r"<html\b", f'<html lang="{locale}"', new_html, count=1)

    # 2. 清掉旧的注入块再写入新的，保证幂等
    new_html = _INJECTED.sub("", new_html)

    # 插到 </head> 之前：一定先于 app.js 执行（app.js 在 body 末尾加载），
    # 也不会像 body 里那样把脚本文本混进可见 DOM
    anchor = "</head>"
    if anchor in new_html:
        new_html = new_html.replace(anchor, build_injection(locale) + "\n" + anchor, 1)
    else:
        # 没有 head 的兜底：插在 app.js 之前，语义等价
        js_tag = '<script src="app.js"></script>'
        if js_tag in new_html:
            new_html = new_html.replace(js_tag, build_injection(locale) + "\n    " + js_tag, 1)

    index.write_text(new_html, encoding="utf-8")
    return index


def main() -> int:
    here = Path(__file__).resolve().parents[1]
    p = argparse.ArgumentParser(description="把 Allure 报告界面切成指定语言（默认中文）")
    p.add_argument("report_dir", nargs="?", default=str(here / "allure-report"),
                   help="Allure 报告目录，默认 allure-report")
    p.add_argument("--locale", default=DEFAULT_LOCALE,
                   help=f"目标语言，默认 {DEFAULT_LOCALE}（可用 en 还原）")
    args = p.parse_args()

    index = localize(Path(args.report_dir), locale=args.locale)
    label = "中文" if args.locale == "zh" else args.locale
    print(f"[i18n] 报告界面已切换为 {label}：{index}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
