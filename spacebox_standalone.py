#!/usr/bin/env python3
"""SpaceBox single-file launcher.

This file is intentionally self-contained:
- FastAPI backend, SQLAlchemy models, authentication and routes live here.
- Jinja templates, project CSS/JS, and Bootstrap assets are embedded here.
- No application configuration is read from environment variables.
- SQLite database and a persistent session secret are managed automatically.

Run directly with: ``python spacebox_standalone.py``
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import mimetypes
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser
import zlib
from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, TypedDict
from urllib.parse import quote
from zoneinfo import ZoneInfo


def _ensure_runtime_dependencies() -> None:
    """Install missing runtime dependencies only when this file is run standalone."""
    required_modules = {
        "fastapi": "fastapi>=0.128,<1.0",
        "uvicorn": "uvicorn[standard]>=0.30",
        "sqlalchemy": "SQLAlchemy>=2.0,<2.1",
        "jinja2": "Jinja2>=3.1",
        "multipart": "python-multipart>=0.0.20",
        "itsdangerous": "itsdangerous>=2.2",
        "tzdata": "tzdata>=2024.1",
    }
    missing_packages = [
        package
        for module_name, package in required_modules.items()
        if importlib.util.find_spec(module_name) is None
    ]
    if not missing_packages:
        return

    print("SpaceBox: installing missing Python dependencies...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", *missing_packages]
    )


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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import DictLoader, Environment, select_autoescape
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    create_engine,
    delete,
    event,
    func,
    inspect,
    select,
    text,
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
from starlette.middleware.sessions import SessionMiddleware

APP_NAME = "SpaceBox"
APP_VERSION = "0.3.0-standalone"
BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "social.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH.as_posix()}"
COOKIE_SECURE = False
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 14
DEFAULT_LOCAL_TIMEZONE = ZoneInfo("Asia/Singapore")

MAX_MEDIA_FILE_SIZE_BYTES = 30 * 1024 * 1024
MAX_MEDIA_FILES_PER_POST = 6
MAX_SEARCH_CANDIDATES = 10_000
MAX_SEARCH_RESULTS = 8
MAX_POST_LENGTH = 5_000
MAX_COMMENT_LENGTH = 1_000
MAX_BIO_LENGTH = 500
MAX_DISPLAY_NAME_LENGTH = 64
MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 32

ALLOWED_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})
ALLOWED_VIDEO_EXTENSIONS = frozenset({".mp4", ".webm", ".mov", ".m4v"})
POST_VISIBILITIES = frozenset({"public", "followers", "private"})
VISIBILITY_LABELS = {
    "public": "Public",
    "followers": "Followers Only",
    "private": "Private",
}

# Third-party frontend notice for the embedded Bootstrap distribution.
BOOTSTRAP_LICENSE_NOTICE = 'Bootstrap is distributed under the MIT License.\n\nCopyright (c) 2011-2025 The Bootstrap Authors\n\nPermission is hereby granted, free of charge, to any person obtaining a copy\nof this software and associated documentation files (the "Software"), to deal\nin the Software without restriction, including without limitation the rights\nto use, copy, modify, merge, publish, distribute, sublicense, and/or sell\ncopies of the Software, and to permit persons to whom the Software is\nfurnished to do so, subject to the following conditions:\n\nThe above copyright notice and this permission notice shall be included in all\ncopies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\nIMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\nFITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\nAUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\nLIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\nOUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\nSOFTWARE.\n'

EMBEDDED_TEMPLATES: dict[str, str] = {'base.html': '<!doctype html>\n<html lang="en" data-bs-theme="light">\n<head>\n  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1">\n  <meta name="color-scheme" content="light dark">\n  <title>{% block title %}SpaceBox{% endblock %}</title>\n  <link\n    href="{{ request.url_for(\'static\', path=\'bootstrap.min.css\') }}"\n    rel="stylesheet"\n  >\n  <link\n    rel="stylesheet"\n    href="{{ request.url_for(\'static\', path=\'style.css\') }}"\n  >\n</head>\n<body class="bg-body-tertiary">\n<nav class="navbar navbar-expand-lg bg-body border-bottom sticky-top app-navbar">\n  <div class="container app-shell">\n    <a class="navbar-brand fw-bold" href="{{ request.url_for(\'home\') }}">\n      SpaceBox\n    </a>\n\n    <div class="navbar-search mx-lg-4 order-lg-2 flex-grow-1">\n      <div class="position-relative" id="user-search-wrap">\n        <input\n          id="user-search-input"\n          class="form-control form-control-sm search-input"\n          type="search"\n          autocomplete="off"\n          spellcheck="false"\n          placeholder="Search usernames, e.g. alex_01"\n          aria-label="Search users"\n          aria-controls="user-search-menu"\n          aria-expanded="false"\n          aria-autocomplete="list"\n        >\n        <div\n          id="user-search-menu"\n          class="search-dropdown shadow-lg d-none"\n          role="listbox"\n        ></div>\n      </div>\n    </div>\n\n    <button\n      class="navbar-toggler order-lg-3"\n      type="button"\n      data-bs-toggle="collapse"\n      data-bs-target="#mainNav"\n      aria-label="Toggle navigation"\n    >\n      <span class="navbar-toggler-icon"></span>\n    </button>\n\n    <div class="collapse navbar-collapse order-lg-4 flex-grow-0" id="mainNav">\n      <div class="navbar-nav ms-auto align-items-lg-center gap-lg-1">\n        {% if current_user %}\n          <a class="nav-link" href="{{ request.url_for(\'new_post_form\') }}">\n            Post\n          </a>\n          <a\n            class="nav-link"\n            href="{{ request.url_for(\'profile\', username=current_user.username) }}"\n          >\n            @{{ current_user.username }}\n          </a>\n          <a class="nav-link" href="{{ request.url_for(\'settings_form\') }}">\n            Settings\n          </a>\n          <form\n            method="post"\n            action="{{ request.url_for(\'logout\') }}"\n            class="d-inline"\n          >\n            <input type="hidden" name="csrf" value="{{ csrf_token }}">\n            <button class="btn btn-outline-secondary btn-sm" type="submit">\n              Log out\n            </button>\n          </form>\n        {% else %}\n          <a class="nav-link" href="{{ request.url_for(\'login_form\') }}">\n            Log in\n          </a>\n          <a\n            class="btn btn-primary btn-sm"\n            href="{{ request.url_for(\'register_form\') }}"\n          >\n            Sign up\n          </a>\n        {% endif %}\n      </div>\n    </div>\n  </div>\n</nav>\n\n<main class="container content-shell py-4">\n  {% if flash %}\n    <div\n      class="alert alert-{{ flash.category }} alert-dismissible fade show shadow-sm"\n      role="alert"\n    >\n      {{ flash.message }}\n      <button\n        type="button"\n        class="btn-close"\n        data-bs-dismiss="alert"\n        aria-label="Close"\n      ></button>\n    </div>\n  {% endif %}\n\n  {% block content %}{% endblock %}\n</main>\n\n<script src="{{ request.url_for(\'static\', path=\'bootstrap.bundle.min.js\') }}"></script>\n<script src="{{ request.url_for(\'static\', path=\'app.js\') }}"></script>\n{% block scripts %}{% endblock %}\n</body>\n</html>\n', 'error.html': '{% extends "base.html" %}\n{% block title %}Error · SpaceBox{% endblock %}\n{% block content %}\n<div class="card border-0 shadow-sm">\n  <div class="card-body p-5 text-center">\n    <div class="display-5 fw-bold mb-2">{{ status_code }}</div>\n    <p class="text-secondary mb-4">{{ detail }}</p>\n    <a class="btn btn-primary" href="/">Back to Home</a>\n  </div>\n</div>\n{% endblock %}\n', 'feed.html': '{% extends "base.html" %}\n{% block title %}Feed · SpaceBox{% endblock %}\n{% block content %}\n<div class="d-flex justify-content-between align-items-center mb-4 gap-3">\n  <div>\n    <h1 class="h4 mb-1">Feed</h1>\n    <div class="text-secondary small">Public posts plus content you are allowed to view. Times are shown in your browser\'s local time zone.</div>\n  </div>\n  {% if current_user %}\n    <a class="btn btn-primary flex-shrink-0" href="{{ request.url_for(\'new_post_form\') }}">New Post</a>\n  {% endif %}\n</div>\n\n{% if posts %}\n  {% for post in posts %}\n    {% include "partials/post_card.html" %}\n  {% endfor %}\n{% else %}\n  <div class="empty-state card border-0 shadow-sm text-center py-5 text-secondary">\n    <div class="fs-2 mb-2">🪐</div>\n    <p class="mb-0">There are no posts available to you yet.</p>\n  </div>\n{% endif %}\n{% endblock %}\n', 'login.html': '{% extends "base.html" %}\n{% block title %}Log In · SpaceBox{% endblock %}\n{% block content %}\n<div class="auth-card card border-0 shadow-sm mx-auto">\n  <div class="card-body p-4 p-md-5">\n    <h1 class="h4 mb-4">Log In</h1>\n    <form method="post" action="{{ request.url_for(\'login\') }}">\n      <input type="hidden" name="csrf" value="{{ csrf_token }}">\n\n      <div class="mb-3">\n        <label class="form-label">Username</label>\n        <input\n          class="form-control"\n          name="username"\n          required\n          autocomplete="username"\n        >\n      </div>\n\n      <div class="mb-4">\n        <label class="form-label">Password</label>\n        <input\n          class="form-control"\n          name="password"\n          type="password"\n          required\n          autocomplete="current-password"\n        >\n      </div>\n\n      <button class="btn btn-primary w-100" type="submit">Log In</button>\n    </form>\n  </div>\n</div>\n{% endblock %}\n', 'new_post.html': '{% extends "base.html" %}\n{% block title %}New Post · SpaceBox{% endblock %}\n{% block content %}\n<div class="page-heading mb-4">\n  <h1 class="h4 mb-1">Create a New Post</h1>\n  <div class="text-secondary small">\n    Spaces, tabs, line breaks, and indentation are preserved. Press Tab in the editor to insert indentation.\n  </div>\n</div>\n\n<div class="card border-0 shadow-sm composer-card">\n  <div class="card-body p-2 p-md-3">\n    <form\n      method="post"\n      action="{{ request.url_for(\'create_post\') }}"\n      enctype="multipart/form-data"\n      data-schedule-form\n    >\n      <input type="hidden" name="csrf" value="{{ csrf_token }}">\n      <input type="hidden" name="scheduled_at_utc" value="">\n\n      <div class="mb-3">\n        <div class="d-flex justify-content-between align-items-center mb-2">\n          <label class="form-label mb-0 fw-semibold">Content</label>\n          <span id="post-char-count" class="small text-secondary"></span>\n        </div>\n        <textarea\n          class="form-control post-editor"\n          name="content"\n          rows="9"\n          maxlength="5000"\n          data-indentable\n          data-count-target="post-char-count"\n          placeholder="Share something...&#10;&#10;Tip: press Tab here to insert indentation instead of moving focus."\n        ></textarea>\n      </div>\n\n      <div class="mb-3">\n        <label class="form-label fw-semibold">Images / Videos</label>\n        <input\n          id="media-files"\n          class="form-control"\n          type="file"\n          name="files"\n          multiple\n          accept="image/jpeg,image/png,image/gif,image/webp,video/mp4,video/webm,video/quicktime"\n        >\n        <div class="form-text">\n          Up to 6 attachments, 30 MB each. Uploaded media is stored directly in SQLite as binary data,\n          not in an uploads directory.\n        </div>\n        <div id="media-preview" class="preview-grid mt-3"></div>\n      </div>\n\n      <div class="row g-3 mb-4">\n        <div class="col-md-6">\n          <label class="form-label fw-semibold">Who can view this?</label>\n          <select class="form-select" name="visibility">\n            {% for visibility_key, visibility_label in visibility_labels.items() %}\n              <option\n                value="{{ visibility_key }}"\n                {% if user.default_post_visibility == visibility_key %}selected{% endif %}\n              >\n                {{ visibility_label }}\n              </option>\n            {% endfor %}\n          </select>\n        </div>\n\n        <div class="col-md-6">\n          <label class="form-label fw-semibold">\n            Schedule publication\n            <span class="fw-normal text-secondary">(optional)</span>\n          </label>\n          <input class="form-control" type="datetime-local" data-schedule-local>\n          <div class="form-text">\n            Use your browser\'s local time. Leave this blank to publish immediately.\n          </div>\n        </div>\n      </div>\n\n      <div class="d-flex gap-2 justify-content-end">\n        <a class="btn btn-outline-secondary" href="{{ request.url_for(\'home\') }}">\n          Cancel\n        </a>\n        <button class="btn btn-primary px-4" type="submit">\n          Publish / Schedule\n        </button>\n      </div>\n    </form>\n  </div>\n</div>\n{% endblock %}\n', 'partials/comment_tree.html': '{% macro render_comments(comment_nodes, post, current_user, csrf_token, depth=0) %}\n  {% for comment_node in comment_nodes %}\n    {% set comment = comment_node.comment %}\n    <div\n      class="comment-node"\n      id="comment-{{ comment.id }}"\n      data-depth="{{ depth }}"\n    >\n      <div class="comment-main">\n        <div class="d-flex gap-2 align-items-start">\n          <a\n            href="{{ request.url_for(\'profile\', username=comment.author.username) }}"\n            class="avatar avatar-sm text-decoration-none flex-shrink-0"\n          >\n            {{ comment.author.display_name[:1]|upper }}\n          </a>\n\n          <div class="flex-grow-1 min-w-0">\n            <div class="d-flex justify-content-between gap-2 align-items-start">\n              <div class="min-w-0">\n                <a\n                  href="{{ request.url_for(\'profile\', username=comment.author.username) }}"\n                  class="fw-semibold text-decoration-none text-body"\n                >\n                  {{ comment.author.display_name }}\n                </a>\n                <span class="text-secondary small">\n                  @{{ comment.author.username }}\n                </span>\n\n                <div class="small text-secondary">\n                  <time\n                    data-datetime="{{ comment.created_at.isoformat() }}"\n                    data-prefix="Commented: "\n                    data-relative\n                  ></time>\n                  {% if comment.is_deleted and comment.deleted_at %}\n                    ·\n                    <time\n                      data-datetime="{{ comment.deleted_at.isoformat() }}"\n                      data-prefix="Deleted: "\n                      data-relative\n                    ></time>\n                  {% endif %}\n                </div>\n              </div>\n\n              {% if\n                current_user\n                and current_user.id == comment.author_id\n                and not comment.is_deleted\n              %}\n                <form\n                  method="post"\n                  action="{{ request.url_for(\'delete_comment\', comment_id=comment.id) }}"\n                  onsubmit="return confirm(\'Delete this comment text? Replies below it will remain.\');"\n                >\n                  <input type="hidden" name="csrf" value="{{ csrf_token }}">\n                  <button\n                    class="btn btn-link btn-sm text-danger p-0 text-decoration-none"\n                    type="submit"\n                  >\n                    Delete\n                  </button>\n                </form>\n              {% endif %}\n            </div>\n\n            {% if comment.is_deleted %}\n              <div class="deleted-comment my-2">This comment was deleted by its author.</div>\n            {% else %}\n              <div class="comment-content my-2">{{ comment.content }}</div>\n            {% endif %}\n\n            {% if current_user and not comment.is_deleted %}\n              <button\n                type="button"\n                class="btn btn-sm btn-link p-0 text-decoration-none reply-button"\n                data-reply-to="{{ comment.id }}"\n              >\n                Reply\n              </button>\n              <div\n                id="reply-box-{{ comment.id }}"\n                class="d-none mt-2 reply-box"\n              >\n                <form\n                  method="post"\n                  action="{{ request.url_for(\'create_comment\', post_id=post.id) }}"\n                >\n                  <input type="hidden" name="csrf" value="{{ csrf_token }}">\n                  <input\n                    type="hidden"\n                    name="parent_id"\n                    value="{{ comment.id }}"\n                  >\n                  <textarea\n                    class="form-control form-control-sm mb-2"\n                    name="content"\n                    rows="2"\n                    maxlength="1000"\n                    required\n                    data-indentable\n                    placeholder="Reply to @{{ comment.author.username }}"\n                  ></textarea>\n                  <div class="d-flex justify-content-end">\n                    <button class="btn btn-primary btn-sm" type="submit">\n                      Send Reply\n                    </button>\n                  </div>\n                </form>\n              </div>\n            {% endif %}\n          </div>\n        </div>\n      </div>\n\n      {% if comment_node.children %}\n        <div class="comment-children">\n          {{ render_comments(\n            comment_node.children,\n            post,\n            current_user,\n            csrf_token,\n            depth + 1\n          ) }}\n        </div>\n      {% endif %}\n    </div>\n  {% endfor %}\n{% endmacro %}\n', 'partials/post_card.html': '<article\n  class="card shadow-sm border-0 mb-4 post-card\n    {% if not post.published_at %}scheduled-post{% endif %}"\n>\n  <div class="card-body p-2 p-md-3">\n    <div class="d-flex justify-content-between gap-3 mb-3">\n      <div class="d-flex gap-3 min-w-0">\n        <a\n          href="{{ request.url_for(\'profile\', username=post.author.username) }}"\n          class="avatar text-decoration-none"\n          aria-label="View {{ post.author.display_name }}\'s profile"\n        >\n          {{ post.author.display_name[:1]|upper }}\n        </a>\n\n        <div class="min-w-0">\n          <a\n            class="fw-semibold text-decoration-none text-body d-inline-block\n              text-truncate author-link"\n            href="{{ request.url_for(\'profile\', username=post.author.username) }}"\n          >\n            {{ post.author.display_name }}\n          </a>\n          <div class="text-secondary small text-truncate">\n            @{{ post.author.username }}\n          </div>\n          <div class="post-time small text-secondary mt-1">\n            {% if not post.published_at and post.scheduled_at %}\n              <time\n                data-datetime="{{ post.scheduled_at.isoformat() }}"\n                data-prefix="Scheduled: "\n                data-relative\n              ></time>\n            {% else %}\n              <time\n                data-datetime="{{ (post.published_at or post.created_at).isoformat() }}"\n                data-prefix="Published: "\n                data-relative\n              ></time>\n            {% endif %}\n          </div>\n        </div>\n      </div>\n\n      <div class="d-flex gap-2 align-items-start flex-wrap justify-content-end">\n        {% if not post.published_at %}\n          <span class="badge rounded-pill text-bg-warning">Scheduled</span>\n        {% endif %}\n        <span class="badge rounded-pill text-bg-light border">\n          {{ visibility_labels[post.visibility] }}\n        </span>\n      </div>\n    </div>\n\n    {% if post.content %}\n      <div class="post-content mb-3">{{ post.content }}</div>\n    {% endif %}\n\n    {% if post.media %}\n      <div\n        class="media-grid media-count-{{ [post.media|length, 4]|min }} mb-3"\n      >\n        {% for media_item in post.media %}\n          <div class="media-tile">\n            {% if media_item.media_type == \'image\' %}\n              <a\n                href="{{ request.url_for(\'media_content\', media_id=media_item.id) }}"\n                target="_blank"\n                rel="noopener"\n              >\n                <img\n                  src="{{ request.url_for(\'media_content\', media_id=media_item.id) }}"\n                  class="media-frame"\n                  alt="{{ media_item.original_name }}"\n                  loading="lazy"\n                >\n              </a>\n            {% else %}\n              <video class="media-frame" controls preload="metadata">\n                <source\n                  src="{{ request.url_for(\'media_content\', media_id=media_item.id) }}"\n                  type="{{ media_item.mime_type }}"\n                >\n                Your browser does not support video playback.\n              </video>\n            {% endif %}\n          </div>\n        {% endfor %}\n      </div>\n    {% endif %}\n\n    <div class="d-flex align-items-center gap-3 small post-actions pt-2">\n      {% if post.published_at %}\n        <a\n          href="{{ request.url_for(\'post_detail\', post_id=post.id) }}#comments"\n          class="text-decoration-none text-secondary action-link"\n        >\n          Comments {{ post.active_comment_count }}\n        </a>\n      {% else %}\n        <span class="text-secondary">Comments open after publication</span>\n      {% endif %}\n\n      <a\n        href="{{ request.url_for(\'post_detail\', post_id=post.id) }}"\n        class="text-decoration-none text-secondary action-link"\n      >\n        Details\n      </a>\n\n      {% if current_user and current_user.id == post.author_id %}\n        <form\n          method="post"\n          action="{{ request.url_for(\'delete_post\', post_id=post.id) }}"\n          class="ms-auto"\n          onsubmit="return confirm(\'Delete this post?\');"\n        >\n          <input type="hidden" name="csrf" value="{{ csrf_token }}">\n          <button\n            type="submit"\n            class="btn btn-link btn-sm text-danger text-decoration-none p-0"\n          >\n            Delete\n          </button>\n        </form>\n      {% endif %}\n    </div>\n  </div>\n</article>\n', 'post_detail.html': '{% extends "base.html" %}\n{% from "partials/comment_tree.html" import render_comments with context %}\n{% block title %}Post · SpaceBox{% endblock %}\n{% block content %}\n{% include "partials/post_card.html" %}\n\n{% if not post_is_published %}\n  <div class="alert alert-warning border-0 shadow-sm">\n    This post is scheduled for the future and is currently visible only to you.\n    Once its scheduled time arrives, it will become available to eligible viewers and comments will open.\n  </div>\n{% else %}\n  <section id="comments" class="card border-0 shadow-sm comments-card">\n    <div class="card-body p-2 p-md-3">\n      <div class="d-flex justify-content-between align-items-center mb-3">\n        <h2 class="h5 mb-0">Comment Tree</h2>\n        <span class="small text-secondary">\n          {{ post.active_comment_count }} active comments\n        </span>\n      </div>\n\n      {% if current_user %}\n        <form\n          method="post"\n          action="{{ request.url_for(\'create_comment\', post_id=post.id) }}"\n          class="comment-composer mb-4"\n        >\n          <input type="hidden" name="csrf" value="{{ csrf_token }}">\n          <textarea\n            class="form-control mb-2"\n            name="content"\n            rows="3"\n            maxlength="1000"\n            required\n            data-indentable\n            placeholder="Write a comment... (Tab inserts indentation)"\n          ></textarea>\n          <div class="d-flex justify-content-end">\n            <button class="btn btn-primary btn-sm px-3" type="submit">\n              Post Comment\n            </button>\n          </div>\n        </form>\n      {% else %}\n        <div class="alert alert-light border">Log in to join the discussion.</div>\n      {% endif %}\n\n      {% if comment_tree %}\n        <div class="comment-tree">\n          {{ render_comments(comment_tree, post, current_user, csrf_token) }}\n        </div>\n      {% else %}\n        <div class="empty-state py-4 text-center text-secondary">\n          No comments yet. Start the conversation.\n        </div>\n      {% endif %}\n    </div>\n  </section>\n{% endif %}\n{% endblock %}\n', 'profile.html': '{% extends "base.html" %}\n{% block title %}{{ profile_user.display_name }} · SpaceBox{% endblock %}\n{% block content %}\n<div class="card border-0 shadow-sm mb-4 profile-card">\n  <div class="card-body p-2 p-md-3">\n    <div class="d-flex justify-content-between gap-3 align-items-start flex-wrap">\n      <div class="d-flex gap-3 min-w-0">\n        <div class="avatar avatar-xl">\n          {{ profile_user.display_name[:1]|upper }}\n        </div>\n\n        <div class="min-w-0">\n          <h1 class="h4 mb-1">{{ profile_user.display_name }}</h1>\n          <div class="text-secondary mb-2">@{{ profile_user.username }}</div>\n          <div class="profile-bio mb-3">\n            {{ profile_user.bio or \'This user has not added a bio yet.\' }}\n          </div>\n\n          <div class="d-flex flex-wrap gap-x-3 gap-y-1 small text-secondary profile-meta">\n            <span>\n              <strong class="text-body">{{ follower_count }}</strong>\n              Followers\n            </span>\n            <span>\n              <strong class="text-body">{{ following_count }}</strong>\n              Following\n            </span>\n            <span>\n              <strong class="text-body">{{ posts|length }}</strong>\n              visible posts\n            </span>\n          </div>\n\n          <div class="small text-secondary mt-2">\n            <time\n              data-datetime="{{ profile_user.created_at.isoformat() }}"\n              data-prefix="Joined SpaceBox: "\n            ></time>\n          </div>\n\n          <div class="small mt-2">\n            <span class="text-secondary">Direct URL:</span>\n            <a\n              class="text-decoration-none"\n              href="{{ request.url_for(\'profile_short\', username=profile_user.username) }}"\n            >\n              /@{{ profile_user.username }}\n            </a>\n          </div>\n        </div>\n      </div>\n\n      <div>\n        {% if current_user and current_user.id != profile_user.id %}\n          {% if is_following %}\n            <form\n              method="post"\n              action="{{ request.url_for(\'unfollow_user\', username=profile_user.username) }}"\n            >\n              <input type="hidden" name="csrf" value="{{ csrf_token }}">\n              <button class="btn btn-outline-secondary" type="submit">\n                Unfollow\n              </button>\n            </form>\n          {% else %}\n            <form\n              method="post"\n              action="{{ request.url_for(\'follow_user\', username=profile_user.username) }}"\n            >\n              <input type="hidden" name="csrf" value="{{ csrf_token }}">\n              <button class="btn btn-primary" type="submit">Follow</button>\n            </form>\n          {% endif %}\n        {% elif current_user and current_user.id == profile_user.id %}\n          <a\n            class="btn btn-outline-secondary"\n            href="{{ request.url_for(\'settings_form\') }}"\n          >\n            Edit Profile\n          </a>\n        {% endif %}\n      </div>\n    </div>\n  </div>\n</div>\n\n<div class="d-flex justify-content-between align-items-center mb-3">\n  <h2 class="h5 mb-0">Posts</h2>\n  <span class="small text-secondary">\n    Profiles are publicly reachable; each post still enforces its own visibility rule.\n  </span>\n</div>\n\n{% if posts %}\n  {% for post in posts %}\n    {% include "partials/post_card.html" %}\n  {% endfor %}\n{% else %}\n  <div class="empty-state card border-0 shadow-sm text-center text-secondary py-5">\n    There are no posts available to you on this profile.\n  </div>\n{% endif %}\n{% endblock %}\n', 'register.html': '{% extends "base.html" %}\n{% block title %}Sign Up · SpaceBox{% endblock %}\n{% block content %}\n<div class="auth-card card border-0 shadow-sm mx-auto">\n  <div class="card-body p-4 p-md-5">\n    <h1 class="h4 mb-4">Create an Account</h1>\n    <form method="post" action="{{ request.url_for(\'register\') }}">\n      <input type="hidden" name="csrf" value="{{ csrf_token }}">\n\n      <div class="mb-3">\n        <label class="form-label">Username</label>\n        <input\n          class="form-control"\n          name="username"\n          minlength="3"\n          maxlength="32"\n          required\n          autocomplete="username"\n          placeholder="e.g. alex_01"\n        >\n        <div class="form-text">3-32 characters using letters, numbers, and underscores only.</div>\n      </div>\n\n      <div class="mb-3">\n        <label class="form-label">Display Name</label>\n        <input\n          class="form-control"\n          name="display_name"\n          maxlength="64"\n          required\n        >\n      </div>\n\n      <div class="mb-4">\n        <label class="form-label">Password</label>\n        <input\n          class="form-control"\n          name="password"\n          type="password"\n          required\n          autocomplete="new-password"\n        >\n      </div>\n\n      <button class="btn btn-primary w-100" type="submit">Sign Up</button>\n    </form>\n  </div>\n</div>\n{% endblock %}\n', 'settings.html': '{% extends "base.html" %}\n{% block title %}Settings · SpaceBox{% endblock %}\n{% block content %}\n<div class="card border-0 shadow-sm settings-card">\n  <div class="card-body p-2 p-md-3">\n    <h1 class="h4 mb-1">Account and Publishing Settings</h1>\n    <p class="text-secondary small mb-4">\n      Anyone who knows your username can open your profile. Each post is still independently controlled as\n      Public / Followers Only / Private.\n    </p>\n\n    <form method="post" action="{{ request.url_for(\'settings_update\') }}">\n      <input type="hidden" name="csrf" value="{{ csrf_token }}">\n\n      <div class="mb-3">\n        <label class="form-label fw-semibold">Username</label>\n        <input class="form-control" value="@{{ user.username }}" disabled>\n        <div class="form-text">\n          Profile: /u/{{ user.username }} or /@{{ user.username }}\n        </div>\n      </div>\n\n      <div class="mb-3">\n        <label class="form-label fw-semibold">Display Name</label>\n        <input\n          class="form-control"\n          name="display_name"\n          maxlength="64"\n          value="{{ user.display_name }}"\n          required\n        >\n      </div>\n\n      <div class="mb-3">\n        <label class="form-label fw-semibold">Bio</label>\n        <textarea\n          class="form-control"\n          name="bio"\n          rows="4"\n          maxlength="500"\n          data-indentable\n        >{{ user.bio }}</textarea>\n      </div>\n\n      <div class="mb-4">\n        <label class="form-label fw-semibold">Default Post Visibility</label>\n        <select class="form-select" name="default_post_visibility">\n          {% for visibility_key, visibility_label in visibility_labels.items() %}\n            <option\n              value="{{ visibility_key }}"\n              {% if user.default_post_visibility == visibility_key %}selected{% endif %}\n            >\n              {{ visibility_label }}\n            </option>\n          {% endfor %}\n        </select>\n      </div>\n\n      <button class="btn btn-primary" type="submit">Save Settings</button>\n    </form>\n  </div>\n</div>\n{% endblock %}\n'}
_EMBEDDED_ASSET_PAYLOADS: dict[str, str] = {'style.css': 'eNrdWcmO4zYQvfdXMDMYwE5Mjyzb3bYaCILkmkOQ5QMoibY4I4kCRdnuGfS/p7hJlCwvPckhCPrgNrd6LNbyqhwJziX6+oAQxnVFEhrzExYkZU0doYWgxXN/iiQJLWWE3u+2T8vF4+gszviBClhDyXoTJONrSCLZgcKiJFwt1KLXh4eP36NfyQtvJPr+48NDzNMXjaxgJc4o22cgdxEEh0wvnpOqwiU5xEToVTFJPqeCV3jHcqnE10Q2gkg6WWyCD1MU542YLB6r07TbXmc0z40McsJHlsoMRCzDoDqZRQkvpULbLXSLguDD82BfuGn3GVw4FqRM9bacSgCltcDKfYRwMA9WSrtqtbrgEQftZe2BgVPKXzUVqKZEJJnWjDveDg3wrzr4ZgFmZdWYR65ImoJ8nNOd7N435iIFcO7Zt9utOsCodC94U6YROhAxwTiuMVxDMiJecLyf9qQo5af8WBpBvGaS8TJCJK553kiqDvwCUFJ6UurbBGpA8ipCCcmTiVIo+gEF8zWAmqo5YV5crzN4A6dxZw32qggpg9vl/BihjKUpLRFpJO+uBgKrEwIcLO0uYm+d8JyL6YgagvnW6WdMD8o8z3QgaN3kRtUpq6ucvERol1ONcU8qdejT2p5KcrYvMZO0AGHKJ6h47p5IL4WV8LGxGzTSIYIOvqQniVOacDB6rfmSl3QEXaTdczYYnRuHbF3p/nenRSVffOvqDGuIuAZ0ZaoO6WDvuPIv9oWqG2+sdozd/9xIycva2HwsS1wJVsBuG6+UCmDQCnm/2+2e/fF47yQPYs+0v8wzg7s2aPVdlGpmL8o282MH3oFjbPOOJw0oNiPgeljs4wiFq+0MLRbrGQrD3lLzwheR2+nL0M2C6eimO8C321/ta0Kgz1lJO6sYfdf/3BN+4+P9b54N3uzzt75UT/F323hfA/fB7EO9ihB9x4qKC0lKeb7dRMubh1jYZ0fZTC35fp/bY/pK1x+4YKcJK1ENljC7hHK9/jBDEhgFzAij2XMJkTYsK+dkDQwiq/mbhyqj3C8yXI2JhMj8RwbfU8jcIq2R4jgMkq5kkAd0rIbsL7GahCwDmThz/ycctFOry7ffC9hXu++V4MDeqPtaA2mCfNJO60yDawmkztekS9iDS5hhqyUf0T9BcYMDdim3FYh+RHP1qdP1UPrIZAdlOOnjGs71QfqTPd5nOVWSk6KaQLpV2Raiznx9OKqPUJOvnhEPOeMbtmqW7V5/yFLXHUslByItiW9JEyt1anDc6QJX+tTUku0Um9A03Z9SO8H8hXJhQxyt4LBjYI5GekNX+KIlHy5o1k0sczpGHNfGLIbkx+3XPA1MOSM1q6cX+WUrbu+L0mzpaHE/BYGvQVwXvnmGw1va733C5eiWO+PUK3NW86GuuhHvnMV81Z0DLw7hpQ293pt3hRWEkD9BDcg+nA4hNGWSi9qPIGa28wtvxDlDzLgW4yoAfBSKZ5Py5ZhRoauOYwaGY+JChCpB9RI9wZWPCErARPQHViOaS5PYXm2lM1HBv+De2OsAo8bga8QqSVtxW7yCnXlbzYV7tzOhQVN5CLAE5gStcmVzJ3+w7+1uwhSafYGPb7uNgXRedS/tdaBW0HsOqhiAuq21gx0pWK79FrTOrPPXSUbTJqcpVod7Idv4V0qgrPYc7EhEqQJN38GGIQVM5zd1WgFYiTEW/S/eC5b2g4ga0ZUXfILbFTAqNY1oilK1N3bCq8ucWQ9LydHKcNOWKZ5w+28CjixxOLs4tbw8tdIXuIAXTIESOQln6m3ArSbBTN1hOr0KZInsVwnuEu2YUEabsTztRAl1W/CPEoX+UWrDSE/iThU9jRTP7xeLhS9hJ0hB+28W5zz5/HyeZL0GkEm6nnmGrg3g9wbWj643EH+iCVgoU/lBQb+MSvuAYpKq6vQYhm5T3GghYM2T8ibpqJk56d+gkRC1Dowe327j1mZUO0Q1xvLWdhYrUE9rP8PuhC9TZd03msE39lu6LHy74eLQmTDwNhMK32wZEHR+MUEa6DClJuyche2v/fbN9januK2TzTWVnPdkHCaN0hRpMoPckOrOW6lzwrJ7Y7e85Ckd9O0EBUuCksr4ldjD42s3cPfytxeElVe2ezrRBUjQ362jEZQXtwEYCmqu37vW9PlWc9MMhjefYAxYFMV0xwW90tk0EcKp1jUscWgMzXnNwCSMQsM7zKIluO/eGYgpzamExGqhjpLn1g88/WuA8Pm4Hu/9Qh5cv6F5d4dVDgjntsce5QvkJMQk8HvzE4DjO7rzNyRV4A2h1x78zVBAYAKSpEBewcHzirYM0hJENeviLsRErAOdMw7IfNhGvrDzChjBJ0ic47vcihe80CtGzwB4v9O6gjyimqoxzciBAbFSyH4yIWviEePtdjHfbqrTVB840t6HWGViyHpQnJ0FuZFy1Az6Hty+fjsRc1B44TxUTb2qa4xgXT+tfaznv5CcVZteYB86qTcF0hC6wqYuTy6vTa4spusc8Krw6wzK51DuVwf/uI7jDDj1xmYhs/gscI+E7o5U9ff0Aui5ih/7Ku4Xeq0BLf1XciCX/vu8PvwN69D95Q==', 'app.js': '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', 'bootstrap.min.css': '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', 'bootstrap.bundle.min.js': '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'}
_EMBEDDED_ASSET_TYPES = {
    "style.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "bootstrap.min.css": "text/css; charset=utf-8",
    "bootstrap.bundle.min.js": "application/javascript; charset=utf-8",
}
_ASSET_CACHE: dict[str, bytes] = {}


def _embedded_asset_bytes(asset_name: str) -> bytes:
    """Return a lazily decompressed embedded frontend asset."""
    cached = _ASSET_CACHE.get(asset_name)
    if cached is not None:
        return cached
    encoded_payload = _EMBEDDED_ASSET_PAYLOADS.get(asset_name)
    if encoded_payload is None:
        raise KeyError(asset_name)
    decoded = zlib.decompress(base64.b64decode(encoded_payload))
    _ASSET_CACHE[asset_name] = decoded
    return decoded


APP_SECRET = secrets.token_urlsafe(48)


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: object) -> None:
    """Enable SQLite foreign-key enforcement for every new connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""


