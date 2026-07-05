#!/usr/bin/env python3
"""douyin-radar

从 TikHub API 采集抖音热点视频的评论。

用法：
    python collect.py --hotlist        # 只打印热榜热词，供肉眼挑选
    python collect.py "关键词"          # 搜索 -> 选最高赞视频 -> 拉评论 -> 写 markdown

所有请求：
  - Base URL   https://api.tikhub.io
  - 请求头     Authorization: Bearer <从 .env 读的 TIKHUB_API_KEY>
  - 每次请求之间 sleep 0.2s，避免超过 10/second 限速
  - 响应可能有 null / 缺字段，一律用 .get() 防御式取值，单条失败就跳过
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

BASE_URL = "https://api.tikhub.io"
RATE_LIMIT_SLEEP = 0.2  # 秒；每次请求之间的间隔，避免触发 10/second 限速
DEBUG = False  # --debug 开启后，打印每次请求的状态码和原始返回

load_dotenv()
API_KEY = (os.getenv("TIKHUB_API_KEY") or "").strip()


# --------------------------------------------------------------------------- #
# HTTP 基础设施
# --------------------------------------------------------------------------- #
def _request(method, path, **kwargs):
    """统一请求：带鉴权头、限速 sleep、异常防御。

    返回解析后的 JSON（通常是 dict），任何失败都返回 None，绝不抛出中断。
    """
    url = BASE_URL + path
    headers = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}
    if DEBUG:
        print(f"  [debug] {method} {url}", file=sys.stderr)
        if kwargs.get("params"):
            print(f"  [debug] params: {kwargs['params']}", file=sys.stderr)
        if kwargs.get("json"):
            print(f"  [debug] body:   {kwargs['json']}", file=sys.stderr)
    try:
        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    except requests.RequestException as exc:
        print(f"  ⚠️  请求异常 {path}: {exc}", file=sys.stderr)
        return None
    finally:
        # 无论成功失败都限速，保证请求节奏
        time.sleep(RATE_LIMIT_SLEEP)

    if DEBUG:
        print(f"  [debug] HTTP {resp.status_code}", file=sys.stderr)
        print(f"  [debug] 原始返回(前 1500 字)：{(resp.text or '')[:1500]}", file=sys.stderr)

    if resp.status_code != 200:
        snippet = (resp.text or "")[:200]
        print(f"  ⚠️  HTTP {resp.status_code} {path}: {snippet}", file=sys.stderr)
        return None

    try:
        return resp.json()
    except ValueError as exc:
        print(f"  ⚠️  响应不是合法 JSON {path}: {exc}", file=sys.stderr)
        return None


def _dig(obj, *keys):
    """安全地从嵌套 dict 里逐层取值，任何一层缺失或非 dict 都返回 None。"""
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def human(n):
    """把点赞数转成 '12.3万' 之类的可读形式；转换失败原样返回。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


# --------------------------------------------------------------------------- #
# 接口 1：热榜
# --------------------------------------------------------------------------- #
def fetch_hot_list(board_type=0):
    """GET /api/v1/douyin/app/v3/fetch_hot_search_list

    热词在 data.word_list[]，每项含 word（还有 hot_value、video_count 可选）。
    """
    data = _request(
        "GET",
        "/api/v1/douyin/app/v3/fetch_hot_search_list",
        params={"board_type": board_type},
    )
    word_list = _dig(data, "data", "word_list")
    return word_list if isinstance(word_list, list) else []


# --------------------------------------------------------------------------- #
# 接口 2：视频搜索（POST）
# --------------------------------------------------------------------------- #
def search_videos(keyword):
    """POST /api/v1/douyin/search/fetch_video_search_v2

    请求体：{"keyword": <词>, "cursor": 0, "sort_type": "0"}
    视频在 business_data[]，视频信息在 business_data[i].data.aweme_info。
    广告/非视频卡片没有 aweme_info，跳过。
    """
    body = {"keyword": keyword, "cursor": 0, "sort_type": "0"}
    data = _request(
        "POST",
        "/api/v1/douyin/search/fetch_video_search_v2",
        json=body,
    )
    if not data:
        return []

    # business_data 可能在顶层，也可能在 data.business_data 下，两种都兜住
    business_data = data.get("business_data")
    if business_data is None:
        business_data = _dig(data, "data", "business_data")
    if not isinstance(business_data, list):
        return []

    videos = []
    for elem in business_data:
        aweme = _dig(elem, "data", "aweme_info")
        if not isinstance(aweme, dict):
            continue  # 广告 / 非视频卡片，没有 aweme_info
        aweme_id = aweme.get("aweme_id")
        if not aweme_id:
            continue
        stats = aweme.get("statistics") or {}
        videos.append(
            {
                "aweme_id": aweme_id,
                "desc": aweme.get("desc") or "",
                "digg_count": stats.get("digg_count") or 0,
                "comment_count": stats.get("comment_count") or 0,
                "url": f"https://www.douyin.com/video/{aweme_id}",
            }
        )
    return videos


