#!/usr/bin/env python3
"""SpaceBox 单文件启动器。

本文件有意设计为完全自包含：
- FastAPI 后端、SQLAlchemy 模型、身份验证和路由均位于此处。
- Jinja 模板与项目 CSS/JS 内嵌于此；Bootstrap 通过外部 CDN 加载。
- 应用配置不会从环境变量中读取。
- SQLite 数据库、媒体目录和持久化 Session 密钥会自动管理。
- 大型媒体采用流式文件存储与 HTTP Range 分段传输，避免整文件进入内存。
- 每次启动都会清理长期未登录的空账号，并校验媒体文件索引。

直接运行：``python spacebox_standalone.zh.cdn.py``
"""

# 运行时依赖检查必须先执行，因此第三方模块有意在检查函数调用后导入。
# ruff: noqa: E402

from __future__ import annotations

import errno
import hashlib
import importlib.util
import os
import re
import secrets
import site
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Generator, Iterable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, BinaryIO, TypedDict
from urllib.parse import quote
from zoneinfo import ZoneInfo


def _ensure_runtime_dependencies() -> None:
    """仅在此文件独立运行时安装缺失的运行时依赖。"""
    user_site_packages = site.getusersitepackages()
    if user_site_packages not in sys.path and Path(user_site_packages).is_dir():
        sys.path.insert(0, user_site_packages)

    required_modules = {
        "fastapi": "fastapi>=0.128,<1.0",
        "uvicorn": "uvicorn[standard]>=0.30",
        "sqlalchemy": "SQLAlchemy>=2.0,<2.1",
        "jinja2": "Jinja2>=3.1",
        "multipart": "python-multipart>=0.0.20",
        "itsdangerous": "itsdangerous>=2.2",
        "tzdata": "tzdata>=2024.1",
    }
    missing_packages: list[str] = []
    for module_name, package_name in required_modules.items():
        if importlib.util.find_spec(module_name) is None:
            missing_packages.append(package_name)
    if not missing_packages:
        return

    print("SpaceBox：正在安装缺失的 Python 依赖……")
    subprocess.check_call([sys.executable, "-m", "pip", "install", *missing_packages])
    # 某些受管 Python 会让 pip 回退到用户目录，但当前进程未启用该路径。
    if user_site_packages not in sys.path and Path(user_site_packages).is_dir():
        sys.path.insert(0, user_site_packages)
    importlib.invalidate_caches()


_ensure_runtime_dependencies()

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from jinja2 import DictLoader, Environment, select_autoescape
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    create_engine,
    delete,
    event,
    func,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    joinedload,
    mapped_column,
    relationship,
    selectinload,
    sessionmaker,
)
from sqlalchemy.sql import Select
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

APP_NAME = "SpaceBox"
APP_VERSION = "0.6.2-standalone"
BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "social.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH.as_posix()}"
MEDIA_STORAGE_DIR = BASE_DIR / "media"
SESSION_SECRET_PATH = BASE_DIR / ".spacebox_session_secret"
COOKIE_SECURE = False
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 14
DEFAULT_LOCAL_TIMEZONE = ZoneInfo("Asia/Singapore")
STALE_EMPTY_ACCOUNT_DAYS = 30

MAX_MEDIA_FILES_PER_POST = 6
MAX_IMAGE_FILE_SIZE_BYTES = 256 * 1024 * 1024
MAX_VIDEO_FILE_SIZE_BYTES = 4 * 1024 * 1024 * 1024
MAX_MEDIA_TOTAL_SIZE_BYTES = 8 * 1024 * 1024 * 1024
MEDIA_IO_CHUNK_SIZE_BYTES = 4 * 1024 * 1024
IMAGE_PREVIEW_LIMIT_BYTES = 32 * 1024 * 1024
VIDEO_PREVIEW_LIMIT_BYTES = 256 * 1024 * 1024
HTTP_RANGE_NOT_SATISFIABLE = getattr(
    status,
    "HTTP_416_RANGE_NOT_SATISFIABLE",
    416,
)
MAX_SEARCH_CANDIDATES = 10_000
MAX_SEARCH_RESULTS = 8
MAX_POST_LENGTH = 5_000
MAX_COMMENT_LENGTH = 1_000
MAX_BIO_LENGTH = 500
MAX_DISPLAY_NAME_LENGTH = 64
MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 32

POST_VISIBILITIES = frozenset({"public", "followers", "private"})
VISIBILITY_LABELS = {
    "public": "公开",
    "followers": "仅关注者",
    "private": "私密",
}


@dataclass(frozen=True, slots=True)
class MediaFormat:
    """一个可上传媒体格式及其服务端限制。"""

    media_type: str
    mime_type: str
    max_bytes: int


MEDIA_FORMATS: dict[str, MediaFormat] = {
    ".jpg": MediaFormat("image", "image/jpeg", MAX_IMAGE_FILE_SIZE_BYTES),
    ".jpeg": MediaFormat("image", "image/jpeg", MAX_IMAGE_FILE_SIZE_BYTES),
    ".png": MediaFormat("image", "image/png", MAX_IMAGE_FILE_SIZE_BYTES),
    ".gif": MediaFormat("image", "image/gif", MAX_IMAGE_FILE_SIZE_BYTES),
    ".webp": MediaFormat("image", "image/webp", MAX_IMAGE_FILE_SIZE_BYTES),
    ".mp4": MediaFormat("video", "video/mp4", MAX_VIDEO_FILE_SIZE_BYTES),
    ".webm": MediaFormat("video", "video/webm", MAX_VIDEO_FILE_SIZE_BYTES),
    ".mov": MediaFormat("video", "video/quicktime", MAX_VIDEO_FILE_SIZE_BYTES),
    ".m4v": MediaFormat("video", "video/x-m4v", MAX_VIDEO_FILE_SIZE_BYTES),
}


def format_byte_size(byte_count: int) -> str:
    """以适合界面提示的二进制单位格式化字节数。"""
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(byte_count)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if value >= 10:
                return f"{value:.0f} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{byte_count} B"


# 自有模板、样式和脚本保留为可读源码；第三方前端库通过 CDN 引入。

