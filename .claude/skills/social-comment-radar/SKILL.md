---
name: social-comment-radar
description: Build or run a command-line Python collector that pulls trending/hot content and its top comments from a social platform (Douyin, Xiaohongshu/RED, X/Twitter, YouTube, etc.) through a data API such as TikHub, and writes a Markdown report. Use when the user wants to scrape or collect hot posts plus their comments from a social platform into a file — especially when adapting the existing douyin-radar script to a new platform.
---

# Social Comment Radar

采集某个社交平台的**热点内容 + 高赞评论**，产出一个 Markdown 报告。
这是 `douyin-radar`（抖音）跑通的做法的通用版，可套用到小红书、X、YouTube 等平台。

## 什么时候用

用户想「采集 / 抓取某平台的热点内容和评论，导出成文件」时。典型说法：
「帮我采集小红书某关键词的笔记和评论」「把 YouTube 上某话题的热门视频评论拉下来」。

## 核心思路（跨平台不变）

一个平台通常需要三类接口，用一个数据 API（如 [TikHub](https://tikhub.io)）拿到：

1. **热榜 / 趋势**：拿到当下热词，供人肉眼挑选。
2. **内容搜索**：按关键词搜内容，选互动量（点赞/收藏/播放）最高的那条。
3. **评论列表**：拉选中内容的评论，过滤 + 排序后取前 N 条。

CLI 就两个命令：
- `python collect.py --hotlist` —— 只调热榜，打印热词让用户挑。
- `python collect.py "关键词"` —— 搜索 → 选最高互动内容 → 拉评论 → 过滤排序 → 写 Markdown 到 `output/`。

## 通用规则（每个平台都照做）

- **先核对接口**：动手前去 API 文档 / Swagger 确认这三个接口的**准确路径、方法（GET/POST）、响应结构**，不要凭记忆写死。用户给的实测结构也要核对一遍。
- **Base URL + 鉴权头**：所有请求带 `Authorization: Bearer <从 .env 读的 key>`。
- **限速**：每次请求之间 `time.sleep(0.2)`，避免超过 API 的每秒请求上限（如 10/s）。
- **防御式取值**：响应随时可能有 `null` 或缺字段，一律 `.get()`；**单条失败就跳过，绝不整个中断**。
- **嵌套不写死**：数据 API 常把真正的数据多套几层（`data.data.xxx`）。用**递归查找**定位目标字段，别写死路径——这是最容易踩的坑（抖音就多套了一层）。
- **自己排序**：搜索接口默认往往**不是**按互动量排的，务必自己按点赞/收藏/播放排序再取最高，别直接拿第 0 条。
- **跳过非内容卡片**：搜索结果里常混着广告 / 话题卡，没有内容主体字段，要跳过。
- **进度打印**：关键步骤打印中文进度（「正在搜索…」「找到 N 条，最高赞 XX 万」「拉到 N 条评论，过滤后剩 M 条」），方便用户看它在干嘛。
- **评论质量过滤**：剔除纯表情、纯标点、长度过短（如 < 8 字）的评论。

## 项目结构

```
<platform>-radar/
├── collect.py          # 主脚本
├── requirements.txt    # requests、python-dotenv
├── .env                # <PLATFORM>_API_KEY=（gitignore，不提交）
├── .env.example        # 模板
├── .gitignore          # 忽略 .env、output/*、__pycache__
├── output/.gitkeep     # 生成的报告放这里（内容 gitignore）
└── README.md
```

`.gitignore` 关键几行：
```
.env
output/*
!output/.gitkeep
__pycache__/
```

## 复用的代码骨架

以下工具函数**平台无关**，直接照搬；每个平台只需改三个 `fetch_*` 里的**接口路径和字段名**。

```python
import os, re, sys, time
from datetime import datetime
import requests
from dotenv import load_dotenv

BASE_URL = "https://api.tikhub.io"      # ← 换成目标 API
RATE_LIMIT_SLEEP = 0.2
DEBUG = False

load_dotenv()
API_KEY = (os.getenv("TIKHUB_API_KEY") or "").strip()   # ← 换成 <PLATFORM>_API_KEY


def _request(method, path, **kwargs):
    """带鉴权头、限速、异常防御的统一请求；失败返回 None，不抛出。--debug 打印原始返回。"""
    url = BASE_URL + path
    headers = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}
    if DEBUG:
        print(f"  [debug] {method} {url}  params={kwargs.get('params')} body={kwargs.get('json')}",
              file=sys.stderr)
    try:
        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    except requests.RequestException as exc:
        print(f"  ⚠️  请求异常 {path}: {exc}", file=sys.stderr)
        return None
    finally:
        time.sleep(RATE_LIMIT_SLEEP)
    if DEBUG:
        print(f"  [debug] HTTP {resp.status_code}  原始返回: {(resp.text or '')[:1500]}", file=sys.stderr)
    if resp.status_code != 200:
        print(f"  ⚠️  HTTP {resp.status_code} {path}: {(resp.text or '')[:200]}", file=sys.stderr)
        return None
    try:
        return resp.json()
    except ValueError as exc:
        print(f"  ⚠️  响应不是合法 JSON {path}: {exc}", file=sys.stderr)
        return None


def _find_container(obj, key):
    """深度优先找到第一个「含有 key 且 key 对应 list」的 dict。应对 data.data.xxx 这类多层嵌套。"""
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


def human(n):
    """点赞数转 '12.3万'；转换失败原样返回。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    return f"{n / 10000:.1f}万" if n >= 10000 else str(n)


_MEANINGFUL_RE = re.compile(r"[一-鿿㐀-䶿A-Za-z0-9]")

def is_quality_comment(text):
    """剔除纯表情 / 纯标点 / 长度 < 8 字的评论。"""
    t = (text or "").strip()
    return len(t) >= 8 and bool(_MEANINGFUL_RE.search(t))
```

三个 `fetch_*` 用 `_find_container` 取数据，例如：
```python
def fetch_hot_list():
    data = _request("GET", "<热榜接口路径>", params={...})
    c = _find_container(data, "<热词列表字段名>")   # 抖音是 "word_list"
    return c["<热词列表字段名>"] if c else []
```

## 落地步骤（给新平台时照做）

1. 找到该平台在数据 API 里的三个接口，**核对文档**里的路径、方法、字段名。
2. `cp` 一份 douyin-radar，改 `BASE_URL`、`API_KEY` 变量名、三个 `fetch_*` 的路径和字段。
3. 先跑 `--hotlist --debug`，看 `原始返回` 确认字段真实位置，调 `_find_container` 的 key。
4. 跑 `python collect.py "关键词" --debug` 跑通完整链路。
5. 打开 `output/` 里的 md 检查内容。

## 排查：加 `--debug` 开关

任何一步「没拿到数据」时，用 `--debug` 打印 **HTTP 状态码 + 原始返回前 1500 字**：
- `401 / 403` → key 没生效 / 没订阅该接口。
- `404` → 路径错了。
- `200 但取不到` → 字段被套更深，看原始返回调 `_find_container` 的 key。

## 各平台待填清单

| 平台 | 数据 API 里对应的接口族 | 要确认的字段（热词 / 内容 / 评论） |
| --- | --- | --- |
| 抖音 | Douyin App V3 / Search | `word_list` / `business_data[].data.aweme_info` / `comments[]`（已跑通，见 douyin-radar） |
| 小红书 | Xiaohongshu / RED | 待查：热榜词、笔记搜索结果、笔记评论 |
| X / Twitter | Twitter | 待查：trends、tweet 搜索、tweet 回复 |
| YouTube | YouTube | 待查：trending、video 搜索、video 评论 |

> 每个平台只需重复「核对接口 → 改路径和字段 → --debug 跑通」，其余逻辑（限速、防御取值、递归查找、过滤排序、markdown 输出）完全复用。
