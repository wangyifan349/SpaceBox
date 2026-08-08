<div align="center">

# 🧡 SpaceBox

### 一个使用 FastAPI 和 SQLite 构建的轻量级、自包含社交发布平台。

**帖子 · 媒体 · 定时发布 · 嵌套评论 · 隐私 · 关注 · 实时搜索**

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.128%2B-009688?logo=fastapi&logoColor=white)](https://github.com/fastapi/fastapi)
[![SQLite](https://img.shields.io/badge/SQLite-3-003B57?logo=sqlite&logoColor=white)](https://github.com/sqlite/sqlite)
[![许可证](https://img.shields.io/badge/License-AGPL--3.0-blue)](LICENSE)

</div>

---

## ✨ 关于 SpaceBox

**SpaceBox** 是一个紧凑的社交媒体与个人空间应用，灵感来自时间线和微博客平台。

用户可以创建账户、发布文字帖子、附加图片或视频、安排未来定时发布、控制每篇帖子由谁可见、关注其他用户、浏览公开个人主页、实时搜索用户名，并参与深度嵌套的评论讨论。

本项目有意保持部署简单。完整应用位于一个 Python 文件中：

```text
spacebox_standalone.py
```

该文件包含 FastAPI 后端、SQLAlchemy 模型、Jinja2 模板、Bootstrap 资源、项目 CSS、JavaScript、身份验证逻辑、媒体处理、搜索、评论以及 Uvicorn 启动代码。

无需单独的前端构建步骤、`templates/` 目录、`static/` 目录、环境变量配置或外部数据库服务器。

**项目仓库：** https://github.com/wangyifan349/SpaceBox

---

## 🚀 快速开始

建议使用 Python **3.11 或更高版本**。

### 方式 1 — 直接安装所有依赖

```bash
git clone https://github.com/wangyifan349/SpaceBox.git
cd SpaceBox
pip install "fastapi>=0.128,<1.0" "uvicorn[standard]>=0.30" "SQLAlchemy>=2.0,<2.1" "Jinja2>=3.1" "python-multipart>=0.0.20" "itsdangerous>=2.2" "tzdata>=2024.1"
python3 spacebox_standalone.py
```

### 方式 2 — 使用 `requirements.txt`

```bash
git clone https://github.com/wangyifan349/SpaceBox.git
cd SpaceBox
pip install -r requirements.txt
python3 spacebox_standalone.py
```

SpaceBox 优先尝试在以下地址启动：

```text
http://127.0.0.1:8000
```

如果端口 `8000` 已被占用，应用会自动在 `8000` 到 `8049` 之间寻找可用端口。

首次启动时，SpaceBox 会自动创建：

```text
social.db
```

该文件位于 `spacebox_standalone.py` 所在的同一目录中。

---

## 🌟 功能

### 👤 账户与个人主页

- 用户注册、登录和退出登录。
- 唯一用户名与独立的显示名称。
- 支持用户个人简介。
- 账户创建时间戳。
- 可通过用户名访问的公开个人主页。
- 标准个人主页路由：`/u/username`。
- 简短个人主页路由：`/@username`。
- 支持关注与取消关注。
- 可配置新帖子的默认可见性。
- 个人主页统计信息，包括关注者、正在关注、可见帖子和加入时间。

### 📝 帖子

- 发布纯文字帖子。
- 附加图片和视频。
- 保留空格、制表符、换行和缩进。
- 在支持的文本区域内按 `Tab` 可插入缩进。
- 每篇帖子最多 5,000 个字符。
- 立即发布。
- 定时发布。
- 记录创建、更新、计划发布和实际发布时间戳。
- 作者可以删除帖子。

### 👁️ 帖子可见性

每篇帖子都会存储一种可见性模式：

| 值 | 含义 | 谁可以查看 |
| --- | --- | --- |
| `public` | 公开 | 所有人 |
| `followers` | 仅关注者 | 作者以及关注作者的用户 |
| `private` | 私密 | 仅作者本人 |

可见性由后端强制执行。

媒体请求也会验证其所属帖子的可见性，因此直接请求媒体 ID 并不能绕过帖子的权限控制。

### ⏰ 定时发布

定时帖子使用两个字段：

- `scheduled_at` — 请求的发布时间。
- `published_at` — 实际发布时间。

典型状态：

```text
scheduled_at = NULL
published_at = 时间戳
    -> 立即发布

scheduled_at = 未来时间戳
published_at = NULL
    -> 等待定时发布

scheduled_at = 时间戳
published_at = 时间戳
    -> 定时帖子已经发布
```

在发布之前，定时帖子只有作者本人可见，并且不能接收评论。

### 🖼️ 图片与视频

- 每篇帖子最多可附加 6 个媒体文件。
- 每个附件最大 30 MB。
- 图片格式：JPG、JPEG、PNG、GIF、WebP。
- 视频格式：MP4、WebM、MOV、M4V。
- 媒体二进制内容使用 `BLOB` 列直接存储在 SQLite 中。
- 上传内容不会写入单独的媒体目录。
- 媒体通过专用的 FastAPI 路由提供。
- 视频响应支持 HTTP Range 请求，以实现定位播放和部分播放。

### 🌳 嵌套评论树

评论可以在任意深度回复其他评论。

```text
评论 A
├── 回复 B
│   └── 回复 C
└── 回复 D
```

该树通过自引用字段 `comments.parent_id` 表示：

```text
B.parent_id = A.id
C.parent_id = B.id
D.parent_id = A.id
```

评论删除使用**软删除**。

SpaceBox 不会从 SQLite 中删除该行，而是将其标记为已删除并记录删除时间戳。后代回复仍然挂接在同一个节点上：

```text
[这条评论已被作者删除。]
├── 回复 B
│   └── 回复 C
└── 回复 D
```

这样可以保持讨论树的结构完整。

### 🔎 实时用户搜索

导航栏包含一个实时用户名搜索下拉框。

- 用户名排序使用**最长公共子序列（LCS）**相似度。
- 每次搜索请求最多可评估 10,000 个候选账户。
- 最多返回 8 个结果。
- 客户端防抖可减少不必要的请求。
- 支持鼠标选择。
- `Arrow Up` / `Arrow Down` 用于在结果之间导航。
- `Enter` 打开选中的个人主页。
- `Esc` 关闭下拉框。

### 🎨 前端

- 响应式 Bootstrap 界面。
- 宽版帖子和讨论布局。
- 橙色操作按钮主题。
- 内嵌 Bootstrap CSS 和 JavaScript。
- 内嵌应用 CSS 和 JavaScript。
- 无需前端构建步骤。
- 按浏览器本地时间渲染时间戳。
- 在适当位置显示相对时间标签。

---

## 🧩 项目结构

```text
SpaceBox/
├── spacebox_standalone.py   # 完整后端 + 内嵌前端
├── requirements.txt         # 运行时依赖
├── README.md                # 项目文档
├── LICENSE                  # GNU AGPL-3.0 许可证
└── .gitignore               # 运行时/缓存/编辑器排除项
```

首次启动后：

```text
SpaceBox/
├── spacebox_standalone.py
└── social.db
```

`social.db` 文件包含应用数据，其中包括上传的图片和视频二进制内容。

---

## ⚙️ 应用架构

```text
浏览器
   |
   v
FastAPI
   |
   +-- Jinja2 页面渲染
   +-- 内嵌 Bootstrap / CSS / JavaScript
   +-- Session 与 CSRF 处理
   +-- 用户与个人主页路由
   +-- 帖子与可见性路由
   +-- 评论树路由
   +-- LCS 搜索路由
   +-- 媒体流式传输路由
   |
   v
SQLAlchemy ORM
   |
   v
SQLite
   |
   v
social.db
```

数据库连接已启用 SQLite 外键约束：

```sql
PRAGMA foreign_keys=ON;
```

常规持久化操作使用 SQLAlchemy ORM。直接 SQL 操作使用参数化查询。

---

# 🗄️ 数据库设计

SpaceBox 将持久化应用状态存储在一个 SQLite 数据库文件中：

```text
social.db
```

核心架构包含五张表：

1. `users`
2. `posts`
3. `media`
4. `comments`
5. `follows`

## 🔗 关系概览

```text
users
  |
  | 1 ---- N
  v
posts
  | \
  |  \ 1 ---- N
  |   +----------> media
  |
  +---- 1 ---- N ---> comments
                       |
                       +---- parent_id ---> comments.id

users  N <---- follows ----> N  users
```

实际含义如下：

- 一个用户可以创建多篇帖子。
- 一个用户可以创建多条评论。
- 一篇帖子可以包含多个媒体附件。
- 一篇帖子可以包含多条评论。
- 一条评论可以将另一条评论引用为其父评论。
- 用户通过 `follows` 关联表与其他用户建立关注关系。

---

## 👤 `users` 表

存储账户与个人资料信息。

| 列 | 类型 | 约束 / 默认值 | 用途 |
| --- | --- | --- | --- |
| `id` | INTEGER | 主键 | 内部用户标识符 |
| `username` | VARCHAR(32) | UNIQUE、INDEX | 用于个人主页 URL 的唯一账户名称 |
| `display_name` | VARCHAR(64) | 必填 | 便于阅读的个人资料名称 |
| `password_hash` | VARCHAR(255) | 必填 | 存储的登录凭据摘要 |
| `bio` | TEXT | 默认空字符串 | 个人简介 |
| `profile_visibility` | VARCHAR(16) | 默认 `public` | 个人主页可见性字段 |
| `default_post_visibility` | VARCHAR(16) | 默认 `public` | 新帖子的默认可见性 |
| `created_at` | DATETIME | 自动填充 | 账户创建时间 |

用户名长度限制为 3–32 个字符，可使用字母、数字和下划线。

显示名称最多 64 个字符，个人简介最多 500 个字符。

---

## 📝 `posts` 表

存储帖子文本、归属、可见性和发布时间信息。

| 列 | 类型 | 约束 / 默认值 | 用途 |
| --- | --- | --- | --- |
| `id` | INTEGER | 主键 | 帖子标识符 |
| `author_id` | INTEGER | FK → `users.id`、INDEX | 帖子作者 |
| `content` | TEXT | 默认空字符串 | 保留空白格式的帖子正文 |
| `visibility` | VARCHAR(16) | 默认 `public`、INDEX | `public`、`followers` 或 `private` |
| `created_at` | DATETIME | INDEX | 帖子创建时间 |
| `updated_at` | DATETIME | 自动更新 | 最近修改时间 |
| `scheduled_at` | DATETIME | NULL、INDEX | 请求的定时发布时间 |
| `published_at` | DATETIME | NULL、INDEX | 实际发布时间 |

`author_id` 引用 `users.id`，并使用 `ON DELETE CASCADE`。

---

## 🖼️ `media` 表

存储图片和视频附件。

| 列 | 类型 | 约束 / 默认值 | 用途 |
| --- | --- | --- | --- |
| `id` | INTEGER | 主键 | 媒体标识符 |
| `post_id` | INTEGER | FK → `posts.id`、INDEX | 所属帖子 |
| `media_type` | VARCHAR(16) | 必填 | `image` 或 `video` |
| `original_name` | VARCHAR(255) | 默认空字符串 | 原始上传文件名 |
| `mime_type` | VARCHAR(128) | 默认 `application/octet-stream` | HTTP 内容类型 |
| `byte_size` | INTEGER | 默认 `0` | 二进制大小（字节） |
| `content_data` | BLOB | 可为 NULL | 图片或视频二进制数据 |
| `file_path` | VARCHAR(255) | 默认空字符串 | 预留路径字段；当前上传使用 BLOB 存储 |

实际文件内容存储在：

```text
media.content_data
```

当浏览器请求 `/media/{media_id}` 时，SpaceBox 会：

1. 加载媒体记录。
2. 通过 `post_id` 找到其所属帖子。
3. 应用帖子的可见性规则。
4. 仅在允许访问时返回二进制数据。

`post_id` 引用 `posts.id`，并使用 `ON DELETE CASCADE`。

---

## 💬 `comments` 表

存储评论以及完整的嵌套讨论树。

| 列 | 类型 | 约束 / 默认值 | 用途 |
| --- | --- | --- | --- |
| `id` | INTEGER | 主键 | 评论标识符 |
| `post_id` | INTEGER | FK → `posts.id`、INDEX | 评论所属帖子 |
| `author_id` | INTEGER | FK → `users.id`、INDEX | 评论作者 |
| `parent_id` | INTEGER | FK → `comments.id`、NULL、INDEX | 父评论；根评论为 `NULL` |
| `content` | TEXT | 必填 | 评论正文 |
| `created_at` | DATETIME | INDEX | 评论创建时间 |
| `is_deleted` | BOOLEAN | 默认 `false`、INDEX | 软删除状态 |
| `deleted_at` | DATETIME | NULL | 软删除时间戳 |

根级评论具有：

```text
parent_id = NULL
```

回复具有：

```text
parent_id = <父评论 id>
```

评论正文最多 1,000 个字符。

用户触发的常规删除操作**不会**执行 SQL `DELETE`。该行会继续保留，使其 `id` 仍可作为后代回复的父节点。

外键：

- `post_id` → `posts.id`，使用 `ON DELETE CASCADE`
- `author_id` → `users.id`，使用 `ON DELETE CASCADE`
- `parent_id` → `comments.id`，使用 `ON DELETE CASCADE`

自引用级联仅在数据库中的该行真正被删除时生效。应用正常执行的软删除操作不会触发它。

---

## 🤝 `follows` 表

表示用户之间多对多的关注关系。

| 列 | 类型 | 约束 | 用途 |
| --- | --- | --- | --- |
| `follower_id` | INTEGER | 复合主键、FK → `users.id` | 关注其他账户的用户 |
| `followed_id` | INTEGER | 复合主键、FK → `users.id` | 被关注的用户 |

两者共同构成复合主键：

```text
(follower_id, followed_id)
```

这可以防止重复的关注关系。

示例：

```text
follower_id = 12
followed_id = 35
```

表示用户 `12` 关注用户 `35`。

两个外键都使用 `ON DELETE CASCADE`。

---

## 🔐 外键汇总

```text
posts.author_id       -> users.id
media.post_id         -> posts.id
comments.post_id      -> posts.id
comments.author_id    -> users.id
comments.parent_id    -> comments.id
follows.follower_id   -> users.id
follows.followed_id   -> users.id
```

由于已启用 `PRAGMA foreign_keys=ON`，这些关系是实际生效的 SQLite 约束，而不仅仅是文档中的关系说明。

---

## 🛠️ 数据库初始化

全新安装无需单独的数据库服务器、迁移服务或手动架构脚本。

启动时，如果所需数据表尚不存在，SQLAlchemy 会创建这些表。

数据库文件位于：

```text
<SpaceBox 目录>/social.db
```

这一个文件会存储：

- 用户账户与个人资料；
- 帖子与可见性设置；
- 定时发布信息；
- 媒体元数据以及图片/视频 BLOB 内容；
- 评论与评论树关系；
- 评论软删除状态；
- 关注关系。

由于媒体二进制内容直接存储在 SQLite 中，随着用户上传图片和视频，`social.db` 会逐渐增大。对于小型部署而言，这种方式便于备份和迁移，因为应用数据始终集中在一个文件中。

---

## 🕒 时间处理

应用时间戳使用基于 UTC 的日期时间值，涵盖：

- 账户创建；
- 帖子创建；
- 帖子更新；
- 定时发布；
- 实际发布；
- 评论创建；
- 评论删除。

浏览器会将时间戳转换为本地时间后显示。

---

## 📦 单文件运行时

`spacebox_standalone.py` 包含完整的运行时应用：

- 应用常量与内部配置；
- SQLite 与 SQLAlchemy 初始化；
- ORM 模型；
- Session 身份验证；
- CSRF 处理；
- 帖子可见性检查；
- 账户与个人主页路由；
- 帖子路由；
- 媒体路由；
- 评论路由；
- LCS 用户名搜索；
- Jinja2 模板；
- Bootstrap 资源；
- 项目 CSS；
- 项目 JavaScript；
- Uvicorn 启动逻辑。

安装 Python 软件包后，无需前端编译或资源部署步骤。

---

## 📚 依赖

`requirements.txt` 包含：

```text
fastapi>=0.128,<1.0
uvicorn[standard]>=0.30
SQLAlchemy>=2.0,<2.1
Jinja2>=3.1
python-multipart>=0.0.20
itsdangerous>=2.2
tzdata>=2024.1
```

---

## 📄 许可证

SpaceBox 根据 **GNU Affero General Public License v3.0（AGPL-3.0）**发布。

完整许可证文本请参阅 [`LICENSE`](LICENSE)。

内嵌的 Bootstrap 发行版仍受 Bootstrap 自身的 MIT License 声明约束，该声明保留在应用源代码中。

---

<div align="center">

## 🧡 构建技术

⚡ **FastAPI** — 现代 Python Web 框架  
[GitHub: fastapi/fastapi](https://github.com/fastapi/fastapi)

🗄️ **SQLite** — 嵌入式关系型数据库引擎  
[GitHub: sqlite/sqlite](https://github.com/sqlite/sqlite)

🐍 **Python** · 🧱 **SQLAlchemy** · 🎨 **Bootstrap** · 🧩 **Jinja2**

<br>

**SpaceBox — 一个 Python 文件中的简洁社交发布平台。** 🚀

</div>