EMBEDDED_TEMPLATES: dict[str, str] = {
    "base.html": r"""<!doctype html>
<html lang="zh-CN" data-bs-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>{% block title %}SpaceBox{% endblock %}</title>
  <link
    href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css"
    rel="stylesheet"
    integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB"
    crossorigin="anonymous"
  >
  <link
    rel="stylesheet"
    href="{{ request.url_for('static', path='style.css') }}?v={{ app_version }}"
  >
</head>
<body class="bg-body-tertiary">
<nav class="navbar navbar-expand-lg bg-body border-bottom sticky-top app-navbar">
  <div class="container app-shell">
    <a class="navbar-brand fw-bold" href="{{ request.url_for('home') }}">
      SpaceBox
    </a>

    <div class="navbar-search mx-lg-4 order-lg-2 flex-grow-1">
      <div class="position-relative" id="user-search-wrap">
        <input
          id="user-search-input"
          class="form-control form-control-sm search-input"
          type="search"
          autocomplete="off"
          spellcheck="false"
          placeholder="搜索用户名，例如 alex_01"
          aria-label="搜索用户"
          aria-controls="user-search-menu"
          aria-expanded="false"
          aria-autocomplete="list"
        >
        <div
          id="user-search-menu"
          class="search-dropdown shadow-lg d-none"
          role="listbox"
        ></div>
      </div>
    </div>

    <button
      class="navbar-toggler order-lg-3"
      type="button"
      data-bs-toggle="collapse"
      data-bs-target="#mainNav"
      aria-label="切换导航"
    >
      <span class="navbar-toggler-icon"></span>
    </button>

    <div class="collapse navbar-collapse order-lg-4 flex-grow-0" id="mainNav">
      <div class="navbar-nav ms-auto align-items-lg-center gap-lg-1">
        {% if current_user %}
          <a class="nav-link" href="{{ request.url_for('new_post_form') }}">
            发帖
          </a>
          <a
            class="nav-link"
            href="{{ request.url_for('profile', username=current_user.username) }}"
          >
            @{{ current_user.username }}
          </a>
          <a class="nav-link" href="{{ request.url_for('settings_form') }}">
            设置
          </a>
          <form
            method="post"
            action="{{ request.url_for('logout') }}"
            class="d-inline"
          >
            <input type="hidden" name="csrf" value="{{ csrf_token }}">
            <button class="btn btn-outline-secondary btn-sm" type="submit">
              退出登录
            </button>
          </form>
        {% else %}
          <a class="nav-link" href="{{ request.url_for('login_form') }}">
            登录
          </a>
          <a
            class="btn btn-primary btn-sm"
            href="{{ request.url_for('register_form') }}"
          >
            注册
          </a>
        {% endif %}
      </div>
    </div>
  </div>
</nav>

<main class="container content-shell py-4">
  {% if flash %}
    <div
      class="alert alert-{{ flash.category }} alert-dismissible fade show shadow-sm"
      role="alert"
    >
      {{ flash.message }}
      <button
        type="button"
        class="btn-close"
        data-bs-dismiss="alert"
        aria-label="关闭"
      ></button>
    </div>
  {% endif %}

  {% block content %}{% endblock %}
</main>

<script
  src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.min.js"
  integrity="sha384-G/EV+4j2dNv+tEPo3++6LCgdCROaejBqfUeNjuKAiuXbjrxilcCdDz6ZAVfHWe1Y"
  crossorigin="anonymous"
></script>
<script src="{{ request.url_for('static', path='app.js') }}?v={{ app_version }}"></script>
{% block scripts %}{% endblock %}
</body>
</html>""",
    "error.html": r"""{% extends "base.html" %}
{% block title %}错误 · SpaceBox{% endblock %}
{% block content %}
<div class="card border-0 shadow-sm">
  <div class="card-body p-5 text-center">
    <div class="display-5 fw-bold mb-2">{{ status_code }}</div>
    <p class="text-secondary mb-4">{{ detail }}</p>
    <a class="btn btn-primary" href="/">返回首页</a>
  </div>
</div>
{% endblock %}""",
    "feed.html": r"""{% extends "base.html" %}
{% block title %}动态 · SpaceBox{% endblock %}
{% block content %}
<div class="d-flex justify-content-between align-items-center mb-4 gap-3">
  <div>
    <h1 class="h4 mb-1">动态</h1>
    <div class="text-secondary small">公开帖子以及你有权查看的内容。时间按浏览器本地时区显示。</div>
  </div>
  {% if current_user %}
    <a class="btn btn-primary flex-shrink-0" href="{{ request.url_for('new_post_form') }}">新建帖子</a>
  {% endif %}
</div>

{% if posts %}
  {% for post in posts %}
    {% include "partials/post_card.html" %}
  {% endfor %}
{% else %}
  <div class="empty-state card border-0 shadow-sm text-center py-5 text-secondary">
    <div class="fs-2 mb-2">🪐</div>
    <p class="mb-0">目前还没有你可以查看的帖子。</p>
  </div>
{% endif %}
{% endblock %}""",
    "login.html": r"""{% extends "base.html" %}
{% block title %}登录 · SpaceBox{% endblock %}
{% block content %}
<div class="auth-card card border-0 shadow-sm mx-auto">
  <div class="card-body p-4 p-md-5">
    <h1 class="h4 mb-4">登录</h1>
    <form method="post" action="{{ request.url_for('login') }}">
      <input type="hidden" name="csrf" value="{{ csrf_token }}">

      <div class="mb-3">
        <label class="form-label">用户名</label>
        <input
          class="form-control"
          name="username"
          required
          autocomplete="username"
        >
      </div>

      <div class="mb-4">
        <label class="form-label">密码</label>
        <input
          class="form-control"
          name="password"
          type="password"
          required
          autocomplete="current-password"
        >
      </div>

      <button class="btn btn-primary w-100" type="submit">登录</button>
    </form>
  </div>
</div>
{% endblock %}""",
    "new_post.html": r"""{% extends "base.html" %}
{% block title %}新建帖子 · SpaceBox{% endblock %}
{% block content %}
<div class="page-heading mb-4">
  <h1 class="h4 mb-1">创建新帖子</h1>
  <div class="text-secondary small">
    空格、制表符、换行和缩进都会被保留。在编辑器中按 Tab 可插入缩进。
  </div>
</div>

<div class="card border-0 shadow-sm composer-card">
  <div class="card-body p-2 p-md-3">
    <form
      method="post"
      action="{{ request.url_for('create_post') }}"
      enctype="multipart/form-data"
      data-schedule-form
      data-large-media-form
    >
      <input type="hidden" name="csrf" value="{{ csrf_token }}">
      <input type="hidden" name="scheduled_at_utc" value="">

      <div class="mb-3">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <label class="form-label mb-0 fw-semibold">
            内容 <span class="fw-normal text-secondary">（可选）</span>
          </label>
          <span id="post-char-count" class="small text-secondary"></span>
        </div>
        <textarea
          class="form-control post-editor"
          name="content"
          rows="9"
          maxlength="5000"
          data-indentable
          data-count-target="post-char-count"
          placeholder="分享点什么……（可选，可只上传图片或视频）&#10;&#10;提示：在这里按 Tab 会插入缩进，而不是移动焦点。"
        ></textarea>
      </div>

      <div class="mb-3">
        <label class="form-label fw-semibold">图片 / 视频</label>
        <input
          id="media-files"
          class="form-control"
          type="file"
          name="files"
          multiple
          accept="image/jpeg,image/png,image/gif,image/webp,video/mp4,video/webm,video/quicktime,video/x-m4v"
        >
        <div class="form-text">
          最多可添加 6 个附件；图片最大 256 MB，视频最大 4.0 GB，总计最大 8.0 GB。文件采用低内存流式保存，
          大视频支持上传进度、拖动播放和断点式分段读取。
        </div>
        <div id="media-preview" class="preview-grid mt-3"></div>
      </div>

      <div class="row g-3 mb-4">
        <div class="col-md-6">
          <label class="form-label fw-semibold">谁可以查看？</label>
          <select class="form-select" name="visibility">
            {% for visibility_key, visibility_label in visibility_labels.items() %}
              <option
                value="{{ visibility_key }}"
                {% if user.default_post_visibility == visibility_key %}selected{% endif %}
              >
                {{ visibility_label }}
              </option>
            {% endfor %}
          </select>
        </div>

        <div class="col-md-6">
          <label class="form-label fw-semibold">
            定时发布
            <span class="fw-normal text-secondary">（可选）</span>
          </label>
          <input class="form-control" type="datetime-local" data-schedule-local>
          <div class="form-text">
            使用浏览器的本地时间。留空则立即发布。
          </div>
        </div>
      </div>

      <div class="d-flex gap-2 justify-content-end">
        <a class="btn btn-outline-secondary" href="{{ request.url_for('home') }}">
          取消
        </a>
        <button class="btn btn-primary px-4" type="submit">
          发布 / 定时发布
        </button>
      </div>
    </form>
  </div>
</div>
{% endblock %}""",
    "partials/comment_tree.html": r"""{% macro render_comments(comment_nodes, post, current_user, csrf_token, depth=0) %}
  {% for comment_node in comment_nodes %}
    {% set comment = comment_node.comment %}
    <article
      class="comment-node"
      id="comment-{{ comment.id }}"
      data-depth="{{ depth }}"
    >
      <div class="comment-main">
        <div class="d-flex gap-2 align-items-start">
          <a
            href="{{ request.url_for('profile', username=comment.author.username) }}"
            class="avatar avatar-sm text-decoration-none flex-shrink-0"
          >
            {{ comment.author.display_name[:1]|upper }}
          </a>

          <div class="flex-grow-1 min-w-0">
            <div class="d-flex justify-content-between gap-2 align-items-start">
              <div class="min-w-0">
                <a
                  href="{{ request.url_for('profile', username=comment.author.username) }}"
                  class="fw-semibold text-decoration-none text-body"
                >
                  {{ comment.author.display_name }}
                </a>
                <span class="text-secondary small">
                  @{{ comment.author.username }}
                </span>

                <div class="small text-secondary">
                  <time
                    data-datetime="{{ comment.created_at.isoformat() }}"
                    data-prefix="评论于："
                    data-relative
                  ></time>
                  {% if comment.is_deleted and comment.deleted_at %}
                    ·
                    <time
                      data-datetime="{{ comment.deleted_at.isoformat() }}"
                      data-prefix="删除于："
                      data-relative
                    ></time>
                  {% endif %}
                </div>
              </div>

              {% if
                current_user
                and current_user.id == comment.author_id
                and not comment.is_deleted
              %}
                <form
                  method="post"
                  action="{{ request.url_for('delete_comment', comment_id=comment.id) }}"
                  onsubmit="return confirm('删除这条评论的文本吗？其下方的回复会被保留。');"
                >
                  <input type="hidden" name="csrf" value="{{ csrf_token }}">
                  <button
                    class="btn btn-link btn-sm text-danger p-0 text-decoration-none"
                    type="submit"
                  >
                    删除
                  </button>
                </form>
              {% endif %}
            </div>

            {% if comment.is_deleted %}
              <div class="deleted-comment my-2">这条评论已被作者删除。</div>
            {% else %}
              <div class="comment-content my-2">{{ comment.content }}</div>
            {% endif %}

            {% if current_user and not comment.is_deleted %}
              <button
                type="button"
                class="btn btn-sm btn-link p-0 text-decoration-none reply-button"
                data-reply-to="{{ comment.id }}"
              >
                回复
              </button>
              <div
                id="reply-box-{{ comment.id }}"
                class="d-none mt-2 reply-box"
              >
                <form
                  method="post"
                  action="{{ request.url_for('create_comment', post_id=post.id) }}"
                >
                  <input type="hidden" name="csrf" value="{{ csrf_token }}">
                  <input
                    type="hidden"
                    name="parent_id"
                    value="{{ comment.id }}"
                  >
                  <textarea
                    class="form-control form-control-sm mb-2"
                    name="content"
                    rows="2"
                    maxlength="1000"
                    required
                    data-indentable
                    placeholder="回复 @{{ comment.author.username }}"
                  ></textarea>
                  <div class="d-flex justify-content-end">
                    <button class="btn btn-primary btn-sm" type="submit">
                      发送回复
                    </button>
                  </div>
                </form>
              </div>
            {% endif %}
          </div>
        </div>
      </div>

      {% if comment_node.children %}
        <div class="comment-children">
          {{ render_comments(
            comment_node.children,
            post,
            current_user,
            csrf_token,
            depth + 1
          ) }}
        </div>
      {% endif %}
    </article>
  {% endfor %}
{% endmacro %}""",
    "partials/post_card.html": r"""<article
  class="card shadow-sm border-0 mb-4 post-card
    {% if not post.published_at %}scheduled-post{% endif %}"
>
  <div class="card-body p-2 p-md-3">
    <div class="d-flex justify-content-between gap-3 mb-3">
      <div class="d-flex gap-3 min-w-0">
        <a
          href="{{ request.url_for('profile', username=post.author.username) }}"
          class="avatar text-decoration-none"
          aria-label="查看 {{ post.author.display_name }} 的个人主页"
        >
          {{ post.author.display_name[:1]|upper }}
        </a>

        <div class="min-w-0">
          <a
            class="fw-semibold text-decoration-none text-body d-inline-block
              text-truncate author-link"
            href="{{ request.url_for('profile', username=post.author.username) }}"
          >
            {{ post.author.display_name }}
          </a>
          <div class="text-secondary small text-truncate">
            @{{ post.author.username }}
          </div>
          <div class="post-time small text-secondary mt-1">
            {% if not post.published_at and post.scheduled_at %}
              <time
                data-datetime="{{ post.scheduled_at.isoformat() }}"
                data-prefix="计划发布："
                data-relative
              ></time>
            {% else %}
              <time
                data-datetime="{{ (post.published_at or post.created_at).isoformat() }}"
                data-prefix="发布于："
                data-relative
              ></time>
            {% endif %}
          </div>
        </div>
      </div>

      <div class="d-flex gap-2 align-items-start flex-wrap justify-content-end">
        {% if not post.published_at %}
          <span class="badge rounded-pill text-bg-warning">已定时</span>
        {% endif %}
        <span class="badge rounded-pill text-bg-light border">
          {{ visibility_labels[post.visibility] }}
        </span>
      </div>
    </div>

    {% if post.content %}
      <div class="post-content mb-3">{{ post.content }}</div>
    {% endif %}

    {% if post.media %}
      <div
        class="media-grid media-count-{{ [post.media|length, 4]|min }} mb-3"
      >
        {% for media_item in post.media %}
          <div class="media-tile">
            {% if media_item.media_type == 'image' %}
              <a
                href="{{ request.url_for('media_content', media_id=media_item.id) }}"
                target="_blank"
                rel="noopener"
              >
                <img
                  src="{{ request.url_for('media_content', media_id=media_item.id) }}"
                  class="media-frame"
                  alt="{{ media_item.original_name }}"
                  loading="lazy"
                >
              </a>
            {% else %}
              <video class="media-frame" controls preload="metadata">
                <source
                  src="{{ request.url_for('media_content', media_id=media_item.id) }}"
                  type="{{ media_item.mime_type }}"
                >
                你的浏览器不支持视频播放。
              </video>
            {% endif %}
          </div>
        {% endfor %}
      </div>
    {% endif %}

    <div class="d-flex align-items-center gap-3 small post-actions pt-2">
      {% if post.published_at %}
        <a
          href="{{ request.url_for('post_detail', post_id=post.id) }}#comments"
          class="text-decoration-none text-secondary action-link"
        >
          评论 {{ post.active_comment_count }}
        </a>
      {% else %}
        <span class="text-secondary">发布后开放评论</span>
      {% endif %}

      <a
        href="{{ request.url_for('post_detail', post_id=post.id) }}"
        class="text-decoration-none text-secondary action-link"
      >
        详情
      </a>

      {% if current_user and current_user.id == post.author_id %}
        <form
          method="post"
          action="{{ request.url_for('delete_post', post_id=post.id) }}"
          class="ms-auto"
          onsubmit="return confirm('删除这篇帖子吗？');"
        >
          <input type="hidden" name="csrf" value="{{ csrf_token }}">
          <button
            type="submit"
            class="btn btn-link btn-sm text-danger text-decoration-none p-0"
          >
            删除
          </button>
        </form>
      {% endif %}
    </div>
  </div>
</article>""",
    "post_detail.html": r"""{% extends "base.html" %}
{% from "partials/comment_tree.html" import render_comments with context %}
{% block title %}帖子 · SpaceBox{% endblock %}
{% block content %}
{% include "partials/post_card.html" %}

{% if not post_is_published %}
  <div class="alert alert-warning border-0 shadow-sm">
    这篇帖子计划在未来发布，目前只有你可以查看。
    到达预定时间后，符合条件的查看者将可以看到它，同时评论也会开放。
  </div>
{% else %}
  <section id="comments" class="comments-section">
    <header
      class="discussion-header d-flex justify-content-between
        align-items-end gap-3 flex-wrap"
    >
      <div>
        <h2 class="h4 mb-1">讨论</h2>
        <p class="text-secondary mb-0">围绕这篇帖子展开交流。</p>
      </div>
      <span class="discussion-count small text-secondary">
        {{ post.active_comment_count }} 条评论
      </span>
    </header>

    {% if current_user %}
      <details class="comment-composer-disclosure">
        <summary class="comment-composer-toggle">
          <span class="comment-toggle-icon" aria-hidden="true"></span>
          <span class="comment-toggle-open-label">写评论</span>
          <span class="comment-toggle-close-label">收起输入框</span>
        </summary>

        <form
          method="post"
          action="{{ request.url_for('create_comment', post_id=post.id) }}"
          class="comment-composer"
        >
          <input type="hidden" name="csrf" value="{{ csrf_token }}">
          <textarea
            class="form-control comment-input mb-3"
            name="content"
            rows="4"
            maxlength="1000"
            required
            data-indentable
            placeholder="写下你的想法……（Tab 可插入缩进）"
          ></textarea>
          <div class="d-flex justify-content-end">
            <button class="btn btn-primary px-4" type="submit">
              发表评论
            </button>
          </div>
        </form>
      </details>
    {% else %}
      <p class="discussion-login-prompt text-secondary mb-0">
        登录后即可参与讨论。
      </p>
    {% endif %}

    {% if comment_tree %}
      <div class="comment-tree">
        {{ render_comments(comment_tree, post, current_user, csrf_token) }}
      </div>
    {% else %}
      <div class="discussion-empty text-secondary">
        还没有评论。来开启讨论吧。
      </div>
    {% endif %}
  </section>
{% endif %}
{% endblock %}""",
    "profile.html": r"""{% extends "base.html" %}
{% block title %}{{ profile_user.display_name }} · SpaceBox{% endblock %}
{% block content %}
<div class="card border-0 shadow-sm mb-4 profile-card">
  <div class="card-body p-2 p-md-3">
    <div class="d-flex justify-content-between gap-3 align-items-start flex-wrap">
      <div class="d-flex gap-3 min-w-0">
        <div class="avatar avatar-xl">
          {{ profile_user.display_name[:1]|upper }}
        </div>

        <div class="min-w-0">
          <h1 class="h4 mb-1">{{ profile_user.display_name }}</h1>
          <div class="text-secondary mb-2">@{{ profile_user.username }}</div>
          <div class="profile-bio mb-3">
            {{ profile_user.bio or '该用户尚未添加个人简介。' }}
          </div>

          <div class="d-flex flex-wrap gap-x-3 gap-y-1 small text-secondary profile-meta">
            <span>
              <strong class="text-body">{{ follower_count }}</strong>
              位关注者
            </span>
            <span>
              <strong class="text-body">{{ following_count }}</strong>
              个正在关注
            </span>
            <span>
              <strong class="text-body">{{ posts|length }}</strong>
              篇可见帖子
            </span>
          </div>

          <div class="small text-secondary mt-2">
            <time
              data-datetime="{{ profile_user.created_at.isoformat() }}"
              data-prefix="加入 SpaceBox："
            ></time>
          </div>

          <div class="small mt-2">
            <span class="text-secondary">直接链接：</span>
            <a
              class="text-decoration-none"
              href="{{ request.url_for('profile_short', username=profile_user.username) }}"
            >
              /@{{ profile_user.username }}
            </a>
          </div>
        </div>
      </div>

      <div>
        {% if current_user and current_user.id != profile_user.id %}
          {% if is_following %}
            <form
              method="post"
              action="{{ request.url_for('unfollow_user', username=profile_user.username) }}"
            >
              <input type="hidden" name="csrf" value="{{ csrf_token }}">
              <button class="btn btn-outline-secondary" type="submit">
                取消关注
              </button>
            </form>
          {% else %}
            <form
              method="post"
              action="{{ request.url_for('follow_user', username=profile_user.username) }}"
            >
              <input type="hidden" name="csrf" value="{{ csrf_token }}">
              <button class="btn btn-primary" type="submit">关注</button>
            </form>
          {% endif %}
        {% elif current_user and current_user.id == profile_user.id %}
          <a
            class="btn btn-outline-secondary"
            href="{{ request.url_for('settings_form') }}"
          >
            编辑个人资料
          </a>
        {% endif %}
      </div>
    </div>
  </div>
</div>

<div class="d-flex justify-content-between align-items-center mb-3">
  <h2 class="h5 mb-0">帖子</h2>
  <span class="small text-secondary">
    个人主页可公开访问；每篇帖子仍会分别执行自己的可见性规则。
  </span>
</div>

{% if posts %}
  {% for post in posts %}
    {% include "partials/post_card.html" %}
  {% endfor %}
{% else %}
  <div class="empty-state card border-0 shadow-sm text-center text-secondary py-5">
    这个个人主页上目前没有你可以查看的帖子。
  </div>
{% endif %}
{% endblock %}""",
    "register.html": r"""{% extends "base.html" %}
{% block title %}注册 · SpaceBox{% endblock %}
{% block content %}
<div class="auth-card card border-0 shadow-sm mx-auto">
  <div class="card-body p-4 p-md-5">
    <h1 class="h4 mb-4">创建账户</h1>
    <form method="post" action="{{ request.url_for('register') }}">
      <input type="hidden" name="csrf" value="{{ csrf_token }}">

      <div class="mb-3">
        <label class="form-label">用户名</label>
        <input
          class="form-control"
          name="username"
          minlength="3"
          maxlength="32"
          required
          autocomplete="username"
          placeholder="例如 alex_01"
        >
        <div class="form-text">长度为 3–32 个字符，只能使用字母、数字和下划线。</div>
      </div>

      <div class="mb-3">
        <label class="form-label">显示名称</label>
        <input
          class="form-control"
          name="display_name"
          maxlength="64"
          required
        >
      </div>

      <div class="mb-4">
        <label class="form-label">密码</label>
        <input
          class="form-control"
          name="password"
          type="password"
          required
          autocomplete="new-password"
        >
      </div>

      <button class="btn btn-primary w-100" type="submit">注册</button>
    </form>
  </div>
</div>
{% endblock %}""",
    "settings.html": r"""{% extends "base.html" %}
{% block title %}设置 · SpaceBox{% endblock %}
{% block content %}
<div class="card border-0 shadow-sm settings-card">
  <div class="card-body p-2 p-md-3">
    <h1 class="h4 mb-1">账户与发布设置</h1>
    <p class="text-secondary small mb-4">
      任何知道你用户名的人都可以打开你的个人主页。每篇帖子仍可独立设置为
      公开 / 仅关注者 / 私密。
    </p>

    <form method="post" action="{{ request.url_for('settings_update') }}">
      <input type="hidden" name="csrf" value="{{ csrf_token }}">

      <div class="mb-3">
        <label class="form-label fw-semibold">用户名</label>
        <input class="form-control" value="@{{ user.username }}" disabled>
        <div class="form-text">
          个人主页：/u/{{ user.username }} 或 /@{{ user.username }}
        </div>
      </div>

      <div class="mb-3">
        <label class="form-label fw-semibold">显示名称</label>
        <input
          class="form-control"
          name="display_name"
          maxlength="64"
          value="{{ user.display_name }}"
          required
        >
      </div>

      <div class="mb-3">
        <label class="form-label fw-semibold">个人简介</label>
        <textarea
          class="form-control"
          name="bio"
          rows="4"
          maxlength="500"
          data-indentable
        >{{ user.bio }}</textarea>
      </div>

      <div class="mb-4">
        <label class="form-label fw-semibold">默认帖子可见性</label>
        <select class="form-select" name="default_post_visibility">
          {% for visibility_key, visibility_label in visibility_labels.items() %}
            <option
              value="{{ visibility_key }}"
              {% if user.default_post_visibility == visibility_key %}selected{% endif %}
            >
              {{ visibility_label }}
            </option>
          {% endfor %}
        </select>
      </div>

      <button class="btn btn-primary" type="submit">保存设置</button>
    </form>
  </div>
</div>
{% endblock %}""",
}


