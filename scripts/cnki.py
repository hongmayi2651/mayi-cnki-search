#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知网(CNKI)检索助手 —— 借用户已登录的 Chrome 与机构 WebVPN 检索 / 摘录题录 / 下载文献。

为什么走 Python 而不是直接敲 bsk：
  PowerShell 向原生 exe 传参会 ①剥离双引号 ②把 URL 里的 % 误判成 cmd.exe 的 %VAR% 语法；
  用 subprocess 传 list 参数可完全绕开这两类问题，同时统一 UTF-8 编码。

用法：
  python cnki.py config                    # 查看当前门户入口配置
  python cnki.py config --set "<门户入口>"  # 写入本地配置（仓库不含机构域名）
  python cnki.py doctor
  python cnki.py search "片上网络 容错路由" [--field SU] [--pages 2] [--out refs.json]
  python cnki.py detail "<v token 或知网节 URL>" [--out d.json]
  python cnki.py download "<v token>" --out "D:/paper/xxx.pdf"
  python cnki.py prefix            # 打印 / 刷新当前可用的 WebVPN 检索页前缀

检索字段 korder 取值：SU 主题 | TI 篇名 | KY 关键词 | AB 摘要 | AU 作者 | FT 全文
"""

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

# ---------- 基础 ----------

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TICKET_RE = re.compile(r"(https://[^/]+/https/webvpn[0-9a-fA-F]{32})")
CACHE = Path.home() / ".workbuddy" / "cache" / "cnki.json"
PORTAL_FILE = Path.home() / ".workbuddy" / "cache" / "cnki-portal.txt"


def get_portal():
    """机构 WebVPN 门户入口地址。

    本仓库**不含任何机构标识**，按此顺序解析：
      1) 环境变量 CNKI_PORTAL
      2) 本地配置 PORTAL_FILE（用 `config --set` 写入）
      3) 都没有 → 返回 None，由调用方给出指引。

    注意：门户入口只在「用户标签页里没有现成知网检索页」时才需要。
    只要浏览器里已打开过一次知网检索结果页，票据可直接从标签页取，无需本项配置。
    """
    v = (os.environ.get("CNKI_PORTAL") or "").strip()
    if v:
        return v
    try:
        v = PORTAL_FILE.read_text("utf-8").strip()
        if v:
            return v
    except Exception:
        pass
    return None


def set_portal(url):
    url = (url or "").strip()
    if not url.startswith("http"):
        sys.exit("[x] 门户入口地址应以 http(s):// 开头，收到：%r" % url)
    PORTAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    PORTAL_FILE.write_text(url, "utf-8")
    print("[ok] 已写入本地配置：%s\n     %s" % (PORTAL_FILE, url))

# 结果列表：td 的 class 依次为 seq / name / author / source / date / data / quote / download / operat
EXTRACT_JS = r"""JSON.stringify(Array.from(document.querySelectorAll('.result-table-list tbody tr')).map(function(tr){
  function g(c){var e=tr.querySelector('td.'+c);return e?e.innerText.replace(/\s+/g,' ').trim():''}
  var a=tr.querySelector('td.name a'), h=a?a.href:'', m=h.match(/[?&]v=([^&]+)/);
  return {seq:g('seq'),title:g('name'),author:g('author'),source:g('source'),date:g('date'),
          type:g('data'),cited:g('quote'),downloads:g('download'),vtoken:m?m[1]:'',href:h};
}))"""

# 知网节：.top-tip 直接给出「刊名 . 年 ,卷 (期) : 起止页」，是著录的关键字段
DETAIL_JS = r"""JSON.stringify({
  title:(document.title||'').replace(/\s*-\s*中国知网\s*$/,''),
  citation:((document.querySelector('.top-tip')||{}).innerText||'').replace(/\s+/g,' ').trim(),
  authors:((document.querySelector('.author')||{}).innerText||'').replace(/\s+/g,' ').trim(),
  body:(document.body.innerText||'').replace(/\s+/g,' ').slice(0,3000)
})"""


def find_bsk():
    cands = []
    if os.environ.get("BSK_INSTALL_DIR"):
        cands.append(Path(os.environ["BSK_INSTALL_DIR"]) / "bsk.exe")
    home = Path.home()
    cands += [home / ".local" / "bin" / "bsk.exe", home / ".local" / "bin" / "bsk"]
    w = shutil.which("bsk")
    if w:
        cands.append(Path(w))
    for c in cands:
        if c.exists():
            return str(c)
    sys.exit("[x] 未找到 bsk CLI。请先安装 browser-skill（见 skill 的 SKILL.md「环境」一节）。")


BSK = find_bsk()
# BSK_AUTO_START=0：禁止每条命令各自拉起 daemon，统一复用宿主那个
ENV = dict(os.environ, BSK_AUTO_START="0")


def bsk(*args, timeout=180, as_json=False):
    """调用 bsk。as_json=True 时返回解析后的对象（失败返回 None）。"""
    p = subprocess.run([BSK, *args], capture_output=True, env=ENV, timeout=timeout)
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    if not as_json:
        return p.returncode, out, err
    txt = out.strip()
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"(\{.*\}|\[.*\])", out, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
        return None


def ev(sid, expr):
    """求值 JS，返回 value。"""
    d = bsk("evaluate", expr, "--session", sid, "--json", as_json=True)
    return (d or {}).get("value")


def ensure_daemon():
    st = bsk("status", "--json", as_json=True)
    if st and st.get("pid"):
        if not st.get("browsers"):
            sys.exit("[x] daemon 在运行，但没有浏览器连接。\n"
                     "    请在 Chrome 里确认 browser-skill 扩展已连接（可跑 `bsk doctor`）。")
        return st
    print("[i] daemon 未运行，尝试拉起 ...", file=sys.stderr)
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([BSK, "daemon", "start"], env=ENV, creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL)
    for _ in range(12):
        time.sleep(1)
        st = bsk("status", "--json", as_json=True)
        if st and st.get("pid"):
            return st
    sys.exit("[x] daemon 起不来（沙箱会回收后台子进程）。\n"
             "    请在 WorkBuddy 里用「持久后台任务」跑一次：\n"
             "        bsk daemon start --foreground\n"
             "    然后重跑本脚本。也可直接跑 `python cnki.py doctor` 看诊断。")


@contextlib.contextmanager
def session(name="cnki"):
    """会话与工作同生共死 —— 规避跨 shell 调用时 Agent Window 被回收导致 session 失效。"""
    d = bsk("session", "start", "--name", name, "--json", as_json=True)
    sid = (d or {}).get("session_id")
    if not sid:
        sys.exit("[x] 起会话失败：%s" % d)
    try:
        yield sid
    finally:
        bsk("session", "stop", sid)


# ---------- 前缀（WebVPN 票据）----------

def read_prefix_cache():
    try:
        return json.loads(CACHE.read_text("utf-8")).get("prefix")
    except Exception:
        return None


def write_prefix_cache(prefix):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"prefix": prefix, "ts": time.time()}, ensure_ascii=False),
                     "utf-8")


def prefix_from_user_tabs(sid):
    """用户自己开的「知网检索结果页」标签是最省事的票据来源。"""
    d = bsk("tab", "list", "--scope", "user", "--session", sid, "--json", as_json=True) or {}
    for t in d.get("tabs", []):
        u = t.get("url", "")
        m = re.match(r"(https://[^/]+/https/webvpn[0-9a-fA-F]{32})/kns8s/", u)
        if m:
            return m.group(1)
    return None


def parse_refs(obs):
    items = []
    for m in re.finditer(r"@(e\d+)\s+(textbox|button|link|combobox)\s*(?:\"([^\"]*)\")?", obs):
        items.append({"ref": m.group(1), "kind": m.group(2), "text": m.group(3) or ""})
    return items


def pick(items, kind, contains):
    for it in items:
        if it["kind"] == kind and contains in it["text"]:
            return it["ref"]
    for it in items:
        if it["kind"] == kind:
            return it["ref"]
    return None


def resolve_prefix(sid, kw="片上网络", field="SU"):
    """拿到可用的检索页前缀。

    实测规律：
      · 从机构 WebVPN 门户点「知网公网站点」进去，拿到的是**入口票据**（形如 webvpndd50a13c...），
        拿它拼 /kns8s/defaultresult/... 会显示「页面不存在」；
      · 真正能用的检索页票据要在**实际检索一次之后**从地址栏取（形如 webvpn34dba545...）。

    门户入口地址需自行配置（见 get_portal）。若用户已打开过知网检索结果页，
    则直接复用其标签页里的票据，无需任何配置。
    """
    p = prefix_from_user_tabs(sid)
    if p:
        write_prefix_cache(p)
        return p

    portal = get_portal()
    if not portal:
        sys.exit("[x] 没有可复用的知网标签页，且未配置机构 WebVPN 门户入口。\n"
                 "    二选一：\n"
                 "      ① 配置入口后再跑：\n"
                 "           python cnki.py config --set \"https://<门户域名>/enlink/#/client/app\"\n"
                 "         （也可用环境变量 CNKI_PORTAL，临时指定）\n"
                 "      ② 不用配置：在浏览器里手动打开知网并检索一次，\n"
                 "         脚本会直接从这个检索结果页取票据，然后重跑本命令即可。")

    print("[i] 用户标签页里没有现成的知网检索页票据，走门户入口 ...", file=sys.stderr)
    bsk("navigate", portal, "--session", sid)
    time.sleep(5)
    _, obs, _ = bsk("observe", "--session", sid)
    items = parse_refs(obs)
    btn = pick(items, "button", "知网")
    if not btn:
        sys.exit("[x] 门户里找不到「知网」入口按钮，请手动在浏览器打开知网后重试。")
    bsk("click", btn, "--session", sid)
    time.sleep(7)

    _, obs, _ = bsk("observe", "--session", sid)
    items = parse_refs(obs)
    box = pick(items, "textbox", "文献")
    go = pick(items, "button", "检索")
    if not (box and go):
        sys.exit("[x] 知网首页检索框定位失败，请手动检索一次后重试（或看 SKILL.md「手工兜底」）。")
    bsk("fill", box, "--value", kw, "--session", sid)
    bsk("click", go, "--session", sid)

    for _ in range(20):
        time.sleep(1)
        url = ev(sid, "location.href") or ""
        if "/kns8s/defaultresult/" in url:
            m = TICKET_RE.match(url)
            if m:
                write_prefix_cache(m.group(1))
                return m.group(1)
    sys.exit("[x] 检索后没拿到结果页票据（可能登录态已失效，请先在浏览器里确认能正常检索）。")


def search_url(prefix, kw, field="SU"):
    return "%s/kns8s/defaultresult/index?korder=%s&kw=%s" % (
        prefix, field, urllib.parse.quote(kw))


# ---------- 动作 ----------

def cmd_doctor(_a):
    print("bsk CLI :", BSK)
    rc, out, _ = bsk("--version")
    print("版本    :", out.strip())
    st = ensure_daemon()
    print("daemon  : pid=%s 会话数=%s" % (st.get("pid"), len(st.get("sessions", []))))
    for b in st.get("browsers", []):
        print("浏览器  : %s %s / 扩展 %s / 协议 %s / skew=%s" % (
            b.get("browser_name"), b.get("browser_version"),
            b.get("extension_version"), b.get("extension_protocol_version"),
            b.get("version_skew")))
    p = read_prefix_cache()
    print("门户入口:", get_portal() or "（未配置 —— 用 `config --set` 写入，或手动开一次知网）")
    print("缓存前缀:", p or "（无）")
    if p:
        with session("cnki-doctor") as sid:
            bsk("navigate", search_url(p, "片上网络"), "--session", sid)
            time.sleep(6)
            n = ev(sid, "document.querySelectorAll('.result-table-list tbody tr').length")
            ttl = ev(sid, "(document.querySelector('.pagerTitleCell')||{}).innerText||''")
        print("票据自检:", ("通过 " + str(ttl)) if (n or 0) > 0 else
              "失效（0 条结果）→ 删掉 %s 后重跑即可自动刷新" % CACHE)
    print("\n提示：以上自检通过后，即可 search / detail。")
    return 0


def _extract_rows(sid):
    v = ev(sid, EXTRACT_JS)
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return v or []


def _short(tok, head=8, tail=14):
    """CNKI 的 v token 前 16 位是本次检索会话的公共盐，只截头会让各行长得一模一样。
    所以「头 … 尾」一起给，方便肉眼区分；但喂给 detail/download 时必须用完整值。"""
    if not tok:
        return "（无）"
    if len(tok) <= head + tail + 3:
        return tok
    return "%s…%s" % (tok[:head], tok[-tail:])


def cmd_config(a):
    if a.set:
        set_portal(a.set)
        return 0
    cur = get_portal()
    if not cur:
        print("门户入口：未配置")
        print("  写入：cnki.py config --set \"https://<门户域名>/enlink/#/client/app\"")
        print("  或设环境变量 CNKI_PORTAL（临时）")
        print("  不配也行：只要浏览器里已打开过知网检索结果页，脚本会直接复用其票据。")
        return 0
    src = ("环境变量 CNKI_PORTAL" if (os.environ.get("CNKI_PORTAL") or "").strip()
           else str(PORTAL_FILE))
    print("门户入口：%s" % cur)
    print("来源    ：%s" % src)
    print("缓存票据：%s" % (read_prefix_cache() or "（无）"))
    return 0


def cmd_prefix(_a):
    with session("cnki-prefix") as sid:
        p = resolve_prefix(sid)
    print(p)
    return 0


def cmd_search(a):
    with session("cnki-search") as sid:
        prefix = read_prefix_cache() or resolve_prefix(sid, a.keyword, a.field)
        url = search_url(prefix, a.keyword, a.field)
        bsk("navigate", url, "--session", sid)

        rows, total = [], ""
        for _ in range(25):
            time.sleep(1)
            rows = _extract_rows(sid)
            if rows:
                break
        total = ev(sid, "(document.querySelector('.pagerTitleCell')||{}).innerText||''") or ""

        if not rows:
            # 票据可能过期：清缓存重来一次
            print("[i] 0 条结果，票据可能已失效，刷新前缀后重试 ...", file=sys.stderr)
            CACHE.unlink(missing_ok=True)
            prefix = resolve_prefix(sid, a.keyword, a.field)
            bsk("navigate", search_url(prefix, a.keyword, a.field), "--session", sid)
            for _ in range(25):
                time.sleep(1)
                rows = _extract_rows(sid)
                if rows:
                    break
            total = ev(sid, "(document.querySelector('.pagerTitleCell')||{}).innerText||''") or ""

        for _ in range(max(0, a.pages - 1)):
            r = bsk("click", "#PageNext", "--session", sid, as_json=True)
            if not r or r.get("error") or "tab_id" not in (r or {}):
                print("[!] 翻页失败（可能已到末页），停止。", file=sys.stderr)
                break
            time.sleep(4)
            rows += _extract_rows(sid)

    if not rows:
        print("[x] 没抓到结果。请在浏览器里确认能正常检索，然后重跑。")
        return 1

    print("关键词：%s   字段：%s" % (a.keyword, a.field))
    print("结果：%s    本次抓到 %d 条\n" % (total.strip() or "?", len(rows)))
    for r in rows:
        print("%-4s %s" % (r.get("seq", ""), r.get("title", "")))
        print("     作者：%s" % r.get("author", ""))
        print("     来源：%s | %s | %s" % (r.get("source", ""), r.get("date", ""), r.get("type", "")))
        print("     被引/下载：%s / %s   vtoken=%s" % (
            r.get("cited", "0") or "0", r.get("downloads", "0") or "0",
            _short(r.get("vtoken", ""))))
    print("\n[注] 结果列表**不含年卷期页**；要完整著录请对目标条目跑 `detail <vtoken>`。")

    if a.out:
        Path(a.out).write_text(json.dumps(
            {"keyword": a.keyword, "field": a.field, "total": total, "rows": rows},
            ensure_ascii=False, indent=2), "utf-8")
        print("已写出：%s" % a.out)
    return 0


def norm_citation(s):
    s = re.sub(r"查看该刊.*$", "", s or "").strip()
    m = re.match(r"^(?P<j>.+?)\s*\.\s*(?P<y>\d{4})\s*,\s*(?P<v>\d+)\s*\(\s*0*(?P<i>\d+)\s*\)\s*:\s*(?P<p>[\d\-–~]+)", s)
    if m:
        return "%s, %s, %s(%d): %s" % (m["j"].strip(), m["y"], m["v"], int(m["i"]), m["p"])
    return s


def _vtoken(s):
    m = re.search(r"[?&]v=([^&\s]+)", s or "")
    return m.group(1) if m else (s or "").strip()


def cmd_detail(a):
    d = None
    with session("cnki-detail") as sid:
        prefix = read_prefix_cache() or resolve_prefix(sid)
        url = "%s/kcms2/article/abstract?v=%s" % (prefix, _vtoken(a.target))
        bsk("navigate", url, "--session", sid)
        for _ in range(20):
            time.sleep(1)
            raw = ev(sid, DETAIL_JS)
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = None
            if isinstance(raw, dict) and (raw.get("citation") or raw.get("title")):
                d = raw
                break
    if not d:
        print("[x] 知网节没读到内容，请检查 vtoken 是否过期（重新检索可拿到新的）。")
        return 1
    d["citation_gbt"] = norm_citation(d.get("citation", ""))
    print("标题：" + d.get("title", ""))
    print("作者：" + d.get("authors", ""))
    print("出处：" + d.get("citation", ""))
    print("著录：" + d["citation_gbt"])
    if not re.search(r"\d{4}\s*,\s*\d+\s*\(", d["citation_gbt"]):
        print("\n[!] 这条没抽出「年, 卷(期): 页」——多半是**学位论文/会议论文**："
              "知网节的 .top-tip 对它们给的是院校/主办信息，不含卷期页。")
        print("    学位论文的 GB/T 7714 格式是 `[D]. 城市: 学校, 年.`，"
              "年份请取结果列表的 date 字段（或知网节正文里的「授予年度」）。")
    if a.out:
        Path(a.out).write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")
        print("\n已写出：" + a.out)
    return 0


def cmd_download(a):
    """下载需要已登录且有权限；未登录时知网会跳登录页，bsk download 会超时失败（属正常）。"""
    out = Path(a.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with session("cnki-download") as sid:
        prefix = read_prefix_cache() or resolve_prefix(sid)
        if a.vtoken:
            bsk("navigate", "%s/kcms2/article/abstract?v=%s" % (prefix, _vtoken(a.vtoken)),
                "--session", sid)
            time.sleep(7)
        sel = "#pdfDown" if a.kind == "pdf" else "#cajDown"
        r = bsk("download", "--out", str(out), "--session", sid,
                "--selector", sel, "--timeout", a.timeout, "--json", as_json=True)
        if out.exists():
            print("[ok] 已下载：%s（%d 字节）" % (out, out.stat().st_size))
            return 0
        print("[x] 未捕获到下载：%s" % (r or {}).get("message", ""))
        print("    常见原因：知网未登录 / 无该文献下载权限 → 会跳转到登录页。")
        print("    处理：让用户在浏览器里登录知网个人账号（学校机构账号）后重跑。")
        return 1


def main():
    ap = argparse.ArgumentParser(description="知网检索助手（借已登录 Chrome + 机构 WebVPN）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="环境与票据自检").set_defaults(func=cmd_doctor)
    sub.add_parser("prefix", help="刷新并打印检索页前缀").set_defaults(func=cmd_prefix)

    c = sub.add_parser("config", help="查看 / 写入机构 WebVPN 门户入口配置")
    c.add_argument("--set", metavar="URL", help="写入本地配置（默认只查看）")
    c.set_defaults(func=cmd_config)

    s = sub.add_parser("search", help="检索并列出题录")
    s.add_argument("keyword")
    s.add_argument("--field", default="SU", help="SU主题/TI篇名/KY关键词/AB摘要/AU作者/FT全文")
    s.add_argument("--pages", type=int, default=1, help="抓取页数（每页 20 条）")
    s.add_argument("--out", help="结果 JSON 落盘路径")
    s.set_defaults(func=cmd_search)

    d = sub.add_parser("detail", help="打开知网节，取年卷期页等完整著录")
    d.add_argument("target", help="vtoken 或知网节 URL")
    d.add_argument("--out", help="题录 JSON 落盘路径")
    d.set_defaults(func=cmd_detail)

    w = sub.add_parser("download", help="下载 PDF/CAJ（需已登录且有权限）")
    w.add_argument("vtoken", nargs="?", default="", help="vtoken；省略则对当前页面下手")
    w.add_argument("--kind", choices=["pdf", "caj"], default="pdf")
    w.add_argument("--out", required=True)
    w.add_argument("--timeout", default="90s")
    w.set_defaults(func=cmd_download)

    a = ap.parse_args()
    sys.exit(a.func(a) or 0)


if __name__ == "__main__":
    main()