def get_db() -> Generator[Session, None, None]:
    """Yield one SQLAlchemy session per request and always close it."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def utcnow() -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


follow_table = Table(
    "follows",
    Base.metadata,
    Column("follower_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("followed_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    """Registered user account and profile."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(255))
    bio: Mapped[str] = mapped_column(Text, default="")

    # Kept for backward compatibility. Profile pages themselves remain public;
    # individual posts still enforce their own visibility rules.
    profile_visibility: Mapped[str] = mapped_column(String(16), default="public")
    default_post_visibility: Mapped[str] = mapped_column(String(16), default="public")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )

    posts: Mapped[list["Post"]] = relationship(
        back_populates="author",
        cascade="all, delete-orphan",
    )
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="author",
        cascade="all, delete-orphan",
    )
    following: Mapped[list["User"]] = relationship(
        "User",
        secondary=follow_table,
        primaryjoin=id == follow_table.c.follower_id,
        secondaryjoin=id == follow_table.c.followed_id,
        back_populates="followers",
        lazy="selectin",
    )
    followers: Mapped[list["User"]] = relationship(
        "User",
        secondary=follow_table,
        primaryjoin=id == follow_table.c.followed_id,
        secondaryjoin=id == follow_table.c.follower_id,
        back_populates="following",
        lazy="selectin",
    )