CUSTOM_STYLESHEET = r""":root {
  --spacebox-radius: 1rem;
  --spacebox-accent: #f97316;
  --spacebox-accent-hover: #ea580c;
  --spacebox-accent-active: #c2410c;
  --spacebox-shell-width: 1600px;
  --spacebox-content-width: 1520px;
}

/* 布局 */

body {
  min-height: 100vh;
}

.app-navbar {
  backdrop-filter: saturate(180%) blur(16px);
}

.app-shell {
  max-width: var(--spacebox-shell-width);
}

.content-shell {
  width: 100%;
  max-width: var(--spacebox-content-width);
  padding-top: clamp(1.5rem, 3vw, 3rem) !important;
  padding-bottom: clamp(2rem, 4vw, 4rem) !important;
}

.navbar-brand {
  letter-spacing: -0.04em;
}

.min-w-0 {
  min-width: 0;
}

/* 用户搜索 */

.navbar-search {
  max-width: 520px;
}

.search-input {
  padding-left: 1rem;
  border-radius: 999px;
  background: var(--bs-tertiary-bg);
}

.search-dropdown {
  position: absolute;
  z-index: 1080;
  top: calc(100% + 0.5rem);
  right: 0;
  left: 0;
  max-height: 420px;
  overflow: hidden auto;
  border: 1px solid var(--bs-border-color);
  border-radius: 0.9rem;
  background: var(--bs-body-bg);
}

.search-result {
  display: flex;
  gap: 0.75rem;
  align-items: center;
  padding: 0.7rem 0.8rem;
  color: var(--bs-body-color);
  text-decoration: none;
}

.search-result:hover,
.search-result.active {
  background: var(--bs-tertiary-bg);
}

.search-empty {
  padding: 1rem;
  color: var(--bs-secondary-color);
  font-size: 0.875rem;
}

/* 按钮 */

.btn-primary {
  --bs-btn-color: #fff;
  --bs-btn-bg: var(--spacebox-accent);
  --bs-btn-border-color: var(--spacebox-accent);
  --bs-btn-hover-color: #fff;
  --bs-btn-hover-bg: var(--spacebox-accent-hover);
  --bs-btn-hover-border-color: var(--spacebox-accent-hover);
  --bs-btn-focus-shadow-rgb: 249, 115, 22;
  --bs-btn-active-color: #fff;
  --bs-btn-active-bg: var(--spacebox-accent-active);
  --bs-btn-active-border-color: var(--spacebox-accent-active);
}

.btn-outline-secondary {
  --bs-btn-color: var(--spacebox-accent);
  --bs-btn-border-color: var(--spacebox-accent);
  --bs-btn-hover-color: #fff;
  --bs-btn-hover-bg: var(--spacebox-accent);
  --bs-btn-hover-border-color: var(--spacebox-accent);
  --bs-btn-focus-shadow-rgb: 249, 115, 22;
  --bs-btn-active-color: #fff;
  --bs-btn-active-bg: var(--spacebox-accent-active);
  --bs-btn-active-border-color: var(--spacebox-accent-active);
}

.btn-link {
  --bs-btn-color: var(--spacebox-accent);
  --bs-btn-hover-color: var(--spacebox-accent-hover);
  --bs-btn-active-color: var(--spacebox-accent-active);
}

.btn.btn-link {
  color: var(--spacebox-accent) !important;
}

.btn.btn-link:hover {
  color: var(--spacebox-accent-hover) !important;
}

.navbar-toggler {
  border-color: color-mix(in srgb, var(--spacebox-accent) 55%, transparent);
}

.navbar-toggler:focus {
  box-shadow: 0 0 0 0.2rem color-mix(in srgb, var(--spacebox-accent) 24%, transparent);
}

/* 通用卡片与身份信息 */

.post-card,
.auth-card,
.composer-card,
.profile-card,
.settings-card,
.empty-state {
  border-radius: var(--spacebox-radius);
}

.post-card,
.composer-card,
.profile-card,
.settings-card {
  width: 100%;
  max-width: none;
}

.post-card > .card-body,
.composer-card > .card-body,
.profile-card > .card-body,
.settings-card > .card-body {
  padding: clamp(1rem, 2vw, 2rem) !important;
}

.auth-card {
  max-width: 520px;
}

.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 2.75rem;
  height: 2.75rem;
  border: 1px solid var(--bs-primary-border-subtle);
  border-radius: 50%;
  color: var(--bs-primary-text-emphasis);
  background: var(--bs-primary-bg-subtle);
  font-weight: 700;
}

.avatar-sm {
  width: 2.25rem;
  height: 2.25rem;
  font-size: 0.85rem;
}

.avatar-xl {
  width: 4.5rem;
  height: 4.5rem;
  font-size: 1.4rem;
}

.author-link {
  max-width: 420px;
}

/* 文本内容与编辑器 */

.post-content,
.comment-content,
.profile-bio {
  overflow-wrap: anywhere;
  white-space: pre-wrap;
  word-break: break-word;
  tab-size: 4;
  -moz-tab-size: 4;
}

.post-content {
  font-size: clamp(1rem, 0.95rem + 0.2vw, 1.125rem);
  line-height: 1.8;
}

.post-editor,
.comment-composer textarea,
.reply-box textarea,
.settings-card textarea {
  line-height: 1.6;
  tab-size: 4;
  -moz-tab-size: 4;
}

.post-editor {
  min-height: 13rem;
  resize: vertical;
  font-family: inherit;
}

.scheduled-post {
  border: 1px dashed var(--bs-warning-border-subtle) !important;
}

/* 帖子媒体 */

.media-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 0.5rem;
  overflow: hidden;
  border-radius: 0.85rem;
}

.media-grid.media-count-2,
.media-grid.media-count-3,
.media-grid.media-count-4 {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.media-grid.media-count-3 .media-tile:first-child {
  grid-row: span 2;
}

.media-tile {
  min-width: 0;
  overflow: hidden;
  border-radius: 0.7rem;
  background: #111;
}

.media-frame {
  display: block;
  width: 100%;
  height: 100%;
  min-height: 220px;
  max-height: 680px;
  object-fit: cover;
  background: #111;
}

.post-actions {
  border-top: 1px solid var(--bs-border-color-translucent);
}

.action-link:hover {
  color: var(--spacebox-accent-hover) !important;
}

.preview-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 0.75rem;
}

.preview-item {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--bs-border-color);
  border-radius: 0.75rem;
  background: var(--bs-body-bg);
}

.preview-media {
  display: block;
  width: 100%;
  height: 120px;
  object-fit: cover;
  background: #111;
}

/* 评论树 */

.comments-section {
  width: 100%;
  padding: clamp(1.5rem, 3vw, 2.75rem) clamp(0.25rem, 1vw, 1rem) 0;
  border-top: 1px solid var(--bs-border-color-translucent);
  scroll-margin-top: 6rem;
}

.discussion-header {
  padding-bottom: clamp(1rem, 2vw, 1.5rem);
}

.discussion-count {
  padding: 0.4rem 0.75rem;
  border-radius: 999px;
  background: var(--bs-tertiary-bg);
  white-space: nowrap;
}

.comment-composer-disclosure {
  padding-bottom: clamp(1.25rem, 2.5vw, 2rem);
  border-bottom: 1px solid var(--bs-border-color-translucent);
}

.comment-composer-toggle {
  display: inline-flex;
  gap: 0.55rem;
  align-items: center;
  min-height: 2.5rem;
  padding: 0.5rem 1rem;
  border: 1px solid var(--spacebox-accent);
  border-radius: 999px;
  color: var(--spacebox-accent);
  background: transparent;
  font-weight: 600;
  cursor: pointer;
  list-style: none;
  user-select: none;
}

.comment-composer-toggle::-webkit-details-marker {
  display: none;
}

.comment-composer-toggle::marker {
  content: "";
}

.comment-composer-toggle:hover {
  color: #fff;
  background: var(--spacebox-accent);
}

.comment-composer-toggle:focus-visible {
  outline: 0;
  box-shadow: 0 0 0 0.2rem
    color-mix(in srgb, var(--spacebox-accent) 22%, transparent);
}

.comment-toggle-icon::before {
  content: "+";
}

.comment-toggle-close-label {
  display: none;
}

.comment-composer-disclosure[open] .comment-composer-toggle {
  margin-bottom: clamp(1rem, 2vw, 1.5rem);
}

.comment-composer-disclosure[open] .comment-toggle-icon::before {
  content: "−";
}

.comment-composer-disclosure[open] .comment-toggle-open-label {
  display: none;
}

.comment-composer-disclosure[open] .comment-toggle-close-label {
  display: inline;
}

.comment-composer {
  margin: 0;
  padding: 0;
  background: transparent;
}

.comment-input {
  min-height: 9rem;
  padding: 1rem 1.1rem;
  resize: vertical;
  border-radius: 1rem;
  background: var(--bs-body-bg);
}

.comment-input:focus,
.reply-box textarea:focus {
  border-color: color-mix(in srgb, var(--spacebox-accent) 65%, transparent);
  box-shadow: 0 0 0 0.2rem
    color-mix(in srgb, var(--spacebox-accent) 16%, transparent);
}

.discussion-login-prompt {
  padding: 1.5rem 0;
  border-bottom: 1px solid var(--bs-border-color-translucent);
}

.discussion-empty {
  padding: clamp(2rem, 5vw, 4rem) 0;
  text-align: center;
}

.comment-tree {
  --thread-indent: 1.5rem;
}

.comment-node {
  position: relative;
  margin: 0;
}

.comment-tree > .comment-node {
  padding: clamp(1.25rem, 2.5vw, 2rem) 0;
}

.comment-tree > .comment-node + .comment-node {
  border-top: 1px solid var(--bs-border-color-translucent);
}

.comment-main {
  position: relative;
  padding: 0;
}

.comment-main > .d-flex {
  gap: 0.9rem !important;
}

.comment-content {
  margin-top: 0.7rem !important;
  margin-bottom: 0.55rem !important;
  font-size: 1.025rem;
  line-height: 1.75;
}

.comment-children {
  position: relative;
  margin-top: 0.65rem;
  margin-left: var(--thread-indent);
  padding-left: 1.4rem;
  border-left: 1px solid
    color-mix(in srgb, var(--spacebox-accent) 30%, var(--bs-border-color));
}

.comment-children::before {
  position: absolute;
  top: 1.2rem;
  left: -1px;
  width: 1rem;
  border-top: 1px solid
    color-mix(in srgb, var(--spacebox-accent) 30%, var(--bs-border-color));
  content: "";
}

.comment-children > .comment-node {
  padding-top: 0.85rem;
}

.comment-children > .comment-node + .comment-node {
  margin-top: 0.75rem;
}

.reply-box {
  width: 100%;
  padding-top: 0.4rem;
}

.reply-box textarea {
  min-height: 5.75rem;
  padding: 0.75rem 0.9rem;
  resize: vertical;
  border-radius: 0.8rem;
}

.deleted-comment {
  display: inline-block;
  padding: 0.35rem 0.65rem;
  border-radius: 0.55rem;
  color: var(--bs-secondary-color);
  background: var(--bs-tertiary-bg);
  font-size: 0.9rem;
  font-style: italic;
}

.reply-button {
  font-size: 0.82rem;
}

/* 个人主页元数据辅助样式 */

.profile-meta {
  column-gap: 1rem;
  row-gap: 0.25rem;
}

.gap-x-3 {
  column-gap: 1rem;
}

.gap-y-1 {
  row-gap: 0.25rem;
}

/* 响应式行为 */

@media (max-width: 991.98px) {
  .navbar-search {
    order: 5 !important;
    width: 100%;
    max-width: none;
    margin-top: 0.65rem;
    margin-bottom: 0.2rem;
  }
}

@media (max-width: 575.98px) {
  .content-shell {
    padding-right: 0.75rem;
    padding-left: 0.75rem;
    padding-top: 1.25rem !important;
    padding-bottom: 2rem !important;
  }

  .media-grid.media-count-2,
  .media-grid.media-count-3,
  .media-grid.media-count-4 {
    grid-template-columns: 1fr;
  }

  .media-grid.media-count-3 .media-tile:first-child {
    grid-row: auto;
  }

  .media-frame {
    min-height: 180px;
  }

  .comment-tree {
    --thread-indent: 0.55rem;
  }

  .comments-section {
    padding: 1.25rem 0 0;
  }

  .comment-composer {
    padding: 0;
  }

  .comment-composer-disclosure {
    padding-bottom: 1.25rem;
  }

  .comment-composer-toggle {
    justify-content: center;
    width: 100%;
  }

  .comment-input {
    min-height: 7.5rem;
  }

  .comment-tree > .comment-node {
    padding: 1.15rem 0;
  }

  .comment-main > .d-flex {
    gap: 0.65rem !important;
  }

  .comment-content {
    font-size: 1rem;
  }

  .comment-children {
    padding-left: 0.8rem;
  }

  .avatar-xl {
    width: 3.75rem;
    height: 3.75rem;
  }
}"""


