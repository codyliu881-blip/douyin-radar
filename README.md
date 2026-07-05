# douyin-radar

一个独立的命令行脚本，从 [TikHub](https://tikhub.io) API 采集抖音热点视频的评论。
没有网页、没有数据库、没有定时任务——就是一个 `collect.py`。

## 安装

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入你的 TikHub API Key
```

`.env`：

```
TIKHUB_API_KEY=你的key
```

## 用法

看今天的热榜，肉眼挑词：

```bash
python collect.py --hotlist
```

采集某个关键词（搜索 → 选点赞最高的视频 → 拉评论 → 过滤排序 → 写 markdown）：

```bash
python collect.py "关键词"
```

结果写到 `output/<关键词>_<日期>.md`，包含：

- 关键词
- 选中视频的文案、点赞、评论数、链接
- 过滤后按点赞排序的前 10 条评论（内容、点赞数、IP 属地）

## 说明

- Base URL：`https://api.tikhub.io`，请求头 `Authorization: Bearer <key>`。
- 每次请求之间 sleep 0.2 秒，避免超过 10/second 限速。
- 响应字段全部用 `.get()` 防御式取值，单条数据出错就跳过，不会整体中断。
- 评论过滤规则：剔除纯表情、纯标点、长度小于 8 字的评论。
- 视频按点赞（`digg_count`）自己排序取最高，不直接用接口返回的第一条。

用到的三个 TikHub 接口：

| 功能 | 方法 | 路径 |
| --- | --- | --- |
| 热榜 | GET | `/api/v1/douyin/app/v3/fetch_hot_search_list` |
| 视频搜索 | POST | `/api/v1/douyin/search/fetch_video_search_v2` |
| 视频评论 | GET | `/api/v1/douyin/app/v3/fetch_video_comments` |