class Post(Base):
    """User-authored post with optional scheduled publication and media."""

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
        default=utcnow,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
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
    media: Mapped[list["Media"]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        order_by="Media.id",
    )
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        order_by="Comment.created_at",
    )

    @property
    def active_comment_count(self) -> int:
        """Return the number of comments that are not soft-deleted."""
        return sum(not comment.is_deleted for comment in self.comments)


class Media(Base):
    """Image or video attachment stored directly in the database."""

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
    content_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # Legacy v0.1 field. New uploads are stored in content_data instead.
    file_path: Mapped[str] = mapped_column(String(255), default="")

    post: Mapped[Post] = relationship(back_populates="media")


class Comment(Base):
    """Tree-structured post comment supporting soft deletion."""

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
        default=utcnow,
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
    """Flash message stored in the signed session cookie."""

    message: str
    category: str


def hash_password(password: str) -> str:
    """Return the direct, unsalted SHA-256 hexadecimal digest of a password."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, encoded_password: str) -> bool:
    """Verify a password against its direct SHA-256 hexadecimal digest."""
    return secrets.compare_digest(hash_password(password), encoded_password.lower())


def get_current_user(request: Request, session: Session) -> User | None:
    """Return the signed-in user for a request, if one exists."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return session.get(User, int(user_id))