APPLICATION_JAVASCRIPT_TEMPLATE = r"""(() => {
  "use strict";

  const USER_SEARCH_DEBOUNCE_MS = 180;
  const RELATIVE_TIME_MAX_DAYS = 7;
  const IMAGE_PREVIEW_LIMIT_BYTES = __IMAGE_PREVIEW_LIMIT_BYTES__;
  const VIDEO_PREVIEW_LIMIT_BYTES = __VIDEO_PREVIEW_LIMIT_BYTES__;

  function findElement(selector, root = document) {
    return root.querySelector(selector);
  }

  function findElements(selector, root = document) {
    return root.querySelectorAll(selector);
  }

  function createElement(tagName, className, textContent = "") {
    const element = document.createElement(tagName);
    element.className = className;
    if (textContent) {
      element.textContent = textContent;
    }
    return element;
  }

  function formatByteSize(byteCount) {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = byteCount;
    let unitIndex = 0;

    while (value >= 1024 && unitIndex < units.length - 1) {
      value /= 1024;
      unitIndex += 1;
    }

    const decimalPlaces = value >= 10 || unitIndex === 0 ? 0 : 1;
    return `${value.toFixed(decimalPlaces)} ${units[unitIndex]}`;
  }

  function setupReplyControls() {
    for (const replyButton of findElements("[data-reply-to]")) {
      replyButton.addEventListener("click", function toggleReplyBox() {
        const replyBox = document.getElementById(
          `reply-box-${replyButton.dataset.replyTo}`,
        );
        if (!replyBox) {
          return;
        }

        replyBox.classList.toggle("d-none");
        if (!replyBox.classList.contains("d-none")) {
          findElement("textarea", replyBox)?.focus();
        }
      });
    }
  }

  function setupIndentableTextareas() {
    for (const textarea of findElements("textarea[data-indentable]")) {
      textarea.addEventListener("keydown", function insertIndent(event) {
        if (event.key !== "Tab") {
          return;
        }

        event.preventDefault();
        const selectionStart = textarea.selectionStart;
        const selectionEnd = textarea.selectionEnd;
        textarea.setRangeText("\t", selectionStart, selectionEnd, "end");
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
      });
    }
  }

  function setupCharacterCounters() {
    for (const input of findElements("[data-count-target]")) {
      const counter = document.getElementById(input.dataset.countTarget);
      if (!counter) {
        continue;
      }

      function updateCharacterCount() {
        const maximumLength = input.maxLength > 0 ? input.maxLength : "∞";
        counter.textContent = `${input.value.length}/${maximumLength}`;
      }

      input.addEventListener("input", updateCharacterCount);
      updateCharacterCount();
    }
  }

  function setupLocalizedTimestamps() {
    const relativeTimeFormatter = new Intl.RelativeTimeFormat("zh-CN", {
      numeric: "auto",
    });
    const fullDateTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });

    function formatRelativeTime(date) {
      const secondsFromNow = Math.round((date.getTime() - Date.now()) / 1000);
      const absoluteSeconds = Math.abs(secondsFromNow);
      const minuteSeconds = 60;
      const hourSeconds = minuteSeconds * 60;
      const daySeconds = hourSeconds * 24;

      if (absoluteSeconds < minuteSeconds) {
        return relativeTimeFormatter.format(secondsFromNow, "second");
      }
      if (absoluteSeconds < hourSeconds) {
        return relativeTimeFormatter.format(
          Math.round(secondsFromNow / minuteSeconds),
          "minute",
        );
      }
      if (absoluteSeconds < daySeconds) {
        return relativeTimeFormatter.format(
          Math.round(secondsFromNow / hourSeconds),
          "hour",
        );
      }
      if (absoluteSeconds < daySeconds * RELATIVE_TIME_MAX_DAYS) {
        return relativeTimeFormatter.format(
          Math.round(secondsFromNow / daySeconds),
          "day",
        );
      }
      return null;
    }

    for (const timeElement of findElements("time[data-datetime]")) {
      const date = new Date(timeElement.dataset.datetime);
      if (Number.isNaN(date.getTime())) {
        continue;
      }

      const fullDateTime = fullDateTimeFormatter.format(date);
      const relativeTime = timeElement.hasAttribute("data-relative")
        ? formatRelativeTime(date)
        : null;
      const prefix = timeElement.dataset.prefix || "";

      timeElement.textContent = `${prefix}${relativeTime || fullDateTime}`;
      timeElement.title = `${fullDateTime} （浏览器本地时间）`;
      timeElement.dateTime = date.toISOString();
    }
  }

  function setupScheduledPostForms() {
    function padTwoDigits(value) {
      return String(value).padStart(2, "0");
    }

    for (const scheduleForm of findElements("form[data-schedule-form]")) {
      const localDateTimeInput = findElement(
        "[data-schedule-local]",
        scheduleForm,
      );
      const utcDateTimeInput = findElement(
        '[name="scheduled_at_utc"]',
        scheduleForm,
      );
      if (!localDateTimeInput || !utcDateTimeInput) {
        continue;
      }

      const currentDate = new Date();
      localDateTimeInput.min =
        `${currentDate.getFullYear()}-` +
        `${padTwoDigits(currentDate.getMonth() + 1)}-` +
        `${padTwoDigits(currentDate.getDate())}T` +
        `${padTwoDigits(currentDate.getHours())}:` +
        padTwoDigits(currentDate.getMinutes());

      scheduleForm.addEventListener("submit", function storeUtcSchedule() {
        if (!localDateTimeInput.value) {
          utcDateTimeInput.value = "";
          return;
        }

        const localDateTime = new Date(localDateTimeInput.value);
        utcDateTimeInput.value = Number.isNaN(localDateTime.getTime())
          ? ""
          : localDateTime.toISOString();
      });
    }
  }

  function createUploadStatus(uploadForm) {
    const uploadStatusContainer = createElement("div", "d-none mt-3");
    uploadStatusContainer.setAttribute("role", "status");
    uploadStatusContainer.setAttribute("aria-live", "polite");

    const uploadStatusText = createElement("div", "small text-secondary mb-1");
    const uploadProgress = createElement("progress", "w-100");
    uploadProgress.max = 100;
    uploadProgress.value = 0;

    uploadStatusContainer.append(uploadStatusText, uploadProgress);
    uploadForm.append(uploadStatusContainer);
    return {
      container: uploadStatusContainer,
      text: uploadStatusText,
      progress: uploadProgress,
    };
  }

  function setUploadState(uploadForm, isUploading) {
    for (const submitButton of findElements(
      'button[type="submit"]',
      uploadForm,
    )) {
      submitButton.disabled = isUploading;
    }
    uploadForm.dataset.uploading = isUploading ? "true" : "false";
  }

  function setupMediaUpload() {
    const mediaInput = document.getElementById("media-files");
    const mediaPreviewContainer = document.getElementById("media-preview");
    if (!mediaInput || !mediaPreviewContainer) {
      return;
    }

    const activePreviewUrls = new Set();

    function releasePreviewUrls() {
      // Object URL 必须主动释放，反复选择大文件时才不会持续占用内存。
      for (const objectUrl of activePreviewUrls) {
        URL.revokeObjectURL(objectUrl);
      }
      activePreviewUrls.clear();
    }

    function createMediaPreview(file) {
      const previewCard = createElement("div", "preview-item");
      const isImage = file.type.startsWith("image/");
      const previewLimitBytes = isImage
        ? IMAGE_PREVIEW_LIMIT_BYTES
        : VIDEO_PREVIEW_LIMIT_BYTES;

      if (file.size <= previewLimitBytes) {
        const objectUrl = URL.createObjectURL(file);
        activePreviewUrls.add(objectUrl);
        const mediaElement = document.createElement(isImage ? "img" : "video");
        mediaElement.src = objectUrl;
        mediaElement.className = "preview-media";
        if (isImage) {
          mediaElement.decoding = "async";
        } else {
          mediaElement.controls = true;
          mediaElement.preload = "metadata";
        }
        previewCard.append(mediaElement);
      } else {
        const largeFileMessage = createElement(
          "div",
          "preview-media d-flex align-items-center justify-content-center " +
            "p-3 text-center text-secondary",
          "大型文件已选择；为节省内存，发布前不生成预览。",
        );
        previewCard.append(largeFileMessage);
      }

      const caption = createElement(
        "div",
        "small text-secondary text-truncate p-2",
        `${file.name} · ${formatByteSize(file.size)}`,
      );
      previewCard.append(caption);
      return previewCard;
    }

    function renderSelectedMedia() {
      releasePreviewUrls();
      mediaPreviewContainer.replaceChildren();
      for (const file of mediaInput.files) {
        mediaPreviewContainer.append(createMediaPreview(file));
      }
    }

    mediaInput.addEventListener("change", renderSelectedMedia);
    window.addEventListener("pagehide", releasePreviewUrls, { once: true });

    const uploadForm = mediaInput.closest("form[data-large-media-form]");
    if (!uploadForm) {
      return;
    }

    const uploadStatus = createUploadStatus(uploadForm);

    uploadForm.addEventListener("submit", function submitMediaForm(event) {
      if (uploadForm.dataset.uploading === "true") {
        event.preventDefault();
        return;
      }
      if (mediaInput.files.length === 0) {
        return;
      }

      // 普通表单无法报告上传进度，因此含媒体时改用 XHR 提交同一 FormData。
      event.preventDefault();
      setUploadState(uploadForm, true);
      uploadStatus.container.classList.remove("d-none");
      uploadStatus.progress.value = 0;
      uploadStatus.text.textContent = "正在准备上传……";

      const uploadRequest = new XMLHttpRequest();
      uploadRequest.open(
        (uploadForm.method || "POST").toUpperCase(),
        uploadForm.action,
      );
      uploadRequest.upload.addEventListener(
        "progress",
        function updateUploadProgress(progressEvent) {
          if (!progressEvent.lengthComputable) {
            uploadStatus.progress.removeAttribute("value");
            uploadStatus.text.textContent = "正在上传大型媒体……";
            return;
          }

          const percentage = Math.min(
            100,
            Math.round((progressEvent.loaded / progressEvent.total) * 100),
          );
          uploadStatus.progress.value = percentage;
          uploadStatus.text.textContent =
            `正在上传：${formatByteSize(progressEvent.loaded)} / ` +
            `${formatByteSize(progressEvent.total)}（${percentage}%）`;
        },
      );
      uploadRequest.upload.addEventListener("load", function finishUpload() {
        uploadStatus.progress.value = 100;
        uploadStatus.text.textContent = "上传完成，正在校验并保存……";
      });
      uploadRequest.addEventListener("load", function handleUploadResponse() {
        if (uploadRequest.status >= 200 && uploadRequest.status < 400) {
          window.location.assign(uploadRequest.responseURL || "/");
          return;
        }

        setUploadState(uploadForm, false);
        let serverDetail = "";
        try {
          const responseData = JSON.parse(uploadRequest.responseText || "{}");
          if (typeof responseData.detail === "string") {
            serverDetail = responseData.detail;
          } else if (Array.isArray(responseData.detail)) {
            serverDetail = responseData.detail
              .map((item) => item && item.msg)
              .filter(Boolean)
              .join("；");
          }
        } catch (_error) {
          // 非 JSON 错误响应保留通用提示。
        }
        uploadStatus.text.textContent = serverDetail
          ? `上传失败（HTTP ${uploadRequest.status}）：${serverDetail}`
          : `上传失败（HTTP ${uploadRequest.status}），请重试。`;
      });
      uploadRequest.addEventListener("error", function handleUploadError() {
        setUploadState(uploadForm, false);
        uploadStatus.text.textContent = "网络连接中断，上传未完成，请重试。";
      });
      uploadRequest.addEventListener("abort", function handleUploadAbort() {
        setUploadState(uploadForm, false);
        uploadStatus.text.textContent = "上传已取消。";
      });
      uploadRequest.send(new FormData(uploadForm));
    });
  }

  function setupUserSearch() {
    const searchWrapper = document.getElementById("user-search-wrap");
    const searchInput = document.getElementById("user-search-input");
    const searchResultsMenu = document.getElementById("user-search-menu");
    if (!searchWrapper || !searchInput || !searchResultsMenu) {
      return;
    }

    let searchTimerId = null;
    let activeResultIndex = -1;
    let activeSearchController = null;

    function closeSearchResults() {
      searchResultsMenu.classList.add("d-none");
      searchInput.setAttribute("aria-expanded", "false");
      activeResultIndex = -1;
    }

    function activateSearchResult(requestedIndex) {
      const resultLinks = findElements(".search-result", searchResultsMenu);
      for (const resultLink of resultLinks) {
        resultLink.classList.remove("active");
        resultLink.setAttribute("aria-selected", "false");
      }
      if (resultLinks.length === 0) {
        return;
      }

      activeResultIndex =
        (requestedIndex + resultLinks.length) % resultLinks.length;
      const activeResult = resultLinks[activeResultIndex];
      activeResult.classList.add("active");
      activeResult.setAttribute("aria-selected", "true");
      activeResult.scrollIntoView({ block: "nearest" });
    }

    function renderUserSearchResults(results) {
      searchResultsMenu.replaceChildren();
      activeResultIndex = -1;

      if (results.length === 0) {
        searchResultsMenu.append(
          createElement("div", "search-empty", "未找到相似的用户名"),
        );
      } else {
        for (const [resultIndex, user] of results.entries()) {
          const profileLink = createElement("a", "search-result");
          profileLink.href = user.profileUrl;
          profileLink.dataset.index = String(resultIndex);
          profileLink.setAttribute("role", "option");
          profileLink.setAttribute("aria-selected", "false");

          const avatar = createElement(
            "span",
            "avatar avatar-sm",
            (user.displayName || user.username).slice(0, 1).toUpperCase(),
          );
          const userText = document.createElement("span");
          const displayName = createElement("strong", "", user.displayName);
          const username = createElement(
            "small",
            "d-block text-secondary",
            `@${user.username}`,
          );
          userText.append(displayName, username);
          profileLink.append(avatar, userText);
          searchResultsMenu.append(profileLink);
        }
      }

      searchResultsMenu.classList.remove("d-none");
      searchInput.setAttribute("aria-expanded", "true");
    }

    searchInput.addEventListener("input", function scheduleUserSearch() {
      window.clearTimeout(searchTimerId);
      activeSearchController?.abort();

      const searchTerm = searchInput.value.trim();
      if (!searchTerm) {
        closeSearchResults();
        return;
      }

      searchTimerId = window.setTimeout(async function loadUserSearchResults() {
        activeSearchController = new AbortController();
        try {
          const response = await fetch(
            `/api/users/search?q=${encodeURIComponent(searchTerm)}`,
            {
              headers: { Accept: "application/json" },
              signal: activeSearchController.signal,
            },
          );
          if (!response.ok) {
            closeSearchResults();
            return;
          }

          const payload = await response.json();
          // 只渲染当前输入对应的响应，避免慢请求覆盖较新的搜索结果。
          if (searchInput.value.trim() === searchTerm) {
            const results = Array.isArray(payload.results)
              ? payload.results
              : [];
            renderUserSearchResults(results);
          }
        } catch (error) {
          if (error.name !== "AbortError") {
            closeSearchResults();
          }
        }
      }, USER_SEARCH_DEBOUNCE_MS);
    });

    searchInput.addEventListener(
      "keydown",
      function navigateSearchResults(event) {
        const resultLinks = findElements(".search-result", searchResultsMenu);

        if (event.key === "ArrowDown" && resultLinks.length > 0) {
          event.preventDefault();
          activateSearchResult(activeResultIndex + 1);
        } else if (event.key === "ArrowUp" && resultLinks.length > 0) {
          event.preventDefault();
          activateSearchResult(activeResultIndex - 1);
        } else if (event.key === "Enter" && resultLinks.length > 0) {
          event.preventDefault();
          const selectedIndex = activeResultIndex >= 0 ? activeResultIndex : 0;
          const selectedResult = resultLinks[selectedIndex];
          if (selectedResult) {
            window.location.href = selectedResult.href;
          }
        } else if (event.key === "Escape") {
          closeSearchResults();
        }
      },
    );

    document.addEventListener("click", function closeSearchFromOutside(event) {
      if (!searchWrapper.contains(event.target)) {
        closeSearchResults();
      }
    });
  }

  setupReplyControls();
  setupIndentableTextareas();
  setupCharacterCounters();
  setupLocalizedTimestamps();
  setupScheduledPostForms();
  setupMediaUpload();
  setupUserSearch();
})();"""