# --------------------------------------------------------------------------- #
# 接口 3：视频评论
# --------------------------------------------------------------------------- #
def fetch_comments(aweme_id, max_pages=3, count=20):
    """GET /api/v1/douyin/app/v3/fetch_video_comments

    参数 aweme_id, cursor, count。评论在 comments[]，每条取 text、digg_count、ip_label。
    翻页看 cursor / has_more，最多翻 max_pages 页。
    """
    collected = []
    cursor = 0
    for _ in range(max_pages):
        data = _request(
            "GET",
            "/api/v1/douyin/app/v3/fetch_video_comments",
            params={"aweme_id": aweme_id, "cursor": cursor, "count": count},
        )
        if not data:
            break

        # comments 可能在顶层，也可能在 data 下
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        comments = payload.get("comments")
        if not isinstance(comments, list):
            break

        for c in comments:
            if not isinstance(c, dict):
                continue
            collected.append(
                {
                    "text": c.get("text") or "",
                    "digg_count": c.get("digg_count") or 0,
                    "ip_label": c.get("ip_label") or "",
                }
            )

        has_more = payload.get("has_more")
        next_cursor = payload.get("cursor")
        if not has_more or next_cursor is None or next_cursor == cursor:
            break
        cursor = next_cursor

    return collected


# --------------------------------------------------------------------------- #
# 评论过滤
# --------------------------------------------------------------------------- #
# 含义字符：汉字、字母、数字。用来判断一条评论是不是纯表情/纯标点。
_MEANINGFUL_RE = re.compile(r"[一-鿿㐀-䶿A-Za-z0-9]")


def is_quality_comment(text):
    """过滤：纯表情 / 纯标点 / 长度小于 8 字 的评论一律剔除。"""
    t = (text or "").strip()
    if len(t) < 8:
        return False
    if not _MEANINGFUL_RE.search(t):  # 没有任何汉字/字母/数字 -> 纯表情或纯标点
        return False
    return True


# --------------------------------------------------------------------------- #
# 输出 markdown
# --------------------------------------------------------------------------- #
def write_markdown(keyword, video, comments):
    os.makedirs("output", exist_ok=True)
    date = datetime.now().strftime("%Y%m%d")
    safe = re.sub(r"[^\w一-鿿-]", "_", keyword).strip("_")[:30] or "keyword"
    path = os.path.join("output", f"{safe}_{date}.md")

    lines = [
        f"# 抖音热点评论采集：{keyword}",
        "",
        f"- 采集时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 关键词：{keyword}",
        "",
        "## 选中视频（点赞最高）",
        "",
        f"- 文案：{video['desc'] or '(无文案)'}",
        f"- 点赞：{human(video['digg_count'])}（{video['digg_count']}）",
        f"- 评论数：{human(video['comment_count'])}（{video['comment_count']}）",
        f"- 链接：{video['url']}",
        "",
        f"## 高赞评论 Top {len(comments)}",
        "",
    ]

    if not comments:
        lines.append("_（没有符合条件的评论）_")
    else:
        for i, c in enumerate(comments, 1):
            ip = c["ip_label"] or "未知"
            lines.append(f"{i}. {c['text']}")
            lines.append(f"   - 👍 {human(c['digg_count'])}（{c['digg_count']}）　📍 {ip}")
            lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    return path


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def cmd_hotlist():
    print("正在获取抖音热榜…")
    words = fetch_hot_list()
    if not words:
        print("没有拿到热榜数据（检查 API Key 或接口是否变动）。")
        return
    print(f"共 {len(words)} 个热词：\n")
    for i, item in enumerate(words, 1):
        if not isinstance(item, dict):
            continue
        word = item.get("word") or "(空)"
        hot = item.get("hot_value")
        vc = item.get("video_count")
        parts = [f"{i:>2}. {word}"]
        if hot is not None:
            parts.append(f"🔥 {human(hot)}")
        if vc is not None:
            parts.append(f"🎬 {vc}")
        print("   ".join(parts))


def cmd_collect(keyword):
    print(f"正在搜索「{keyword}」…")
    videos = search_videos(keyword)
    if not videos:
        print("没有搜到有效视频，退出。")
        return

    # 接口默认不是按点赞排序的，自己按 digg_count 从高到低排，取最高的那条
    videos.sort(key=lambda v: v["digg_count"], reverse=True)
    top = videos[0]
    print(f"找到 {len(videos)} 条视频，最高赞 {human(top['digg_count'])}")
    desc_preview = top["desc"][:40] + ("…" if len(top["desc"]) > 40 else "")
    print(f"选中视频：{desc_preview or '(无文案)'}  {top['url']}")

    print("正在拉取评论…")
    raw = fetch_comments(top["aweme_id"])
    filtered = [c for c in raw if is_quality_comment(c["text"])]
    filtered.sort(key=lambda c: c["digg_count"], reverse=True)
    top10 = filtered[:10]
    print(f"拉到 {len(raw)} 条评论，过滤后剩 {len(filtered)} 条，取前 {len(top10)} 条")

    path = write_markdown(keyword, top, top10)
    print(f"✅ 已写入 {path}")


def main():
    parser = argparse.ArgumentParser(
        description="从 TikHub API 采集抖音热点视频的评论",
    )
    parser.add_argument("keyword", nargs="?", help="要采集的关键词")
    parser.add_argument(
        "--hotlist", action="store_true", help="只调热榜接口，打印热词供肉眼挑选"
    )
    parser.add_argument(
        "--debug", action="store_true", help="打印每次请求的状态码和原始返回，便于排查"
    )
    args = parser.parse_args()

    global DEBUG
    DEBUG = args.debug

    if not args.hotlist and not args.keyword:
        parser.print_help()
        sys.exit(1)

    if not API_KEY:
        print("❌ 未找到 TIKHUB_API_KEY，请在 .env 里配置。", file=sys.stderr)
        sys.exit(1)

    if args.hotlist:
        cmd_hotlist()
    else:
        cmd_collect(args.keyword)


if __name__ == "__main__":
    main()