def require_user(request: Request, session: Session) -> User:
    """Return the signed-in user or raise HTTP 401."""
    current_user = get_current_user(request, session)
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Please log in first",
        )
    return current_user


def csrf_token(request: Request) -> str:
    """Get or create the CSRF token stored in the signed session."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def verify_csrf(request: Request, submitted_token: str) -> None:
    """Validate a submitted CSRF token using constant-time comparison."""
    expected_token = request.session.get("csrf_token", "")
    if (
        not expected_token
        or not submitted_token
        or not secrets.compare_digest(expected_token, submitted_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed",
        )


def flash(request: Request, message: str, category: str = "info") -> None:
    """Store a one-time flash message in the session."""
    request.session["flash"] = {"message": message, "category": category}


def pop_flash(request: Request) -> FlashMessage | None:
    """Return and remove the current one-time flash message."""
    return request.session.pop("flash", None)


class EmbeddedTemplates:
    """Small Jinja adapter compatible with the TemplateResponse calls below."""

    def __init__(self, template_map: dict[str, str]) -> None:
        self.environment = Environment(
            loader=DictLoader(template_map),
            autoescape=select_autoescape(("html", "xml")),
            enable_async=False,
        )

    def TemplateResponse(
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


app = FastAPI(title=APP_NAME, version=APP_VERSION)
app.add_middleware(
    SessionMiddleware,
    secret_key=APP_SECRET,
    same_site="lax",
    https_only=COOKIE_SECURE,
    max_age=SESSION_MAX_AGE_SECONDS,
)
templates = EmbeddedTemplates(EMBEDDED_TEMPLATES)


@app.get("/static/{path:path}", name="static", include_in_schema=False)
def embedded_static(path: str) -> Response:
    """Serve frontend resources embedded directly in this Python file."""
    try:
        content = _embedded_asset_bytes(path)
        media_type = _EMBEDDED_ASSET_TYPES[path]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Static asset not found") from exc

    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@dataclass(frozen=True, slots=True)
class MediaUploadPayload:
    """Validated media payload ready to persist in SQLite."""

    content_data: bytes
    media_type: str
    original_name: str
    mime_type: str
    byte_size: int


class CommentTreeNode(TypedDict):
    """Recursive template representation of a comment tree node."""

    comment: Comment
    children: list["CommentTreeNode"]


def ensure_schema() -> None:
    """Create tables and apply the small SQLite compatibility upgrades."""
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        inspector = inspect(connection)
        columns_by_table = {
            table_name: {
                column["name"]
                for column in inspector.get_columns(table_name)
            }
            for table_name in ("posts", "comments", "media")
            if inspector.has_table(table_name)
        }

        post_columns = columns_by_table.get("posts", set())
        if "scheduled_at" not in post_columns:
            connection.execute(
                text("ALTER TABLE posts ADD COLUMN scheduled_at DATETIME")
            )
        if "published_at" not in post_columns:
            connection.execute(
                text("ALTER TABLE posts ADD COLUMN published_at DATETIME")
            )
        connection.execute(
            text(
                "UPDATE posts SET published_at = created_at "
                "WHERE published_at IS NULL AND scheduled_at IS NULL"
            )
        )

        comment_columns = columns_by_table.get("comments", set())
        if "is_deleted" not in comment_columns:
            connection.execute(
                text(
                    "ALTER TABLE comments ADD COLUMN is_deleted "
                    "BOOLEAN NOT NULL DEFAULT 0"
                )
            )
        if "deleted_at" not in comment_columns:
            connection.execute(
                text("ALTER TABLE comments ADD COLUMN deleted_at DATETIME")
            )

        media_columns = columns_by_table.get("media", set())
        if "mime_type" not in media_columns:
            connection.execute(
                text(
                    "ALTER TABLE media ADD COLUMN mime_type VARCHAR(128) "
                    "NOT NULL DEFAULT 'application/octet-stream'"
                )
            )
        if "byte_size" not in media_columns:
            connection.execute(
                text(
                    "ALTER TABLE media ADD COLUMN byte_size INTEGER NOT NULL DEFAULT 0"
                )
            )
        if "content_data" not in media_columns:
            connection.execute(
                text("ALTER TABLE media ADD COLUMN content_data BLOB")
            )

    # Migrate legacy v0.1 filesystem attachments into SQLite BLOB storage.
    with engine.begin() as connection:
        legacy_media_rows = connection.execute(
            text(
                "SELECT id, file_path, original_name, media_type, content_data "
                "FROM media WHERE content_data IS NULL"
            )
        ).mappings()

        for media_row in legacy_media_rows:
            legacy_file_path = media_row.get("file_path") or ""
            if not legacy_file_path:
                continue

            legacy_media_path = BASE_DIR / legacy_file_path.lstrip("/")
            if not legacy_media_path.is_file():
                continue

            media_bytes = legacy_media_path.read_bytes()
            guessed_mime_type, _ = mimetypes.guess_type(
                media_row.get("original_name") or legacy_media_path.name
            )
            mime_type = guessed_mime_type or (
                "image/jpeg"
                if media_row.get("media_type") == "image"
                else "video/mp4"
            )
            connection.execute(
                text(
                    "UPDATE media "
                    "SET content_data=:data, mime_type=:mime, byte_size=:size "
                    "WHERE id=:id"
                ),
                {
                    "data": media_bytes,
                    "mime": mime_type,
                    "size": len(media_bytes),
                    "id": media_row["id"],
                },
            )


ensure_schema()


def build_template_context(
    request: Request,
    session: Session,
    **extra_context: Any,
) -> dict[str, Any]:
    """Build the common Jinja context shared by page responses."""
    return {
        "current_user": get_current_user(request, session),
        "csrf_token": csrf_token(request),
        "flash": pop_flash(request),
        "visibility_labels": VISIBILITY_LABELS,
        **extra_context,
    }


def redirect_to(
    url: str,
    status_code: int = status.HTTP_303_SEE_OTHER,
) -> RedirectResponse:
    """Return the application's standard post/redirect/get response."""
    return RedirectResponse(url=url, status_code=status_code)