_EMBEDDED_ASSET_TYPES = {
    "style.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}
_EMBEDDED_ASSET_CACHE: dict[str, bytes] = {}


def build_application_javascript() -> str:
    """将服务端媒体限制注入可读的前端脚本。"""
    javascript = APPLICATION_JAVASCRIPT_TEMPLATE.replace(
        "__IMAGE_PREVIEW_LIMIT_BYTES__",
        str(IMAGE_PREVIEW_LIMIT_BYTES),
    )
    return javascript.replace(
        "__VIDEO_PREVIEW_LIMIT_BYTES__",
        str(VIDEO_PREVIEW_LIMIT_BYTES),
    )


def load_embedded_asset(asset_name: str) -> bytes:
    """按需编码 SpaceBox 自有前端资源。"""
    cached_asset = _EMBEDDED_ASSET_CACHE.get(asset_name)
    if cached_asset is not None:
        return cached_asset

    if asset_name == "style.css":
        asset_bytes = CUSTOM_STYLESHEET.encode("utf-8")
    elif asset_name == "app.js":
        asset_bytes = build_application_javascript().encode("utf-8")
    else:
        raise KeyError(asset_name)

    _EMBEDDED_ASSET_CACHE[asset_name] = asset_bytes
    return asset_bytes


def load_or_create_session_secret(secret_path: Path) -> str:
    """原子创建或读取持久化 Session 密钥。"""
    try:
        existing_secret = secret_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing_secret = ""
    if len(existing_secret) >= 32:
        return existing_secret

    new_secret = secrets.token_urlsafe(48)
    try:
        descriptor = os.open(
            secret_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        concurrent_secret = secret_path.read_text(encoding="utf-8").strip()
        if len(concurrent_secret) < 32:
            raise RuntimeError("Session 密钥文件无效，请删除后重新启动。") from exc
        return concurrent_secret

    with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
        secret_file.write(new_secret)
    return new_secret


APP_SECRET = load_or_create_session_secret(SESSION_SECRET_PATH)


database_engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


@event.listens_for(database_engine, "connect")
def configure_sqlite_connection(
    dbapi_connection: Any, _connection_record: object
) -> None:
    """为每个新连接启用外键并设置合理的并发等待时间。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


database_session_factory = sessionmaker(
    bind=database_engine,
    autoflush=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """所有 SQLAlchemy ORM 模型的基类。"""


def get_database_session() -> Generator[Session, None, None]:
    """为每个请求提供一个 SQLAlchemy Session，并确保始终将其关闭。"""
    session = database_session_factory()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def commit_database_session(session: Session) -> None:
    """提交当前事务，并在提交失败时恢复 Session 的可用状态。"""
    try:
        session.commit()
    except BaseException:
        # 提交失败后必须显式回滚，否则该 Session 不能继续安全使用。
        session.rollback()
        raise


def utc_now() -> datetime:
    """返回当前带时区信息的 UTC 日期时间。"""
    return datetime.now(timezone.utc)


follow_table = Table(
    "follows",
    Base.metadata,
    Column(
        "follower_id",
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "followed_id",
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class User(Base):
    """已注册的用户账户与个人资料。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(255))
    bio: Mapped[str] = mapped_column(Text, default="")
    default_post_visibility: Mapped[str] = mapped_column(String(16), default="public")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )
    last_login_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    posts: Mapped[list[Post]] = relationship(
        back_populates="author",
        cascade="all, delete-orphan",
    )
    comments: Mapped[list[Comment]] = relationship(
        back_populates="author",
        cascade="all, delete-orphan",
    )
    following: Mapped[list[User]] = relationship(
        "User",
        secondary=follow_table,
        primaryjoin=id == follow_table.c.follower_id,
        secondaryjoin=id == follow_table.c.followed_id,
        back_populates="followers",
        lazy="selectin",
    )
    followers: Mapped[list[User]] = relationship(
        "User",
        secondary=follow_table,
        primaryjoin=id == follow_table.c.followed_id,
        secondaryjoin=id == follow_table.c.follower_id,
        back_populates="following",
        lazy="selectin",
    )


class Post(Base):
    """用户创建的帖子，可选择定时发布并附加媒体。"""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    content: Mapped[str] = mapped_column(Text, default="")
    visibility: Mapped[str] = mapped_column(String(16), default="public", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    author: Mapped[User] = relationship(back_populates="posts")
    media: Mapped[list[Media]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        order_by="Media.id",
    )
    comments: Mapped[list[Comment]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        order_by="Comment.created_at",
    )

    @property
    def active_comment_count(self) -> int:
        """返回未被软删除的评论数量。"""
        active_count = 0
        for comment in self.comments:
            if not comment.is_deleted:
                active_count += 1
        return active_count


class Media(Base):
    """存储在独立媒体目录中的图片或视频附件索引。"""

    __tablename__ = "media"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"),
        index=True,
    )
    media_type: Mapped[str] = mapped_column(String(16))
    original_name: Mapped[str] = mapped_column(String(255), default="")
    mime_type: Mapped[str] = mapped_column(
        String(128),
        default="application/octet-stream",
    )
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    # 路径始终相对于 BASE_DIR；随机文件名避免用户输入参与路径解析。
    file_path: Mapped[str] = mapped_column(
        String(512),
        unique=True,
        index=True,
    )

    post: Mapped[Post] = relationship(back_populates="media")


