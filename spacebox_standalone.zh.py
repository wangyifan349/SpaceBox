#!/usr/bin/env python3
"""SpaceBox 单文件启动器。

本文件有意设计为完全自包含：
- FastAPI 后端、SQLAlchemy 模型、身份验证和路由均位于此处。
- Jinja 模板、项目 CSS/JS 和 Bootstrap 资源均内嵌于此。
- 应用配置不会从环境变量中读取。
- SQLite 数据库、媒体目录和持久化 Session 密钥会自动管理。
- 大型媒体采用流式文件存储与 HTTP Range 分段传输，避免整文件进入内存。
- 每次启动都会清理长期未登录的空账号，并校验媒体文件索引。

直接运行：``python spacebox_standalone.zh.py``
"""

# 运行时依赖检查必须先执行，因此第三方模块有意在检查函数调用后导入。
# ruff: noqa: E402

from __future__ import annotations

import base64
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
import zlib
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


# 自有模板、样式和脚本保留为可读源码；仅压缩第三方发行文件。
BOOTSTRAP_LICENSE_NOTICE = r"""Bootstrap 根据 MIT 许可证分发。

版权所有 (c) 2011-2025 Bootstrap 作者

特此免费授予任何获得本软件及相关文档文件（“软件”）副本的人不受限制地处理本软件的许可，包括但不限于使用、复制、修改、合并、出版、分发、再许可和/或销售本软件副本的权利，并允许获得本软件的人在遵守以下条件的前提下这样做：

上述版权声明和本许可声明应包含在本软件的所有副本或实质性部分中。

本软件按“原样”提供，不作任何形式的明示或默示保证，包括但不限于对适销性、特定用途适用性及不侵权的保证。在任何情况下，作者或版权所有者均不对因本软件或本软件的使用或其他交易而引起、产生或与之相关的任何索赔、损害或其他责任承担责任，无论该责任是基于合同、侵权或其他行为。"""


EMBEDDED_TEMPLATES: dict[str, str] = {
    "base.html": r"""<!doctype html>
<html lang="zh-CN" data-bs-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>{% block title %}SpaceBox{% endblock %}</title>
  <link
    href="{{ request.url_for('static', path='bootstrap.min.css') }}?v={{ app_version }}"
    rel="stylesheet"
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
  src="{{ request.url_for('static', path='bootstrap.bundle.min.js') }}?v={{ app_version }}"
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
          <label class="form-label mb-0 fw-semibold">内容</label>
          <span id="post-char-count" class="small text-secondary"></span>
        </div>
        <textarea
          class="form-control post-editor"
          name="content"
          rows="9"
          maxlength="5000"
          data-indentable
          data-count-target="post-char-count"
          placeholder="分享点什么……&#10;&#10;提示：在这里按 Tab 会插入缩进，而不是移动焦点。"
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
        uploadStatus.text.textContent = `上传失败（HTTP ${uploadRequest.status}），请重试。`;
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


_COMPRESSED_VENDOR_ASSETS: dict[str, str] = {
    "bootstrap.min.css": (
        "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"
        "oXtc9Puzo3dRR1vZ"
    ),
    "bootstrap.bundle.min.js": (
        "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"
        "qIgK8A=="
    ),
}


_EMBEDDED_ASSET_TYPES = {
    "style.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "bootstrap.min.css": "text/css; charset=utf-8",
    "bootstrap.bundle.min.js": "application/javascript; charset=utf-8",
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
    """按需编码自有资源或解压第三方前端发行文件。"""
    cached_asset = _EMBEDDED_ASSET_CACHE.get(asset_name)
    if cached_asset is not None:
        return cached_asset

    if asset_name == "style.css":
        asset_bytes = CUSTOM_STYLESHEET.encode("utf-8")
    elif asset_name == "app.js":
        asset_bytes = build_application_javascript().encode("utf-8")
    else:
        encoded_payload = _COMPRESSED_VENDOR_ASSETS.get(asset_name)
        if encoded_payload is None:
            raise KeyError(asset_name)
        asset_bytes = zlib.decompress(base64.b64decode(encoded_payload))

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
    """提供直接内嵌在此 Python 文件中的前端资源。"""
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
    content: str,
    visibility: str,
    scheduled_at_utc: str | None,
    current_user: User,
    upload_count: int,
) -> PostDraft:
    """集中校验发帖表单，路由只负责协调认证、存储与响应。"""
    normalized_content = normalize_line_endings(content)
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
    content: Annotated[str, Form()],
    visibility: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
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