def normalize_text(value: str) -> str:
    """Normalize newlines while preserving indentation and surrounding spaces."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def to_utc(value: datetime | None) -> datetime | None:
    """Return a datetime normalized to UTC while preserving ``None``."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_scheduled_datetime(value: str | None) -> datetime | None:
    """Parse a browser-provided schedule timestamp and normalize it to UTC."""
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
            detail="Invalid scheduled publication time",
        ) from exc

    if scheduled_datetime.tzinfo is None:
        scheduled_datetime = scheduled_datetime.replace(
            tzinfo=DEFAULT_LOCAL_TIMEZONE
        )
    return scheduled_datetime.astimezone(timezone.utc)


def publish_due_posts(session: Session) -> None:
    """Mark scheduled posts as published once their scheduled time is due."""
    current_time = utcnow()
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
        post.published_at = to_utc(post.scheduled_at) or current_time
    session.commit()


def is_post_published(post: Post) -> bool:
    """Return whether a post is currently considered published."""
    if post.published_at is not None:
        return True

    scheduled_datetime = to_utc(post.scheduled_at)
    return bool(scheduled_datetime and scheduled_datetime <= utcnow())


def get_followed_user_ids(
    session: Session,
    current_user: User | None,
) -> set[int]:
    """Return IDs of accounts followed by the current user."""
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
    """Evaluate scheduled-publication and visibility rules for one post."""
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