class Comment(Base):
    """支持软删除的树状帖子评论。"""

    __tablename__ = "comments"
    __mapper_args__ = {"confirm_deleted_rows": False}

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"),
        index=True,
    )
    author_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("comments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    post: Mapped[Post] = relationship(back_populates="comments")
    author: Mapped[User] = relationship(back_populates="comments")


class FlashMessage(TypedDict):
    """存储在签名 Session Cookie 中的 Flash 消息。"""

    message: str
    category: str


def hash_password(password: str) -> str:
    """返回密码直接计算所得、未加盐的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, encoded_password: str) -> bool:
    """根据密码直接计算的 SHA-256 十六进制摘要验证密码。"""
    return secrets.compare_digest(hash_password(password), encoded_password.lower())


def get_authenticated_user(request: Request, session: Session) -> User | None:
    """如果存在，则返回请求对应的已登录用户。"""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return session.get(User, int(user_id))


def require_authenticated_user(request: Request, session: Session) -> User:
    """返回已登录用户，否则抛出 HTTP 401。"""
    current_user = get_authenticated_user(request, session)
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请先登录",
        )
    return current_user


def get_or_create_csrf_token(request: Request) -> str:
    """获取或创建存储在签名 Session 中的 CSRF 令牌。"""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def validate_csrf_token(request: Request, submitted_token: str) -> None:
    """使用恒定时间比较验证提交的 CSRF 令牌。"""
    expected_token = request.session.get("csrf_token", "")
    if (
        not expected_token
        or not submitted_token
        or not secrets.compare_digest(expected_token, submitted_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF 验证失败",
        )


def set_flash_message(request: Request, message: str, category: str = "info") -> None:
    """在 Session 中存储一条一次性 Flash 消息。"""
    request.session["flash"] = {"message": message, "category": category}


def consume_flash_message(request: Request) -> FlashMessage | None:
    """返回并移除当前的一次性 Flash 消息。"""
    return request.session.pop("flash", None)


class TemplateRenderer:
    """封装内嵌 Jinja 模板的加载与 HTML 响应渲染。"""

    def __init__(self, template_map: dict[str, str]) -> None:
        self.environment = Environment(
            loader=DictLoader(template_map),
            autoescape=select_autoescape(("html", "xml")),
            enable_async=False,
        )
        # 资源 URL 带版本参数，升级后浏览器会立即获取新的 CSS 与 JavaScript。
        self.environment.globals["app_version"] = APP_VERSION

    def render_response(
        self,
        *,
        request: Request,
        name: str,
        context: dict[str, Any],
        status_code: int = 200,
    ) -> HTMLResponse:
        render_context = {"request": request, **context}
        rendered_html = self.environment.get_template(name).render(**render_context)
        return HTMLResponse(content=rendered_html, status_code=status_code)


@asynccontextmanager
async def application_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """在接收请求前完成数据库与媒体索引维护。"""
    maintenance_report = initialize_application_state()
    print(maintenance_report.summary())
    yield


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    lifespan=application_lifespan,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=APP_SECRET,
    same_site="lax",
    https_only=COOKIE_SECURE,
    max_age=SESSION_MAX_AGE_SECONDS,
)
template_renderer = TemplateRenderer(EMBEDDED_TEMPLATES)


@app.get("/static/{path:path}", name="static", include_in_schema=False)
def serve_embedded_asset(path: str) -> Response:
    """提供直接内嵌在此 Python 文件中的 SpaceBox 自有前端资源。"""
    try:
        content = load_embedded_asset(path)
        media_type = _EMBEDDED_ASSET_TYPES[path]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="未找到静态资源") from exc

    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@dataclass(frozen=True, slots=True)
class StoredMedia:
    """已验证并原子写入媒体目录的附件元数据。"""

    media_type: str
    original_name: str
    mime_type: str
    byte_size: int
    file_path: str


@dataclass(frozen=True, slots=True)
class ByteRange:
    """闭区间形式的单个 HTTP 字节范围。"""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True, slots=True)
class MediaSource:
    """已定位并可分块读取的文件系统媒体源。"""

    byte_size: int
    etag: str
    file_path: Path


@dataclass(frozen=True, slots=True)
class MediaReconciliationReport:
    """一次媒体文件与数据库索引对账的结果。"""

    invalid_records_removed: int = 0
    metadata_records_updated: int = 0
    orphan_files_removed: int = 0
    partial_files_removed: int = 0


def sanitize_upload_filename(filename: str) -> str:
    """移除路径与控制字符，只保留用于展示的安全文件名。"""
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    safe_characters: list[str] = []
    for character in basename:
        if character >= " " and character != "\x7f":
            safe_characters.append(character)
    cleaned_name = "".join(safe_characters).strip()
    return (cleaned_name or "media")[:255]


def parse_http_byte_range(range_header: str, total_bytes: int) -> ByteRange:
    """解析单个 RFC 7233 字节范围；无效或多范围请求抛出 ValueError。"""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if match is None or total_bytes <= 0:
        raise ValueError("无效的 Range 请求")

    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ValueError("空 Range 请求")

    if start_text:
        range_start = int(start_text)
        if end_text:
            range_end = int(end_text)
        else:
            range_end = total_bytes - 1
        if range_start >= total_bytes or range_end < range_start:
            raise ValueError("Range 超出媒体大小")
        range_end = min(range_end, total_bytes - 1)
    else:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("Range 后缀长度无效")
        range_start = max(0, total_bytes - suffix_length)
        range_end = total_bytes - 1

    return ByteRange(range_start, range_end)


def _path_depth(path: Path) -> int:
    """返回路径层级，用于从最深目录开始清理空目录。"""
    return len(path.parts)


class MediaStorage:
    """封装大型媒体的校验、原子落盘、流式读取和清理。"""

    def __init__(
        self,
        media_root: Path,
        application_root: Path,
        chunk_size_bytes: int = MEDIA_IO_CHUNK_SIZE_BYTES,
    ) -> None:
        self.media_root = media_root.resolve()
        self.application_root = application_root.resolve()
        self.chunk_size_bytes = chunk_size_bytes
        self.media_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def classify_upload(upload: UploadFile) -> tuple[str, str, MediaFormat]:
        original_name = sanitize_upload_filename(upload.filename or "")
        extension = Path(original_name).suffix.lower()
        media_format = MEDIA_FORMATS.get(extension)
        if media_format is None:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=(
                    f"不支持的文件类型：{original_name}。"
                    "仅允许 JPG/PNG/GIF/WebP 图片和 MP4/WebM/MOV/M4V 视频。"
                ),
            )

        claimed_mime_type = (upload.content_type or "").partition(";")[0].lower()
        if (
            claimed_mime_type
            and claimed_mime_type != "application/octet-stream"
            and not claimed_mime_type.startswith(f"{media_format.media_type}/")
        ):
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"文件 {original_name} 的扩展名与媒体类型不一致。",
            )
        return original_name, extension, media_format

    @staticmethod
    def matches_file_signature(extension: str, header: bytes) -> bool:
        """检查常见媒体魔数，避免仅依赖客户端 MIME 声明。"""
        if extension in {".jpg", ".jpeg"}:
            return header.startswith(b"\xff\xd8\xff")
        if extension == ".png":
            return header.startswith(b"\x89PNG\r\n\x1a\n")
        if extension == ".gif":
            return header.startswith((b"GIF87a", b"GIF89a"))
        if extension == ".webp":
            return (
                len(header) >= 12
                and header.startswith(b"RIFF")
                and header[8:12] == b"WEBP"
            )
        if extension == ".webm":
            return header.startswith(b"\x1aE\xdf\xa3")
        if extension in {".mp4", ".mov", ".m4v"}:
            return b"ftyp" in header[4:512]
        return False

    def allocate_destination_path(self, extension: str) -> Path:
        current_time = utc_now()
        destination_directory = (
            self.media_root / f"{current_time.year:04d}" / f"{current_time.month:02d}"
        )
        destination_directory.mkdir(parents=True, exist_ok=True)
        return destination_directory / f"{secrets.token_hex(20)}{extension}"

    def store_upload_stream(
        self,
        source_file: BinaryIO,
        original_name: str,
        extension: str,
        media_format: MediaFormat,
    ) -> StoredMedia:
        """同步分块复制一个上传流；由线程池调用以免阻塞事件循环。"""
        destination_path = self.allocate_destination_path(extension)
        temporary_path = destination_path.with_name(
            f".{destination_path.name}.{secrets.token_hex(8)}.part"
        )
        total_bytes = 0

        try:
            source_file.seek(0)
            first_chunk = source_file.read(self.chunk_size_bytes)
            if not first_chunk:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"文件 {original_name} 为空。",
                )
            if not self.matches_file_signature(extension, first_chunk):
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail=f"文件 {original_name} 的内容与扩展名不匹配。",
                )

            with temporary_path.open("xb") as destination_file:
                chunk = first_chunk
                while chunk:
                    total_bytes += len(chunk)
                    if total_bytes > media_format.max_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail=(
                                f"文件 {original_name} 超过 "
                                f"{format_byte_size(media_format.max_bytes)}。"
                            ),
                        )
                    destination_file.write(chunk)
                    chunk = source_file.read(self.chunk_size_bytes)

            # 原子替换确保媒体索引永远不会指向只写入一部分的目标文件。
            os.replace(temporary_path, destination_path)
        except OSError as exc:
            temporary_path.unlink(missing_ok=True)
            if exc.errno in {errno.ENOSPC, errno.EDQUOT, errno.EFBIG}:
                raise HTTPException(
                    status_code=507,
                    detail="磁盘空间不足，无法保存该媒体文件。",
                ) from exc
            raise
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

        relative_path = destination_path.relative_to(self.application_root).as_posix()
        return StoredMedia(
            media_type=media_format.media_type,
            original_name=original_name,
            mime_type=media_format.mime_type,
            byte_size=total_bytes,
            file_path=relative_path,
        )

    async def store_uploads(self, uploads: list[UploadFile]) -> list[StoredMedia]:
        """顺序保存一批上传，并在任一项失败时回滚已落盘文件。"""
        saved_media: list[StoredMedia] = []
        try:
            declared_total_bytes = 0
            for upload in uploads:
                declared_size = getattr(upload, "size", None)
                if isinstance(declared_size, int):
                    declared_total_bytes += declared_size

            if declared_total_bytes > MAX_MEDIA_TOTAL_SIZE_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=(
                        "单篇帖子的附件总大小不得超过 "
                        f"{format_byte_size(MAX_MEDIA_TOTAL_SIZE_BYTES)}。"
                    ),
                )

            actual_total_bytes = 0
            for upload in uploads:
                original_name, extension, media_format = self.classify_upload(upload)
                declared_size = getattr(upload, "size", None)
                if (
                    isinstance(declared_size, int)
                    and declared_size > media_format.max_bytes
                ):
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=(
                            f"文件 {original_name} 超过 "
                            f"{format_byte_size(media_format.max_bytes)}。"
                        ),
                    )

                stored_media = await run_in_threadpool(
                    self.store_upload_stream,
                    upload.file,
                    original_name,
                    extension,
                    media_format,
                )
                saved_media.append(stored_media)
                actual_total_bytes += stored_media.byte_size
                if actual_total_bytes > MAX_MEDIA_TOTAL_SIZE_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=(
                            "单篇帖子的附件总大小不得超过 "
                            f"{format_byte_size(MAX_MEDIA_TOTAL_SIZE_BYTES)}。"
                        ),
                    )
            return saved_media
        except BaseException:
            saved_paths: list[str] = []
            for media_item in saved_media:
                saved_paths.append(media_item.file_path)
            self.delete_indexed_files(saved_paths)
            raise
        finally:
            for upload in uploads:
                await upload.close()

    def resolve_indexed_path(self, stored_path: str) -> Path | None:
        """安全解析媒体索引，只接受媒体根目录内的普通路径。"""
        if not stored_path:
            return None
        candidate_path = (self.application_root / stored_path.lstrip("/")).resolve()
        try:
            # relative_to 同时充当越界检查，拒绝目录穿越与根目录外索引。
            candidate_path.relative_to(self.media_root)
        except ValueError:
            return None
        return candidate_path

    def delete_indexed_files(self, stored_paths: Iterable[str]) -> None:
        """尽力删除媒体文件；数据库事务不因清理失败而回滚。"""
        for stored_path in stored_paths:
            resolved_path = self.resolve_indexed_path(stored_path)
            if resolved_path is None:
                continue
            try:
                resolved_path.unlink(missing_ok=True)
            except OSError:
                continue

    def reconcile_index(self, session: Session) -> MediaReconciliationReport:
        """在启动时对账媒体索引、文件元数据和媒体目录。"""
        referenced_paths: set[Path] = set()
        invalid_records_removed = 0
        metadata_records_updated = 0

        for media_record in session.scalars(select(Media)).all():
            resolved_path = self.resolve_indexed_path(media_record.file_path)
            media_format = None
            if resolved_path is not None:
                media_format = MEDIA_FORMATS.get(resolved_path.suffix.lower())
            try:
                if (
                    resolved_path is None
                    or media_format is None
                    or not resolved_path.is_file()
                ):
                    raise OSError("媒体索引指向无效文件")
                file_stat = resolved_path.stat()
                with resolved_path.open("rb") as media_file:
                    signature = media_file.read(512)
                if file_stat.st_size <= 0 or not self.matches_file_signature(
                    resolved_path.suffix.lower(),
                    signature,
                ):
                    raise OSError("媒体内容签名无效")
            except OSError:
                session.delete(media_record)
                invalid_records_removed += 1
                continue

            if resolved_path in referenced_paths:
                session.delete(media_record)
                invalid_records_removed += 1
                continue

            canonical_path = resolved_path.relative_to(self.application_root).as_posix()
            expected_values = (
                canonical_path,
                file_stat.st_size,
                media_format.media_type,
                media_format.mime_type,
            )
            current_values = (
                media_record.file_path,
                media_record.byte_size,
                media_record.media_type,
                media_record.mime_type,
            )
            if current_values != expected_values:
                (
                    media_record.file_path,
                    media_record.byte_size,
                    media_record.media_type,
                    media_record.mime_type,
                ) = expected_values
                metadata_records_updated += 1
            referenced_paths.add(resolved_path)

        commit_database_session(session)

        orphan_files_removed = 0
        partial_files_removed = 0
        for candidate_path in self.media_root.rglob("*"):
            if not candidate_path.is_file() and not candidate_path.is_symlink():
                continue
            is_partial_file = candidate_path.name.endswith(".part")
            try:
                resolved_candidate = candidate_path.resolve()
            except (OSError, RuntimeError):
                resolved_candidate = None
            is_orphan_file = (
                candidate_path.is_symlink()
                or resolved_candidate not in referenced_paths
            )
            if not is_partial_file and not is_orphan_file:
                continue
            try:
                candidate_path.unlink(missing_ok=True)
            except OSError:
                continue
            if is_partial_file:
                partial_files_removed += 1
            else:
                orphan_files_removed += 1

        directories: list[Path] = []
        for candidate_path in self.media_root.rglob("*"):
            if candidate_path.is_dir() and not candidate_path.is_symlink():
                directories.append(candidate_path)
        directories.sort(key=_path_depth, reverse=True)
        for directory in directories:
            try:
                directory.rmdir()
            except OSError:
                continue

        return MediaReconciliationReport(
            invalid_records_removed=invalid_records_removed,
            metadata_records_updated=metadata_records_updated,
            orphan_files_removed=orphan_files_removed,
            partial_files_removed=partial_files_removed,
        )

    def load_media_source(self, media_record: Media) -> MediaSource:
        """根据数据库索引定位一个可流式读取的媒体文件。"""
        resolved_path = self.resolve_indexed_path(media_record.file_path)
        if resolved_path is None or not resolved_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="未找到媒体数据",
            )
        file_stat = resolved_path.stat()
        return MediaSource(
            byte_size=file_stat.st_size,
            etag=f'"file-{file_stat.st_mtime_ns:x}-{file_stat.st_size:x}"',
            file_path=resolved_path,
        )

    def iter_file_range(
        self,
        source: MediaSource,
        range_start: int,
        byte_count: int,
    ) -> Iterator[bytes]:
        """仅以固定大小缓冲区读取指定范围。"""
        remaining_bytes = byte_count
        with source.file_path.open("rb") as media_file:
            media_file.seek(range_start)
            while remaining_bytes > 0:
                chunk = media_file.read(min(self.chunk_size_bytes, remaining_bytes))
                if not chunk:
                    break
                remaining_bytes -= len(chunk)
                yield chunk

    def build_media_response(
        self,
        request: Request,
        media_record: Media,
        source: MediaSource,
    ) -> Response:
        """构建支持缓存、HEAD 和单 Range 的低内存媒体响应。"""
        common_headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=300",
            "Content-Type": media_record.mime_type or "application/octet-stream",
            "Content-Disposition": (
                f"inline; filename*=UTF-8''{quote(media_record.original_name, safe='')}"
            ),
            "ETag": source.etag,
            "X-Content-Type-Options": "nosniff",
        }
        if_none_match = request.headers.get("if-none-match", "")
        etag_candidates: set[str] = set()
        for candidate in if_none_match.split(","):
            etag_candidates.add(candidate.strip())
        if source.etag in etag_candidates:
            return Response(
                status_code=status.HTTP_304_NOT_MODIFIED,
                headers=common_headers,
            )

        requested_range: ByteRange | None = None
        range_header = request.headers.get("range")
        if range_header and request.headers.get("if-range", source.etag) == source.etag:
            try:
                # 视频拖动播放依赖 Range；只解析一个范围以保持响应可预测。
                requested_range = parse_http_byte_range(
                    range_header,
                    source.byte_size,
                )
            except ValueError:
                return Response(
                    status_code=HTTP_RANGE_NOT_SATISFIABLE,
                    headers={
                        **common_headers,
                        "Content-Range": f"bytes */{source.byte_size}",
                    },
                )

        if requested_range is None:
            range_start = 0
            content_length = source.byte_size
            response_status = status.HTTP_200_OK
        else:
            range_start = requested_range.start
            content_length = requested_range.length
            response_status = status.HTTP_206_PARTIAL_CONTENT
            common_headers["Content-Range"] = (
                f"bytes {requested_range.start}-{requested_range.end}/"
                f"{source.byte_size}"
            )
        common_headers["Content-Length"] = str(content_length)

        if request.method == "HEAD":
            return Response(status_code=response_status, headers=common_headers)
        return StreamingResponse(
            self.iter_file_range(source, range_start, content_length),
            status_code=response_status,
            media_type=media_record.mime_type or "application/octet-stream",
            headers=common_headers,
        )


media_storage_service = MediaStorage(MEDIA_STORAGE_DIR, BASE_DIR)


class CommentTreeNode(TypedDict):
    """评论树节点的递归模板表示。"""

    comment: Comment
    children: list[CommentTreeNode]


@dataclass(frozen=True, slots=True)
class StartupMaintenanceReport:
    """数据库初始化、空账号清理与媒体对账汇总。"""

    stale_empty_accounts_removed: int
    media: MediaReconciliationReport

    def summary(self) -> str:
        return (
            "启动维护完成："
            f"清理空账号 {self.stale_empty_accounts_removed} 个；"
            f"移除无效媒体索引 {self.media.invalid_records_removed} 个；"
            f"修正媒体索引 {self.media.metadata_records_updated} 个；"
            f"清理孤立文件 {self.media.orphan_files_removed} 个；"
            f"清理中断上传文件 {self.media.partial_files_removed} 个。"
        )


def cleanup_stale_empty_accounts(
    session: Session,
    current_time: datetime | None = None,
) -> int:
    """删除 30 天未登录且完全没有资料或互动的空账号。"""
    cutoff_time = (current_time or utc_now()) - timedelta(days=STALE_EMPTY_ACCOUNT_DAYS)
    # “空账号”必须同时没有简介、内容和关注关系，避免误删仍有数据的用户。
    has_post = select(Post.id).where(Post.author_id == User.id).exists()
    has_comment = select(Comment.id).where(Comment.author_id == User.id).exists()
    follows_someone = (
        select(follow_table.c.followed_id)
        .where(follow_table.c.follower_id == User.id)
        .exists()
    )
    is_followed = (
        select(follow_table.c.follower_id)
        .where(follow_table.c.followed_id == User.id)
        .exists()
    )
    stale_account_ids = session.scalars(
        select(User.id).where(
            User.last_login_at <= cutoff_time,
            func.trim(User.bio) == "",
            ~has_post,
            ~has_comment,
            ~follows_someone,
            ~is_followed,
        )
    ).all()
    if not stale_account_ids:
        return 0

    session.execute(delete(User).where(User.id.in_(stale_account_ids)))
    commit_database_session(session)
    return len(stale_account_ids)


def initialize_application_state() -> StartupMaintenanceReport:
    """创建全新数据库结构，并执行每次启动所需的安全维护。"""
    Base.metadata.create_all(bind=database_engine)
    with database_session_factory() as session:
        removed_accounts = cleanup_stale_empty_accounts(session)
    with database_session_factory() as session:
        media_report = media_storage_service.reconcile_index(session)
    return StartupMaintenanceReport(
        stale_empty_accounts_removed=removed_accounts,
        media=media_report,
    )


def build_template_context(
    request: Request,
    session: Session,
    **extra_context: Any,
) -> dict[str, Any]:
    """构建页面响应共享的通用 Jinja 上下文。"""
    return {
        "current_user": get_authenticated_user(request, session),
        "csrf_token": get_or_create_csrf_token(request),
        "flash": consume_flash_message(request),
        "visibility_labels": VISIBILITY_LABELS,
        **extra_context,
    }


def redirect_to(
    url: str,
    status_code: int = status.HTTP_303_SEE_OTHER,
) -> RedirectResponse:
    """返回应用标准的 POST/重定向/GET 响应。"""
    return RedirectResponse(url=url, status_code=status_code)


def normalize_line_endings(value: str) -> str:
    """规范化换行符，同时保留缩进和首尾空格。"""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def to_utc_datetime(value: datetime | None) -> datetime | None:
    """返回规范化为 UTC 的日期时间，同时保留 ``None``。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_scheduled_datetime(value: str | None) -> datetime | None:
    """解析浏览器提供的定时发布时间戳，并将其规范化为 UTC。"""
    if not value:
        return None

    normalized_value = value.strip()
    if not normalized_value:
        return None

    try:
        scheduled_datetime = datetime.fromisoformat(
            normalized_value.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的定时发布时间",
        ) from exc

    if scheduled_datetime.tzinfo is None:
        scheduled_datetime = scheduled_datetime.replace(tzinfo=DEFAULT_LOCAL_TIMEZONE)
    return scheduled_datetime.astimezone(timezone.utc)


def publish_due_posts(session: Session) -> None:
    """定时帖子到达预定时间后，将其标记为已发布。"""
    current_time = utc_now()
    due_posts = session.scalars(
        select(Post).where(
            Post.published_at.is_(None),
            Post.scheduled_at.is_not(None),
            Post.scheduled_at <= current_time,
        )
    ).all()
    if not due_posts:
        return

    for post in due_posts:
        post.published_at = to_utc_datetime(post.scheduled_at) or current_time
    commit_database_session(session)


def is_post_published(post: Post) -> bool:
    """返回帖子当前是否应视为已发布。"""
    if post.published_at is not None:
        return True

    scheduled_datetime = to_utc_datetime(post.scheduled_at)
    return bool(scheduled_datetime and scheduled_datetime <= utc_now())


def get_followed_user_ids(
    session: Session,
    current_user: User | None,
) -> set[int]:
    """返回当前用户所关注账户的 ID。"""
    if current_user is None:
        return set()

    return set(
        session.scalars(
            select(follow_table.c.followed_id).where(
                follow_table.c.follower_id == current_user.id
            )
        ).all()
    )


def can_view_post(
    viewer: User | None,
    post: Post,
    followed_user_ids: set[int] | None = None,
) -> bool:
    """评估一篇帖子的定时发布规则和可见性规则。"""
    if not is_post_published(post):
        return bool(viewer and viewer.id == post.author_id)

    if viewer and viewer.id == post.author_id:
        return True
    if post.visibility == "public":
        return True
    if post.visibility == "private" or viewer is None:
        return False

    followed_user_ids = followed_user_ids or set()
    return post.author_id in followed_user_ids


def build_post_select() -> Select[tuple[Post]]:
    """构建通用帖子查询，并预加载轻量媒体索引。"""
    return select(Post).options(
        joinedload(Post.author),
        selectinload(Post.media),
        selectinload(Post.comments).joinedload(Comment.author),
    )


def comment_sort_key(comment: Comment) -> tuple[datetime, int]:
    """返回稳定的评论时间排序键。"""
    created_at = to_utc_datetime(comment.created_at) or utc_now()
    return created_at, comment.id


def build_comment_tree(comments: list[Comment]) -> list[CommentTreeNode]:
    """构建 O(n) 评论树，同时保留已软删除的父评论。"""
    ordered_comments = sorted(comments, key=comment_sort_key)
    nodes_by_id: dict[int, CommentTreeNode] = {}
    for comment in ordered_comments:
        nodes_by_id[comment.id] = {
            "comment": comment,
            "children": [],
        }

    root_nodes: list[CommentTreeNode] = []

    for comment in ordered_comments:
        current_node = nodes_by_id[comment.id]
        parent_node = None
        if comment.parent_id is not None:
            parent_node = nodes_by_id.get(comment.parent_id)
        if parent_node is not None:
            parent_node["children"].append(current_node)
        else:
            root_nodes.append(current_node)

    return root_nodes


@dataclass(frozen=True, slots=True)
class PostDraft:
    """完成文本、可见性和发布时间校验后的帖子输入。"""

    content: str
    visibility: str
    scheduled_at: datetime | None
    published_at: datetime | None


def build_post_draft(
    *,
    content: str | None,
    visibility: str,
    scheduled_at_utc: str | None,
    current_user: User,
    upload_count: int,
) -> PostDraft:
    """集中校验发帖表单，路由只负责协调认证、存储与响应。"""
    normalized_content = normalize_line_endings(content or "")
    if not normalized_content.strip() and upload_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="帖子必须包含文字或至少一个附件。",
        )
    if len(normalized_content) > MAX_POST_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="帖子内容不得超过 5,000 个字符。",
        )
    if upload_count > MAX_MEDIA_FILES_PER_POST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"每篇帖子最多可以包含 {MAX_MEDIA_FILES_PER_POST} 个附件。",
        )

    selected_visibility = current_user.default_post_visibility
    if visibility in POST_VISIBILITIES:
        selected_visibility = visibility

    scheduled_datetime = parse_scheduled_datetime(scheduled_at_utc)
    current_time = utc_now()
    if scheduled_datetime and scheduled_datetime <= current_time:
        scheduled_datetime = None

    published_at = current_time
    if scheduled_datetime is not None:
        published_at = None

    return PostDraft(
        content=normalized_content,
        visibility=selected_visibility,
        scheduled_at=scheduled_datetime,
        published_at=published_at,
    )


