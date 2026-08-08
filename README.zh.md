# 🧡 SpaceBox 中文版

一个基于 FastAPI 和 SQLite 的轻量级、自包含社交发布平台。

## 关于

SpaceBox 是一个社交媒体和个人空间应用，支持账号、帖子、图片和视频、定时发布、隐私控制、关注、搜索以及嵌套评论。

完整应用位于单个 Python 文件：

`spacebox_standalone.py`

无需独立前端构建步骤、模板目录、静态目录或外部数据库服务器。

## 快速开始

推荐使用 Python 3.11 或更高版本。

```bash
git clone https://github.com/wangyifan349/SpaceBox.git
cd SpaceBox
pip install -r requirements.txt
python3 spacebox_standalone.py
```

默认地址：

`http://127.0.0.1:8000`

首次启动会自动创建 SQLite 数据库 `social.db`。

## 功能

- 用户注册、登录和个人资料
- 发布文字、图片和视频
- 定时发布
- 公开、关注者、私密三种可见范围
- 关注系统
- 实时搜索
- 多层嵌套评论
- SQLite 媒体存储

这是原 README.md 的中文翻译版本。