def build_post_query() -> Select[tuple[Post]]:
    """Build the common post query without eagerly loading media BLOBs."""
    return select(Post).options(
        joinedload(Post.author),
        selectinload(Post.media).defer(Media.content_data),
        selectinload(Post.comments).joinedload(Comment.author),
    )


def build_comment_tree(comments: list[Comment]) -> list[CommentTreeNode]:
    """Build an O(n) comment tree while preserving soft-deleted parents."""
    ordered_comments = sorted(
        comments,
        key=lambda comment: (
            to_utc(comment.created_at) or utcnow(),
            comment.id,
        ),
    )
    nodes_by_id: dict[int, CommentTreeNode] = {
        comment.id: {"comment": comment, "children": []}
        for comment in ordered_comments
    }
    root_nodes: list[CommentTreeNode] = []

    for comment in ordered_comments:
        current_node = nodes_by_id[comment.id]
        parent_node = (
            nodes_by_id.get(comment.parent_id)
            if comment.parent_id is not None
            else None
        )
        if parent_node is not None:
            parent_node["children"].append(current_node)
        else:
            root_nodes.append(current_node)

    return root_nodes


async def read_upload_payload(upload: UploadFile) -> MediaUploadPayload | None:
    """Validate and read an uploaded image or video into memory."""
    if not upload.filename:
        return None

    file_extension = Path(upload.filename).suffix.lower()
    mime_type = (upload.content_type or "").lower()
    if mime_type.startswith("image/") and file_extension in ALLOWED_IMAGE_EXTENSIONS:
        media_type = "image"
    elif mime_type.startswith("video/") and file_extension in ALLOWED_VIDEO_EXTENSIONS:
        media_type = "video"
    else:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type: {upload.filename}. "
                "Only common image formats and MP4/WebM/MOV videos are allowed."
            ),
        )

    file_buffer = bytearray()
    try:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break

            file_buffer.extend(chunk)
            if len(file_buffer) > MAX_MEDIA_FILE_SIZE_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=f"File {upload.filename} exceeds 30 MB",
                )
    finally:
        await upload.close()

    media_bytes = bytes(file_buffer)
    return MediaUploadPayload(
        content_data=media_bytes,
        media_type=media_type,
        original_name=upload.filename,
        mime_type=mime_type,
        byte_size=len(media_bytes),
    )


def lcs_length(left: str, right: str) -> int:
    """Return the longest common subsequence length using one-dimensional DP."""
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
    """Calculate an LCS-based username similarity score and matched length."""
    normalized_query = query.casefold()
    normalized_username = username.casefold()
    common_subsequence_length = lcs_length(
        normalized_query,
        normalized_username,
    )
    if common_subsequence_length == 0:
        return 0.0, 0

    # LCS remains the primary signal. Prefix and substring matches only make
    # the ordering more intuitive when multiple usernames have similar LCS.
    score = (
        (common_subsequence_length / len(normalized_query)) * 0.75
        + (common_subsequence_length / len(normalized_username)) * 0.25
    )
    if normalized_username.startswith(normalized_query):
        score += 0.60
    elif normalized_query in normalized_username:
        score += 0.30
    if normalized_username == normalized_query:
        score += 1.00

    return score, common_subsequence_length