def persist_post(
    session: Session,
    author_id: int,
    draft: PostDraft,
    stored_media: list[StoredMedia],
) -> Post:
    """在一个数据库事务中持久化帖子与附件元数据。"""
    new_post = Post(
        author_id=author_id,
        content=draft.content,
        visibility=draft.visibility,
        scheduled_at=draft.scheduled_at,
        published_at=draft.published_at,
    )
    try:
        session.add(new_post)
        session.flush()
        media_records: list[Media] = []
        for media_item in stored_media:
            media_record = Media(
                post_id=new_post.id,
                media_type=media_item.media_type,
                original_name=media_item.original_name,
                mime_type=media_item.mime_type,
                byte_size=media_item.byte_size,
                file_path=media_item.file_path,
            )
            media_records.append(media_record)
        session.add_all(media_records)
        commit_database_session(session)
    except BaseException:
        session.rollback()
        raise
    return new_post


def calculate_lcs_length(left: str, right: str) -> int:
    """使用一维动态规划返回最长公共子序列长度。"""
    if len(left) > len(right):
        left, right = right, left

    previous_row = [0] * (len(left) + 1)
    for right_character in right:
        current_row = [0]
        for index, left_character in enumerate(left, start=1):
            if left_character == right_character:
                current_row.append(previous_row[index - 1] + 1)
            else:
                current_row.append(max(previous_row[index], current_row[-1]))
        previous_row = current_row

    return previous_row[-1]


def calculate_username_search_score(
    query: str,
    username: str,
) -> tuple[float, int]:
    """计算基于 LCS 的用户名相似度分数和匹配长度。"""
    normalized_query = query.casefold()
    normalized_username = username.casefold()
    common_subsequence_length = calculate_lcs_length(
        normalized_query,
        normalized_username,
    )
    if common_subsequence_length == 0:
        return 0.0, 0

    # LCS 仍是主要信号。前缀与子字符串匹配仅用于让多个用户名具有相似 LCS 时
    # 排序结果更加符合直觉。
    score = (common_subsequence_length / len(normalized_query)) * 0.75 + (
        common_subsequence_length / len(normalized_username)
    ) * 0.25
    if normalized_username.startswith(normalized_query):
        score += 0.60
    elif normalized_query in normalized_username:
        score += 0.30
    if normalized_username == normalized_query:
        score += 1.00

    return score, common_subsequence_length


def filter_visible_posts(
    posts: Iterable[Post],
    viewer: User | None,
    followed_user_ids: set[int],
    maximum_results: int | None = None,
) -> list[Post]:
    """按查看权限筛选帖子，并可在达到结果上限时提前停止。"""
    visible_posts: list[Post] = []
    for post in posts:
        if not can_view_post(viewer, post, followed_user_ids):
            continue
        visible_posts.append(post)
        if maximum_results is not None and len(visible_posts) >= maximum_results:
            break
    return visible_posts


@dataclass(frozen=True, slots=True)
class UsernameSearchMatch:
    """一个已计算相似度的用户名搜索候选项。"""

    score: float
    match_length: int
    user: User


def username_search_sort_key(
    search_match: UsernameSearchMatch,
) -> tuple[float, int, int, str]:
    """将更相关、更短且字典序靠前的用户名排在前面。"""
    return (
        -search_match.score,
        -search_match.match_length,
        len(search_match.user.username),
        search_match.user.username,
    )


def serialize_user_search_matches(
    search_matches: list[UsernameSearchMatch],
) -> list[dict[str, Any]]:
    """将后端搜索结果转换为前端采用 camelCase 的 JSON。"""
    serialized_results: list[dict[str, Any]] = []
    for search_match in search_matches[:MAX_SEARCH_RESULTS]:
        candidate_user = search_match.user
        serialized_results.append(
            {
                "username": candidate_user.username,
                "displayName": candidate_user.display_name,
                "profileUrl": f"/u/{quote(candidate_user.username)}",
                "matchLength": search_match.match_length,
                "score": round(search_match.score, 4),
            }
        )
    return serialized_results


async def prepare_upload_files(
    received_files: Iterable[UploadFile],
) -> list[UploadFile]:
    """保留有文件名的上传项，并及时关闭浏览器生成的空项。"""
    upload_files: list[UploadFile] = []
    for upload_file in received_files:
        if upload_file.filename:
            upload_files.append(upload_file)
        else:
            await upload_file.close()
    return upload_files


@app.get("/", response_class=HTMLResponse, name="home")
def home(
    request: Request,
    session: Session = Depends(get_database_session),
) -> Response:
    publish_due_posts(session)
    current_user = get_authenticated_user(request, session)
    followed_user_ids = get_followed_user_ids(session, current_user)
    recent_posts = (
        session.scalars(
            build_post_select()
            .order_by(
                func.coalesce(
                    Post.published_at,
                    Post.scheduled_at,
                    Post.created_at,
                ).desc()
            )
            .limit(150)
        )
        .unique()
        .all()
    )
    visible_posts = filter_visible_posts(
        recent_posts,
        current_user,
        followed_user_ids,
        maximum_results=60,
    )
    return template_renderer.render_response(
        request=request,
        name="feed.html",
        context=build_template_context(request, session, posts=visible_posts),
    )


@app.get("/register", response_class=HTMLResponse, name="register_form")
def register_form(
    request: Request, session: Session = Depends(get_database_session)
) -> Response:
    if get_authenticated_user(request, session):
        return redirect_to("/")
    return template_renderer.render_response(
        request=request,
        name="register.html",
        context=build_template_context(request, session),
    )


@app.post("/register", name="register")
def register(
    request: Request,
    username: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
    session: Session = Depends(get_database_session),
) -> RedirectResponse:
    validate_csrf_token(request, csrf)

    normalized_username = username.strip().lower()
    normalized_display_name = display_name.strip()
    username_length_is_valid = (
        MIN_USERNAME_LENGTH <= len(normalized_username) <= MAX_USERNAME_LENGTH
    )
    username_characters_are_valid = normalized_username.replace("_", "").isalnum()

    if not username_length_is_valid or not username_characters_are_valid:
        set_flash_message(
            request, "用户名必须为 3–32 个字符，只能使用字母、数字或下划线。", "danger"
        )
        return redirect_to("/register")
    if (
        not normalized_display_name
        or len(normalized_display_name) > MAX_DISPLAY_NAME_LENGTH
    ):
        set_flash_message(request, "显示名称为必填项，且不得超过 64 个字符。", "danger")
        return redirect_to("/register")
    if session.scalar(select(User).where(User.username == normalized_username)):
        set_flash_message(request, "该用户名已被占用。", "danger")
        return redirect_to("/register")

    registration_time = utc_now()
    new_user = User(
        username=normalized_username,
        display_name=normalized_display_name,
        password_hash=hash_password(password),
        created_at=registration_time,
        last_login_at=registration_time,
    )
    session.add(new_user)
    commit_database_session(session)
    session.refresh(new_user)

    request.session.clear()
    request.session["user_id"] = new_user.id
    get_or_create_csrf_token(request)
    set_flash_message(request, "注册完成。欢迎来到 SpaceBox！", "success")
    return redirect_to("/")


@app.get("/login", response_class=HTMLResponse, name="login_form")
def login_form(
    request: Request, session: Session = Depends(get_database_session)
) -> Response:
    if get_authenticated_user(request, session):
        return redirect_to("/")
    return template_renderer.render_response(
        request=request,
        name="login.html",
        context=build_template_context(request, session),
    )


@app.post("/login", name="login")
def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
    session: Session = Depends(get_database_session),
) -> RedirectResponse:
    validate_csrf_token(request, csrf)

    normalized_username = username.strip().lower()
    authenticated_user = session.scalar(
        select(User).where(User.username == normalized_username)
    )
    if authenticated_user is None or not verify_password(
        password,
        authenticated_user.password_hash,
    ):
        set_flash_message(request, "用户名或密码不正确。", "danger")
        return redirect_to("/login")

    authenticated_user.last_login_at = utc_now()
    commit_database_session(session)
    request.session.clear()
    request.session["user_id"] = authenticated_user.id
    get_or_create_csrf_token(request)
    set_flash_message(request, "登录成功。", "success")
    return redirect_to("/")


