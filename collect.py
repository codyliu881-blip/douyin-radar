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

TOP_VIDEOS = 2  # 每个关键词输出几个视频（按点赞从高到低取）
TOP_COMMENTS = 20  # 每个视频取几条高赞评论
COMMENT_PAGES = 5  # 每个视频翻几页评论（接口无"按赞排序"，只能靠多翻扩大样本；一页约20条）

# 搜索相关（一页只返回约 6~10 条，所以要翻多页）
SEARCH_PAGES = 5  # 搜索翻几页，扩大候选池
SEARCH_SORT_TYPE = "1"  # 排序：'0'综合 / '1'最多点赞 / '2'最新发布
SEARCH_PUBLISH_TIME = "180"  # 服务端发布时间过滤：'0'不限 / '1'一天 / '7'一周 / '180'半年内
RECENT_DAYS = 0  # 客户端兜底过滤：只保留最近 N 天发布的视频，0=关闭（比服务端过滤更可靠）

# 按博主采集相关
USER_PAGES = 5  # 拉博主作品翻几页（越大越能覆盖到历史高赞老视频）

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


def _find_container(obj, key):
    """深度优先在嵌套 dict/list 里找到第一个「含有 key 且 key 对应 list」的 dict。

    TikHub 的返回经常把真正的数据多套几层（data.data.xxx 之类），
    用递归查找就不必写死具体路径，接口小改动也不容易崩。找不到返回 None。
    """
    if isinstance(obj, dict):
        if isinstance(obj.get(key), list):
            return obj
        for value in obj.values():
            found = _find_container(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_container(item, key)
            if found is not None:
                return found
    return None


def fmt_date(ts):
    """把 unix 秒时间戳转成 '2024-05-18'；缺失或异常返回 '未知'。"""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "未知"


def human(n):
    """把点赞数转成 '12.3万' 之类的可读形式；转换失败原样返回。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def _video_from_aweme(aweme):
    """从一个 aweme(视频)对象抽出我们要的字段；不是有效视频返回 None。

    搜索和「博主作品列表」两个接口的视频对象结构一致，共用这个函数。
    """
    if not isinstance(aweme, dict):
        return None
    aweme_id = aweme.get("aweme_id")
    if not aweme_id:
        return None
    stats = aweme.get("statistics") or {}
    author = aweme.get("author") or {}
    return {
        "aweme_id": aweme_id,
        "desc": aweme.get("desc") or "",
        "digg_count": stats.get("digg_count") or 0,
        "comment_count": stats.get("comment_count") or 0,
        "create_time": aweme.get("create_time"),  # 发布时间，unix 秒
        "author": author.get("nickname") or "",
        "url": f"https://www.douyin.com/video/{aweme_id}",
    }


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
    container = _find_container(data, "word_list")
    return container["word_list"] if container else []


# --------------------------------------------------------------------------- #
# 接口 2：视频搜索（POST）
# --------------------------------------------------------------------------- #
def search_videos(keyword, max_pages=SEARCH_PAGES):
    """POST /api/v1/douyin/search/fetch_video_search_v2

    请求体：{"keyword": <词>, "cursor": <页>, "sort_type": SEARCH_SORT_TYPE, ...}
    视频在 business_data[]，视频信息在 business_data[i].data.aweme_info。
    广告/非视频卡片没有 aweme_info，跳过。翻多页扩大候选池，跨页按 aweme_id 去重。
    """
    videos = []
    seen = set()
    cursor = 0
    for _ in range(max_pages):
        body = {
            "keyword": keyword,
            "cursor": cursor,
            "sort_type": SEARCH_SORT_TYPE,
            "publish_time": SEARCH_PUBLISH_TIME,
        }
        data = _request(
            "POST",
            "/api/v1/douyin/search/fetch_video_search_v2",
            json=body,
        )
        if not data:
            break

        # business_data 不管被套多深，递归找到那一层
        container = _find_container(data, "business_data")
        if not container:
            break
        business_data = container["business_data"]

        for elem in business_data:
            aweme = _dig(elem, "data", "aweme_info")  # 搜索结果里视频套在 data.aweme_info
            video = _video_from_aweme(aweme)
            if not video or video["aweme_id"] in seen:
                continue  # 广告/非视频卡片，或重复
            seen.add(video["aweme_id"])
            videos.append(video)

        # 翻页：cursor / has_more 通常和 business_data 同级
        has_more = container.get("has_more")
        next_cursor = container.get("cursor")
        if not has_more or next_cursor is None or next_cursor == cursor:
            break
        cursor = next_cursor

    return videos


# --------------------------------------------------------------------------- #
# 接口 4：博主作品列表（按博主采集用）
# --------------------------------------------------------------------------- #
def extract_sec_user_id(target):
    """从主页链接里抽 sec_user_id；已经是 sec_user_id 就原样返回。"""
    target = (target or "").strip()
    m = re.search(r"/user/([A-Za-z0-9_\-]+)", target)
    if m:
        return m.group(1)
    return target  # 用户直接传了 sec_user_id


def fetch_user_videos(sec_user_id, max_pages=USER_PAGES, count=20):
    """GET /api/v1/douyin/app/v3/fetch_user_post_videos

    参数 sec_user_id, max_cursor, count。作品在 aweme_list[]，结构同搜索的视频对象。
    翻页看 max_cursor / has_more。
    """
    videos = []
    seen = set()
    max_cursor = 0
    for _ in range(max_pages):
        data = _request(
            "GET",
            "/api/v1/douyin/app/v3/fetch_user_post_videos",
            params={"sec_user_id": sec_user_id, "max_cursor": max_cursor, "count": count},
        )
        if not data:
            break

        container = _find_container(data, "aweme_list")
        if not container:
            break

        for aweme in container["aweme_list"]:
            video = _video_from_aweme(aweme)
            if not video or video["aweme_id"] in seen:
                continue
            seen.add(video["aweme_id"])
            videos.append(video)

        has_more = container.get("has_more")
        next_cursor = container.get("max_cursor")
        if not has_more or next_cursor is None or next_cursor == max_cursor:
            break
        max_cursor = next_cursor

    return videos


# --------------------------------------------------------------------------- #
# 接口 3：视频评论
# --------------------------------------------------------------------------- #
def fetch_comments(aweme_id, max_pages=COMMENT_PAGES, count=20):
    """GET /api/v1/douyin/app/v3/fetch_video_comments

    参数 aweme_id, cursor, count。评论在 comments[]，每条取 text、digg_count、ip_label。
    翻页看 cursor / has_more，最多翻 max_pages 页。
    """
    collected = []
    seen = set()  # 相邻页常有重叠，按评论 id / 内容去重
    cursor = 0
    for _ in range(max_pages):
        data = _request(
            "GET",
            "/api/v1/douyin/app/v3/fetch_video_comments",
            params={"aweme_id": aweme_id, "cursor": cursor, "count": count},
        )
        if not data:
            break

        # comments 不管被套多深，递归找到含它的那个 dict（好顺带拿 has_more/cursor）
        container = _find_container(data, "comments")
        if not container:
            break
        comments = container["comments"]

        for c in comments:
            if not isinstance(c, dict):
                continue
            text = c.get("text") or ""
            key = c.get("cid") or c.get("comment_id") or text  # 优先用评论唯一 id 去重
            if key in seen:
                continue
            seen.add(key)
            collected.append(
                {
                    "text": text,
                    "digg_count": c.get("digg_count") or 0,
                    "ip_label": c.get("ip_label") or "",
                }
            )

        has_more = container.get("has_more")
        next_cursor = container.get("cursor")
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
def write_markdown(keyword, sections):
    """sections: [(video, comments), ...]，按点赞从高到低的多个视频，各自带高赞评论。"""
    os.makedirs("output", exist_ok=True)
    date = datetime.now().strftime("%Y%m%d")
    safe = re.sub(r"[^\w一-鿿-]", "_", keyword).strip("_")[:30] or "keyword"
    path = os.path.join("output", f"{safe}_{date}.md")

    lines = [
        f"# 抖音热点评论采集：{keyword}",
        "",
        f"- 采集时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 关键词：{keyword}",
        f"- 视频数：{len(sections)}",
        "",
    ]

    for idx, (video, comments) in enumerate(sections, 1):
        lines += [
            f"## 视频 {idx}（点赞 {human(video['digg_count'])}）",
            "",
            f"- 文案：{video['desc'] or '(无文案)'}",
            f"- 点赞：{human(video['digg_count'])}（{video['digg_count']}）",
            f"- 评论数：{human(video['comment_count'])}（{video['comment_count']}）",
            f"- 发布时间：{fmt_date(video['create_time'])}",
            f"- 链接：{video['url']}",
            "",
            f"### 高赞评论 Top {len(comments)}",
            "",
        ]
        if not comments:
            lines.append("_（没有符合条件的评论）_")
            lines.append("")
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


def _apply_recent_filter(videos):
    """按 RECENT_DAYS 只保留近期视频；关闭或过滤后为空时的处理都在这里。返回过滤后的列表。"""
    if RECENT_DAYS <= 0:
        return videos
    cutoff = time.time() - RECENT_DAYS * 86400
    recent = [v for v in videos if (v.get("create_time") or 0) >= cutoff]
    print(f"按最近 {RECENT_DAYS} 天过滤：{len(videos)} → {len(recent)} 条")
    return recent


def _build_sections(chosen):
    """对选中的每个视频拉评论、过滤、排序取前 TOP_COMMENTS，返回 [(video, comments), ...]。"""
    sections = []
    for idx, video in enumerate(chosen, 1):
        desc_preview = video["desc"][:40] + ("…" if len(video["desc"]) > 40 else "")
        print(f"[{idx}/{len(chosen)}] 赞 {human(video['digg_count'])}  发布 {fmt_date(video['create_time'])}")
        print(f"        {desc_preview or '(无文案)'}  {video['url']}")
        print("  正在拉取评论…")
        raw = fetch_comments(video["aweme_id"])
        filtered = [c for c in raw if is_quality_comment(c["text"])]
        filtered.sort(key=lambda c: c["digg_count"], reverse=True)
        top = filtered[:TOP_COMMENTS]
        print(f"  拉到 {len(raw)} 条评论，过滤后剩 {len(filtered)} 条，取前 {len(top)} 条")
        sections.append((video, top))
    return sections


def _collect_from_videos(subject, videos):
    """公共收尾：按 digg_count 排序取前 TOP_VIDEOS、拉评论、写 markdown。subject 作为标题/文件名。"""
    videos = _apply_recent_filter(videos)
    if not videos:
        print("过滤后没有视频。可调大 RECENT_DAYS，或把 SEARCH_SORT_TYPE 改成 '0'(综合)/'2'(最新)。")
        return
    videos.sort(key=lambda v: v["digg_count"], reverse=True)
    chosen = videos[:TOP_VIDEOS]
    print(f"取点赞最高的 {len(chosen)} 条（最高赞 {human(chosen[0]['digg_count'])}）")
    sections = _build_sections(chosen)
    path = write_markdown(subject, sections)
    print(f"✅ 已写入 {path}")


def cmd_collect(keyword):
    print(f"正在搜索「{keyword}」…")
    videos = search_videos(keyword)
    if not videos:
        print("没有搜到有效视频，退出。")
        return
    print(f"找到 {len(videos)} 条视频")
    _collect_from_videos(keyword, videos)


def cmd_user(target):
    sec_user_id = extract_sec_user_id(target)
    print(f"正在获取博主作品（sec_user_id={sec_user_id[:20]}…）")
    videos = fetch_user_videos(sec_user_id)
    if not videos:
        print("没有拿到该博主的作品（检查主页链接/sec_user_id，或加 --debug 看返回）。")
        return
    author = videos[0].get("author") or ""
    print(f"共 {len(videos)} 条作品" + (f"，博主：{author}" if author else ""))
    subject = author or f"用户_{sec_user_id[:12]}"
    _collect_from_videos(subject, videos)


def main():
    parser = argparse.ArgumentParser(
        description="从 TikHub API 采集抖音热点视频的评论",
    )
    parser.add_argument("keyword", nargs="?", help="要采集的关键词（按话题模式）")
    parser.add_argument(
        "--hotlist", action="store_true", help="只调热榜接口，打印热词供肉眼挑选"
    )
    parser.add_argument(
        "--user", metavar="链接或sec_uid", help="按博主采集：博主主页链接或 sec_user_id"
    )
    parser.add_argument(
        "--debug", action="store_true", help="打印每次请求的状态码和原始返回，便于排查"
    )
    args = parser.parse_args()

    global DEBUG
    DEBUG = args.debug

    if not args.hotlist and not args.user and not args.keyword:
        parser.print_help()
        sys.exit(1)

    if not API_KEY:
        print("❌ 未找到 TIKHUB_API_KEY，请在 .env 里配置。", file=sys.stderr)
        sys.exit(1)

    if args.hotlist:
        cmd_hotlist()
    elif args.user:
        cmd_user(args.user)
    else:
        cmd_collect(args.keyword)


if __name__ == "__main__":
    main()