@app.get("/", response_class=HTMLResponse, name="home")
def home(request: Request, session: Session = Depends(get_db)) -> Response:
    publish_due_posts(session)
    current_user = get_current_user(request, session)
    followed_user_ids = get_followed_user_ids(session, current_user)
    recent_posts = (
        session.scalars(
            build_post_query()
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
    visible_posts = [
        post
        for post in recent_posts
        if can_view_post(current_user, post, followed_user_ids)
    ][:60]
    return templates.TemplateResponse(
        request=request,
        name="feed.html",
        context=build_template_context(request, session, posts=visible_posts),
    )


@app.get("/register", response_class=HTMLResponse, name="register_form")
def register_form(request: Request, session: Session = Depends(get_db)) -> Response:
    if get_current_user(request, session):
        return redirect_to("/")
    return templates.TemplateResponse(
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
    session: Session = Depends(get_db),
) -> RedirectResponse:
    verify_csrf(request, csrf)

    normalized_username = username.strip().lower()
    normalized_display_name = display_name.strip()
    username_length_is_valid = (
        MIN_USERNAME_LENGTH <= len(normalized_username) <= MAX_USERNAME_LENGTH
    )
    username_characters_are_valid = normalized_username.replace("_", "").isalnum()

    if not username_length_is_valid or not username_characters_are_valid:
        flash(request, "Username must be 3-32 characters using letters, numbers, or underscores.", "danger")
        return redirect_to("/register")
    if (
        not normalized_display_name
        or len(normalized_display_name) > MAX_DISPLAY_NAME_LENGTH
    ):
        flash(request, "Display name is required and must not exceed 64 characters.", "danger")
        return redirect_to("/register")
    if session.scalar(
        select(User).where(User.username == normalized_username)
    ):
        flash(request, "That username is already taken.", "danger")
        return redirect_to("/register")

    new_user = User(
        username=normalized_username,
        display_name=normalized_display_name,
        password_hash=hash_password(password),
    )
    session.add(new_user)
    session.commit()
    session.refresh(new_user)

    request.session.clear()
    request.session["user_id"] = new_user.id
    csrf_token(request)
    flash(request, "Registration complete. Welcome to SpaceBox!", "success")
    return redirect_to("/")


@app.get("/login", response_class=HTMLResponse, name="login_form")
def login_form(request: Request, session: Session = Depends(get_db)) -> Response:
    if get_current_user(request, session):
        return redirect_to("/")
    return templates.TemplateResponse(
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
    session: Session = Depends(get_db),
) -> RedirectResponse:
    verify_csrf(request, csrf)

    normalized_username = username.strip().lower()
    authenticated_user = session.scalar(
        select(User).where(User.username == normalized_username)
    )
    if authenticated_user is None or not verify_password(
        password,
        authenticated_user.password_hash,
    ):
        flash(request, "Incorrect username or password.", "danger")
        return redirect_to("/login")

    request.session.clear()
    request.session["user_id"] = authenticated_user.id
    csrf_token(request)
    flash(request, "Logged in successfully.", "success")
    return redirect_to("/")


@app.post("/logout", name="logout")
def logout(request: Request, csrf: Annotated[str, Form()]) -> RedirectResponse:
    verify_csrf(request, csrf)
    request.session.clear()
    return redirect_to("/")


@app.get("/api/users/search", response_class=JSONResponse, name="search_users")
def search_users(
    search_term: Annotated[str, Query(alias="q")] = "",
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    normalized_query = search_term.strip().casefold()[:MAX_USERNAME_LENGTH]
    if not normalized_query:
        return {"query": "", "results": []}

    candidate_users = session.scalars(
        select(User).order_by(User.username).limit(MAX_SEARCH_CANDIDATES)
    ).all()
    ranked_users: list[tuple[float, int, User]] = []

    for candidate_user in candidate_users:
        score, common_subsequence_length = calculate_username_search_score(
            normalized_query,
            candidate_user.username,
        )
        minimum_match_length = max(1, (len(normalized_query) + 1) // 2)
        if common_subsequence_length < minimum_match_length:
            continue
        ranked_users.append(
            (score, common_subsequence_length, candidate_user)
        )

    ranked_users.sort(
        key=lambda ranked_user: (
            -ranked_user[0],
            -ranked_user[1],
            len(ranked_user[2].username),
            ranked_user[2].username,
        )
    )

    return {
        "query": normalized_query,
        "results": [
            {
                "username": candidate_user.username,
                "display_name": candidate_user.display_name,
                "profile_url": f"/u/{quote(candidate_user.username)}",
                "lcs": common_subsequence_length,
                "score": round(score, 4),
            }
            for score, common_subsequence_length, candidate_user in ranked_users[
                :MAX_SEARCH_RESULTS
            ]
        ],
    }


@app.get("/post/new", response_class=HTMLResponse, name="new_post_form")
def new_post_form(request: Request, session: Session = Depends(get_db)) -> Response:
    current_user = require_user(request, session)
    return templates.TemplateResponse(
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
    session: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = require_user(request, session)
    verify_csrf(request, csrf)

    normalized_content = normalize_text(content)
    selected_visibility = (
        visibility
        if visibility in POST_VISIBILITIES
        else current_user.default_post_visibility
    )
    uploaded_files = [
        upload
        for upload in (files or [])
        if upload.filename
    ]

    if not normalized_content.strip() and not uploaded_files:
        flash(request, "A post must contain text or at least one attachment.", "danger")
        return redirect_to("/post/new")
    if len(normalized_content) > MAX_POST_LENGTH:
        flash(request, "Post content must not exceed 5,000 characters.", "danger")
        return redirect_to("/post/new")
    if len(uploaded_files) > MAX_MEDIA_FILES_PER_POST:
        flash(
            request,
            f"Each post can contain at most {MAX_MEDIA_FILES_PER_POST} attachments.",
            "danger",
        )
        return redirect_to("/post/new")

    try:
        scheduled_datetime = parse_scheduled_datetime(scheduled_at_utc)
    except HTTPException as exc:
        flash(request, str(exc.detail), "danger")
        return redirect_to("/post/new")

    current_time = utcnow()
    if scheduled_datetime and scheduled_datetime <= current_time:
        scheduled_datetime = None
    published_datetime = None if scheduled_datetime else current_time

    media_payloads: list[MediaUploadPayload] = []
    try:
        for uploaded_file in uploaded_files:
            media_payload = await read_upload_payload(uploaded_file)
            if media_payload is not None:
                media_payloads.append(media_payload)
    except HTTPException as exc:
        flash(request, str(exc.detail), "danger")
        return redirect_to("/post/new")

    new_post = Post(
        author_id=current_user.id,
        content=normalized_content,
        visibility=selected_visibility,
        scheduled_at=scheduled_datetime,
        published_at=published_datetime,
    )
    session.add(new_post)
    session.flush()

    for media_payload in media_payloads:
        session.add(
            Media(
                post_id=new_post.id,
                media_type=media_payload.media_type,
                original_name=media_payload.original_name,
                mime_type=media_payload.mime_type,
                byte_size=media_payload.byte_size,
                content_data=media_payload.content_data,
                file_path="",
            )
        )
    session.commit()

    if scheduled_datetime:
        flash(request, "Post scheduled successfully.", "success")
    else:
        flash(request, "Post published successfully.", "success")
    return redirect_to(f"/post/{new_post.id}")


@app.get("/media/{media_id}", name="media_content")
def media_content(
    media_id: int,
    request: Request,
    session: Session = Depends(get_db),
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
            detail="Media not found",
        )

    current_user = get_current_user(request, session)
    followed_user_ids = get_followed_user_ids(session, current_user)
    if not can_view_post(
        current_user,
        media_record.post,
        followed_user_ids,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this media",
        )

    media_bytes = media_record.content_data
    if media_bytes is None and media_record.file_path:
        legacy_media_path = BASE_DIR / media_record.file_path.lstrip("/")
        if legacy_media_path.is_file():
            media_bytes = legacy_media_path.read_bytes()
    if media_bytes is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media data not found",
        )

    total_bytes = len(media_bytes)
    mime_type = media_record.mime_type or "application/octet-stream"
    common_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=300",
        "X-Content-Type-Options": "nosniff",
    }
    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes="):
        try:
            range_value = range_header.removeprefix("bytes=").split(",", 1)[0]
            start_text, end_text = range_value.split("-", 1)
            if start_text:
                range_start = int(start_text)
                range_end = int(end_text) if end_text else total_bytes - 1
            else:
                suffix_length = int(end_text)
                range_start = max(0, total_bytes - suffix_length)
                range_end = total_bytes - 1

            if (
                range_start < 0
                or range_end < range_start
                or range_start >= total_bytes
            ):
                raise ValueError
            range_end = min(range_end, total_bytes - 1)
        except (TypeError, ValueError):
            return Response(
                status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                headers={"Content-Range": f"bytes */{total_bytes}"},
            )

        media_chunk = media_bytes[range_start : range_end + 1]
        partial_headers = {
            **common_headers,
            "Content-Range": (
                f"bytes {range_start}-{range_end}/{total_bytes}"
            ),
            "Content-Length": str(len(media_chunk)),
        }
        return Response(
            content=media_chunk,
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            media_type=mime_type,
            headers=partial_headers,
        )

    return Response(
        content=media_bytes,
        media_type=mime_type,
        headers={
            **common_headers,
            "Content-Length": str(total_bytes),
        },
    )


@app.get("/post/{post_id}", response_class=HTMLResponse, name="post_detail")
def post_detail(
    post_id: int,
    request: Request,
    session: Session = Depends(get_db),
) -> Response:
    publish_due_posts(session)
    post = session.scalar(build_post_query().where(Post.id == post_id))
    if post is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found",
        )

    current_user = get_current_user(request, session)
    followed_user_ids = get_followed_user_ids(session, current_user)
    if not can_view_post(current_user, post, followed_user_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this post",
        )

    return templates.TemplateResponse(
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
    session: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = require_user(request, session)
    verify_csrf(request, csrf)
    publish_due_posts(session)

    post = session.scalar(build_post_query().where(Post.id == post_id))
    if post is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found",
        )
    if not is_post_published(post):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Scheduled posts cannot receive comments before publication",
        )

    followed_user_ids = get_followed_user_ids(session, current_user)
    if not can_view_post(current_user, post, followed_user_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to comment on this post",
        )

    normalized_content = normalize_text(content)
    if (
        not normalized_content.strip()
        or len(normalized_content) > MAX_COMMENT_LENGTH
    ):
        flash(request, "Comments cannot be empty and must not exceed 1,000 characters.", "danger")
        return redirect_to(f"/post/{post_id}")

    if parent_id is not None:
        parent_comment = session.get(Comment, parent_id)
        if parent_comment is None or parent_comment.post_id != post_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid reply target",
            )
        if parent_comment.is_deleted:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Deleted comments cannot receive new replies",
            )

    new_comment = Comment(
        post_id=post_id,
        author_id=current_user.id,
        parent_id=parent_id,
        content=normalized_content,
    )
    session.add(new_comment)
    session.commit()
    flash(request, "Comment posted successfully.", "success")
    return redirect_to(f"/post/{post_id}#comments")