@app.post("/logout", name="logout")
def logout(request: Request, csrf: Annotated[str, Form()]) -> RedirectResponse:
    validate_csrf_token(request, csrf)
    request.session.clear()
    return redirect_to("/")


@app.get("/api/users/search", response_class=JSONResponse, name="search_users")
def search_users(
    search_term: Annotated[str, Query(alias="q")] = "",
    session: Session = Depends(get_database_session),
) -> dict[str, Any]:
    normalized_query = search_term.strip().casefold()[:MAX_USERNAME_LENGTH]
    if not normalized_query:
        return {"query": "", "results": []}

    candidate_users = session.scalars(
        select(User).order_by(User.username).limit(MAX_SEARCH_CANDIDATES)
    ).all()
    search_matches: list[UsernameSearchMatch] = []
    minimum_match_length = max(1, (len(normalized_query) + 1) // 2)

    for candidate_user in candidate_users:
        score, common_subsequence_length = calculate_username_search_score(
            normalized_query,
            candidate_user.username,
        )
        if common_subsequence_length < minimum_match_length:
            continue
        search_matches.append(
            UsernameSearchMatch(
                score=score,
                match_length=common_subsequence_length,
                user=candidate_user,
            )
        )

    search_matches.sort(key=username_search_sort_key)

    return {
        "query": normalized_query,
        "results": serialize_user_search_matches(search_matches),
    }


@app.get("/post/new", response_class=HTMLResponse, name="new_post_form")
def new_post_form(
    request: Request, session: Session = Depends(get_database_session)
) -> Response:
    current_user = require_authenticated_user(request, session)
    return template_renderer.render_response(
        request=request,
        name="new_post.html",
        context=build_template_context(request, session, user=current_user),
    )


@app.post("/post/new", name="create_post")
async def create_post(
    request: Request,
    visibility: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
    content: Annotated[str | None, Form()] = None,
    scheduled_at_utc: Annotated[str | None, Form()] = None,
    files: list[UploadFile] | None = File(default=None),
    session: Session = Depends(get_database_session),
) -> RedirectResponse:
    current_user = require_authenticated_user(request, session)
    validate_csrf_token(request, csrf)

    received_files: list[UploadFile] = []
    if files is not None:
        received_files = files
    uploaded_files = await prepare_upload_files(received_files)

    try:
        draft = build_post_draft(
            content=content,
            visibility=visibility,
            scheduled_at_utc=scheduled_at_utc,
            current_user=current_user,
            upload_count=len(uploaded_files),
        )
    except HTTPException as exc:
        for uploaded_file in uploaded_files:
            await uploaded_file.close()
        set_flash_message(request, str(exc.detail), "danger")
        return redirect_to("/post/new")

    try:
        stored_media = await media_storage_service.store_uploads(uploaded_files)
    except HTTPException as exc:
        set_flash_message(request, str(exc.detail), "danger")
        return redirect_to("/post/new")

    try:
        new_post = persist_post(session, current_user.id, draft, stored_media)
    except BaseException:
        stored_paths: list[str] = []
        for media_item in stored_media:
            stored_paths.append(media_item.file_path)
        media_storage_service.delete_indexed_files(stored_paths)
        raise

    if draft.scheduled_at:
        set_flash_message(request, "帖子已成功设置定时发布。", "success")
    else:
        set_flash_message(request, "帖子发布成功。", "success")
    return redirect_to(f"/post/{new_post.id}")


@app.api_route(
    "/media/{media_id}",
    methods=["GET", "HEAD"],
    name="media_content",
)
def media_content(
    media_id: int,
    request: Request,
    session: Session = Depends(get_database_session),
) -> Response:
    publish_due_posts(session)
    media_record = session.scalar(
        select(Media)
        .options(joinedload(Media.post).joinedload(Post.author))
        .where(Media.id == media_id)
    )
    if media_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到媒体",
        )

    current_user = get_authenticated_user(request, session)
    followed_user_ids = get_followed_user_ids(session, current_user)
    if not can_view_post(
        current_user,
        media_record.post,
        followed_user_ids,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="你没有权限查看此媒体",
        )

    source = media_storage_service.load_media_source(media_record)
    return media_storage_service.build_media_response(request, media_record, source)


@app.get("/post/{post_id}", response_class=HTMLResponse, name="post_detail")
def post_detail(
    post_id: int,
    request: Request,
    session: Session = Depends(get_database_session),
) -> Response:
    publish_due_posts(session)
    post = session.scalar(build_post_select().where(Post.id == post_id))
    if post is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到帖子",
        )

    current_user = get_authenticated_user(request, session)
    followed_user_ids = get_followed_user_ids(session, current_user)
    if not can_view_post(current_user, post, followed_user_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="你没有权限查看此帖子",
        )

    return template_renderer.render_response(
        request=request,
        name="post_detail.html",
        context=build_template_context(
            request,
            session,
            post=post,
            post_is_published=is_post_published(post),
            comment_tree=build_comment_tree(post.comments),
        ),
    )


@app.post("/post/{post_id}/comment", name="create_comment")
def create_comment(
    post_id: int,
    request: Request,
    content: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
    parent_id: Annotated[int | None, Form()] = None,
    session: Session = Depends(get_database_session),
) -> RedirectResponse:
    current_user = require_authenticated_user(request, session)
    validate_csrf_token(request, csrf)
    publish_due_posts(session)

    post = session.scalar(build_post_select().where(Post.id == post_id))
    if post is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到帖子",
        )
    if not is_post_published(post):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="定时帖子在发布前不能接收评论",
        )

    followed_user_ids = get_followed_user_ids(session, current_user)
    if not can_view_post(current_user, post, followed_user_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="你没有权限评论此帖子",
        )

    normalized_content = normalize_line_endings(content)
    if not normalized_content.strip() or len(normalized_content) > MAX_COMMENT_LENGTH:
        set_flash_message(request, "评论不能为空，且不得超过 1,000 个字符。", "danger")
        return redirect_to(f"/post/{post_id}")

    if parent_id is not None:
        parent_comment = session.get(Comment, parent_id)
        if parent_comment is None or parent_comment.post_id != post_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="无效的回复目标",
            )
        if parent_comment.is_deleted:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="已删除的评论不能接收新回复",
            )

    new_comment = Comment(
        post_id=post_id,
        author_id=current_user.id,
        parent_id=parent_id,
        content=normalized_content,
    )
    session.add(new_comment)
    commit_database_session(session)
    set_flash_message(request, "评论发布成功。", "success")
    return redirect_to(f"/post/{post_id}#comments")


@app.post("/comment/{comment_id}/delete", name="delete_comment")
def delete_comment(
    comment_id: int,
    request: Request,
    csrf: Annotated[str, Form()],
    session: Session = Depends(get_database_session),
) -> RedirectResponse:
    current_user = require_authenticated_user(request, session)
    validate_csrf_token(request, csrf)

    comment = session.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到评论",
        )
    if comment.author_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="你只能删除自己的评论",
        )

    post_id = comment.post_id
    if not comment.is_deleted:
        comment.is_deleted = True
        comment.deleted_at = utc_now()
        # 软删除会有意在数据库中保留 content 和 parent_id。
        commit_database_session(session)
        set_flash_message(
            request,
            "评论已标记为删除，其下方的回复已保留。",
            "success",
        )

    return redirect_to(f"/post/{post_id}#comment-{comment.id}")


@app.post("/post/{post_id}/delete", name="delete_post")
def delete_post(
    post_id: int,
    request: Request,
    csrf: Annotated[str, Form()],
    session: Session = Depends(get_database_session),
) -> RedirectResponse:
    current_user = require_authenticated_user(request, session)
    validate_csrf_token(request, csrf)

    post = session.scalar(
        select(Post).options(selectinload(Post.media)).where(Post.id == post_id)
    )
    if post is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到帖子",
        )
    if post.author_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="你只能删除自己的帖子",
        )

    stored_paths: list[str] = []
    for media_record in post.media:
        if media_record.file_path:
            stored_paths.append(media_record.file_path)

    session.delete(post)
    commit_database_session(session)
    media_storage_service.delete_indexed_files(stored_paths)
    set_flash_message(request, "帖子删除成功。", "success")
    return redirect_to("/")


def render_profile_page(
    username: str,
    request: Request,
    session: Session,
) -> Response:
    """渲染公开个人主页框架，并按查看者权限筛选帖子。"""
    publish_due_posts(session)
    profile_user = session.scalar(select(User).where(User.username == username.lower()))
    if profile_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到用户",
        )

    current_user = get_authenticated_user(request, session)
    followed_user_ids = get_followed_user_ids(session, current_user)
    profile_posts = (
        session.scalars(
            build_post_select()
            .where(Post.author_id == profile_user.id)
            .order_by(
                func.coalesce(
                    Post.published_at,
                    Post.scheduled_at,
                    Post.created_at,
                ).desc()
            )
        )
        .unique()
        .all()
    )
    visible_posts = filter_visible_posts(
        profile_posts,
        current_user,
        followed_user_ids,
    )

    follower_count = (
        session.scalar(
            select(func.count())
            .select_from(follow_table)
            .where(follow_table.c.followed_id == profile_user.id)
        )
        or 0
    )
    following_count = (
        session.scalar(
            select(func.count())
            .select_from(follow_table)
            .where(follow_table.c.follower_id == profile_user.id)
        )
        or 0
    )

    return template_renderer.render_response(
        request=request,
        name="profile.html",
        context=build_template_context(
            request,
            session,
            profile_user=profile_user,
            posts=visible_posts,
            is_following=profile_user.id in followed_user_ids,
            follower_count=follower_count,
            following_count=following_count,
        ),
    )


@app.get("/u/{username}", response_class=HTMLResponse, name="profile")
def profile(
    username: str,
    request: Request,
    session: Session = Depends(get_database_session),
) -> Response:
    return render_profile_page(username, request, session)


@app.get("/@{username}", response_class=HTMLResponse, name="profile_short")
def profile_short(
    username: str,
    request: Request,
    session: Session = Depends(get_database_session),
) -> Response:
    return render_profile_page(username, request, session)


@app.post("/u/{username}/follow", name="follow_user")
def follow_user(
    username: str,
    request: Request,
    csrf: Annotated[str, Form()],
    session: Session = Depends(get_database_session),
) -> RedirectResponse:
    current_user = require_authenticated_user(request, session)
    validate_csrf_token(request, csrf)

    target_user = session.scalar(select(User).where(User.username == username.lower()))
    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到用户",
        )
    if target_user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="你不能关注自己",
        )

    existing_follow = session.scalar(
        select(follow_table.c.follower_id).where(
            follow_table.c.follower_id == current_user.id,
            follow_table.c.followed_id == target_user.id,
        )
    )
    if existing_follow is None:
        session.execute(
            follow_table.insert().values(
                follower_id=current_user.id,
                followed_id=target_user.id,
            )
        )
        commit_database_session(session)

    set_flash_message(request, f"现在正在关注 @{target_user.username}", "success")
    return redirect_to(f"/u/{target_user.username}")


@app.post("/u/{username}/unfollow", name="unfollow_user")
def unfollow_user(
    username: str,
    request: Request,
    csrf: Annotated[str, Form()],
    session: Session = Depends(get_database_session),
) -> RedirectResponse:
    current_user = require_authenticated_user(request, session)
    validate_csrf_token(request, csrf)

    target_user = session.scalar(select(User).where(User.username == username.lower()))
    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到用户",
        )

    session.execute(
        delete(follow_table).where(
            follow_table.c.follower_id == current_user.id,
            follow_table.c.followed_id == target_user.id,
        )
    )
    commit_database_session(session)
    set_flash_message(request, f"已取消关注 @{target_user.username}", "success")
    return redirect_to(f"/u/{target_user.username}")


@app.get("/settings", response_class=HTMLResponse, name="settings_form")
def settings_form(
    request: Request,
    session: Session = Depends(get_database_session),
) -> Response:
    current_user = require_authenticated_user(request, session)
    return template_renderer.render_response(
        request=request,
        name="settings.html",
        context=build_template_context(
            request,
            session,
            user=current_user,
        ),
    )


@app.post("/settings", name="settings_update")
def settings_update(
    request: Request,
    display_name: Annotated[str, Form()],
    bio: Annotated[str, Form()],
    default_post_visibility: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
    session: Session = Depends(get_database_session),
) -> RedirectResponse:
    current_user = require_authenticated_user(request, session)
    validate_csrf_token(request, csrf)

    normalized_display_name = display_name.strip()
    normalized_bio = normalize_line_endings(bio)
    if (
        not normalized_display_name
        or len(normalized_display_name) > MAX_DISPLAY_NAME_LENGTH
    ):
        set_flash_message(request, "显示名称为必填项，且不得超过 64 个字符。", "danger")
        return redirect_to("/settings")
    if len(normalized_bio) > MAX_BIO_LENGTH:
        set_flash_message(request, "个人简介不得超过 500 个字符。", "danger")
        return redirect_to("/settings")
    if default_post_visibility not in POST_VISIBILITIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的可见性设置",
        )

    current_user.display_name = normalized_display_name
    current_user.bio = normalized_bio
    current_user.default_post_visibility = default_post_visibility
    commit_database_session(session)

    set_flash_message(request, "设置保存成功。", "success")
    return redirect_to("/settings")


@app.exception_handler(HTTPException)
def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        return redirect_to("/login")

    return template_renderer.render_response(
        request=request,
        name="error.html",
        context={
            "current_user": None,
            "csrf_token": "",
            "flash": None,
            "status_code": exc.status_code,
            "detail": exc.detail,
        },
        status_code=exc.status_code,
    )


def _find_available_port(start_port: int = 8000, attempts: int = 50) -> int:
    """查找所有网络接口上的可用 TCP 端口，优先使用 8000 端口。"""
    for port in range(start_port, start_port + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
            try:
                probe_socket.bind(("0.0.0.0", port))
            except OSError:
                continue
            return port
    raise RuntimeError("在 8000–8049 范围内未找到可用的本地端口。")


def main() -> None:
    """启动 SpaceBox，无需 CLI 参数或环境配置。"""
    host = "0.0.0.0"
    port = _find_available_port()
    url = f"http://{host}:{port}"
    print("=" * 68)
    print(f"{APP_NAME} {APP_VERSION}")
    print(f"数据库   : {DATABASE_PATH}")
    print(f"媒体目录 : {MEDIA_STORAGE_DIR}")
    print(f"监听     : {url}")
    print("按 Ctrl+C 停止。")
    print("=" * 68)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