@app.post("/comment/{comment_id}/delete", name="delete_comment")
def delete_comment(
    comment_id: int,
    request: Request,
    csrf: Annotated[str, Form()],
    session: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = require_user(request, session)
    verify_csrf(request, csrf)

    comment = session.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Comment not found",
        )
    if comment.author_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own comments",
        )

    post_id = comment.post_id
    if not comment.is_deleted:
        comment.is_deleted = True
        comment.deleted_at = utcnow()
        # Soft deletion intentionally preserves content and parent_id in the DB.
        session.commit()
        flash(
            request,
            "Comment marked as deleted. Replies beneath it have been preserved.",
            "success",
        )

    return redirect_to(f"/post/{post_id}#comment-{comment.id}")


@app.post("/post/{post_id}/delete", name="delete_post")
def delete_post(
    post_id: int,
    request: Request,
    csrf: Annotated[str, Form()],
    session: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = require_user(request, session)
    verify_csrf(request, csrf)

    post = session.scalar(select(Post).where(Post.id == post_id))
    if post is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found",
        )
    if post.author_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own posts",
        )

    session.delete(post)
    session.commit()
    flash(request, "Post deleted successfully.", "success")
    return redirect_to("/")


def render_profile_page(
    username: str,
    request: Request,
    session: Session,
) -> Response:
    """Render a public profile shell with posts filtered for the viewer."""
    publish_due_posts(session)
    profile_user = session.scalar(
        select(User).where(User.username == username.lower())
    )
    if profile_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    current_user = get_current_user(request, session)
    followed_user_ids = get_followed_user_ids(session, current_user)
    profile_posts = (
        session.scalars(
            build_post_query()
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
    visible_posts = [
        post
        for post in profile_posts
        if can_view_post(current_user, post, followed_user_ids)
    ]

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

    return templates.TemplateResponse(
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
    session: Session = Depends(get_db),
) -> Response:
    return render_profile_page(username, request, session)


@app.get("/@{username}", response_class=HTMLResponse, name="profile_short")
def profile_short(
    username: str,
    request: Request,
    session: Session = Depends(get_db),
) -> Response:
    return render_profile_page(username, request, session)


@app.post("/u/{username}/follow", name="follow_user")
def follow_user(
    username: str,
    request: Request,
    csrf: Annotated[str, Form()],
    session: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = require_user(request, session)
    verify_csrf(request, csrf)

    target_user = session.scalar(
        select(User).where(User.username == username.lower())
    )
    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    if target_user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot follow yourself",
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
        session.commit()

    flash(request, f"Now following @{target_user.username}", "success")
    return redirect_to(f"/u/{target_user.username}")


@app.post("/u/{username}/unfollow", name="unfollow_user")
def unfollow_user(
    username: str,
    request: Request,
    csrf: Annotated[str, Form()],
    session: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = require_user(request, session)
    verify_csrf(request, csrf)

    target_user = session.scalar(
        select(User).where(User.username == username.lower())
    )
    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    session.execute(
        delete(follow_table).where(
            follow_table.c.follower_id == current_user.id,
            follow_table.c.followed_id == target_user.id,
        )
    )
    session.commit()
    flash(request, f"Unfollowed @{target_user.username}", "success")
    return redirect_to(f"/u/{target_user.username}")


@app.get("/settings", response_class=HTMLResponse, name="settings_form")
def settings_form(
    request: Request,
    session: Session = Depends(get_db),
) -> Response:
    current_user = require_user(request, session)
    return templates.TemplateResponse(
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
    session: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = require_user(request, session)
    verify_csrf(request, csrf)

    normalized_display_name = display_name.strip()
    normalized_bio = normalize_text(bio)
    if (
        not normalized_display_name
        or len(normalized_display_name) > MAX_DISPLAY_NAME_LENGTH
    ):
        flash(request, "Display name is required and must not exceed 64 characters.", "danger")
        return redirect_to("/settings")
    if len(normalized_bio) > MAX_BIO_LENGTH:
        flash(request, "Bio must not exceed 500 characters.", "danger")
        return redirect_to("/settings")
    if default_post_visibility not in POST_VISIBILITIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid visibility setting",
        )

    current_user.display_name = normalized_display_name
    current_user.bio = normalized_bio
    current_user.default_post_visibility = default_post_visibility
    current_user.profile_visibility = "public"
    session.commit()

    flash(request, "Settings saved successfully.", "success")
    return redirect_to("/settings")


@app.exception_handler(HTTPException)
def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        return redirect_to("/login")

    return templates.TemplateResponse(
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
    """Find an available loopback TCP port, preferring port 8000."""
    for port in range(start_port, start_port + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
            try:
                probe_socket.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("No available local port found in the range 8000-8049.")


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url, new=2)
    except Exception:
        pass


def main() -> None:
    """Start SpaceBox with no CLI flags or environment configuration required."""
    host = "127.0.0.1"
    port = _find_available_port()
    url = f"http://{host}:{port}"
    print("=" * 68)
    print(f"{APP_NAME} {APP_VERSION}")
    print(f"Database : {DATABASE_PATH}")
    print(f"Open     : {url}")
    print("Press Ctrl+C to stop.")
    print("=" * 68)

    browser_timer = threading.Timer(0.8, _open_browser, args=(url,))
    browser_timer.daemon = True
    browser_timer.start()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
