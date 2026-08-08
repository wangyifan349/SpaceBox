<div align="center">

# 🧡 SpaceBox

### A lightweight, self-contained social publishing platform built with FastAPI and SQLite.

**Posts · Media · Scheduled Publishing · Nested Comments · Privacy · Following · Live Search**

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.128%2B-009688?logo=fastapi&logoColor=white)](https://github.com/fastapi/fastapi)
[![SQLite](https://img.shields.io/badge/SQLite-3-003B57?logo=sqlite&logoColor=white)](https://github.com/sqlite/sqlite)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue)](LICENSE)

</div>

---

## ✨ About SpaceBox

**SpaceBox** is a compact social media and personal-space application inspired by timeline and microblog platforms.

Users can create accounts, publish text posts, attach images or videos, schedule future posts, control who can see each post, follow other users, browse public profiles, search usernames in real time, and participate in deeply nested comment discussions.

The project is intentionally simple to deploy. The complete application lives in a single Python file:

```text
spacebox_standalone.py
```

That file contains the FastAPI backend, SQLAlchemy models, Jinja2 templates, Bootstrap assets, project CSS, JavaScript, authentication logic, media handling, search, comments, and Uvicorn startup code.

No separate frontend build step, `templates/` directory, `static/` directory, environment-variable configuration, or external database server is required.

**Project repository:** https://github.com/wangyifan349/SpaceBox

---

## 🚀 Quick Start

Python **3.11 or newer** is recommended.

### Option 1 — Install all dependencies directly

```bash
git clone https://github.com/wangyifan349/SpaceBox.git
cd SpaceBox
pip install "fastapi>=0.128,<1.0" "uvicorn[standard]>=0.30" "SQLAlchemy>=2.0,<2.1" "Jinja2>=3.1" "python-multipart>=0.0.20" "itsdangerous>=2.2" "tzdata>=2024.1"
python3 spacebox_standalone.py
```

### Option 2 — Use `requirements.txt`

```bash
git clone https://github.com/wangyifan349/SpaceBox.git
cd SpaceBox
pip install -r requirements.txt
python3 spacebox_standalone.py
```

SpaceBox prefers to start at:

```text
http://127.0.0.1:8000
```

If port `8000` is already in use, the application automatically searches for an available port between `8000` and `8049`.

On the first launch, SpaceBox automatically creates:

```text
social.db
```

in the same directory as `spacebox_standalone.py`.

---

## 🌟 Features

### 👤 Accounts and Profiles

- User registration, login, and logout.
- Unique usernames and separate display names.
- User biography support.
- Account creation timestamps.
- Public profile pages reachable by username.
- Standard profile route: `/u/username`.
- Short profile route: `/@username`.
- Follow and unfollow support.
- Configurable default visibility for new posts.
- Profile statistics for followers, following, visible posts, and join time.

### 📝 Posts

- Publish text-only posts.
- Attach images and videos.
- Preserve spaces, tabs, line breaks, and indentation.
- Press `Tab` inside supported text areas to insert indentation.
- Up to 5,000 characters per post.
- Immediate publishing.
- Scheduled publishing.
- Creation, update, scheduled, and publication timestamps.
- Post deletion by the author.

### 👁️ Post Visibility

Every post stores one visibility mode:

| Value | Meaning | Who Can View It |
| --- | --- | --- |
| `public` | Public | Everyone |
| `followers` | Followers Only | The author and users who follow the author |
| `private` | Private | The author only |

Visibility is enforced by the backend.

Media requests also verify the visibility of the post that owns the media, so directly requesting a media ID does not bypass post permissions.

### ⏰ Scheduled Publishing

Scheduled posts use two fields:

- `scheduled_at` — requested publication time.
- `published_at` — actual publication time.

Typical states:

```text
scheduled_at = NULL
published_at = timestamp
    -> immediately published

scheduled_at = future timestamp
published_at = NULL
    -> waiting for scheduled publication

scheduled_at = timestamp
published_at = timestamp
    -> scheduled post already published
```

Before publication, a scheduled post is visible only to its author and cannot receive comments.

### 🖼️ Images and Videos

- Up to 6 media attachments per post.
- Maximum size of 30 MB per attachment.
- Image formats: JPG, JPEG, PNG, GIF, WebP.
- Video formats: MP4, WebM, MOV, M4V.
- Media binary content is stored directly in SQLite using a `BLOB` column.
- Uploads are not written to a separate media directory.
- Media is served through a dedicated FastAPI route.
- Video responses support HTTP Range requests for seeking and partial playback.

### 🌳 Nested Comment Tree

Comments can reply to other comments at arbitrary depth.

```text
Comment A
├── Reply B
│   └── Reply C
└── Reply D
```

The tree is represented by the self-referencing `comments.parent_id` field:

```text
B.parent_id = A.id
C.parent_id = B.id
D.parent_id = A.id
```

Comment deletion uses **soft deletion**.

Instead of deleting the row from SQLite, SpaceBox marks it as deleted and records a deletion timestamp. Descendant replies remain attached to the same node:

```text
[This comment was deleted by its author.]
├── Reply B
│   └── Reply C
└── Reply D
```

This keeps the discussion tree structurally intact.

### 🔎 Real-Time User Search

The navigation bar includes a live username search dropdown.

- Username ranking uses **Longest Common Subsequence (LCS)** similarity.
- Up to 10,000 candidate accounts can be evaluated per search request.
- Up to 8 results are returned.
- Client-side debounce reduces unnecessary requests.
- Mouse selection is supported.
- `Arrow Up` / `Arrow Down` navigate results.
- `Enter` opens the selected profile.
- `Esc` closes the dropdown.

### 🎨 Frontend

- Responsive Bootstrap interface.
- Wide post and discussion layout.
- Orange action-button theme.
- Embedded Bootstrap CSS and JavaScript.
- Embedded application CSS and JavaScript.
- No frontend build step.
- Browser-local timestamp rendering.
- Relative-time labels where appropriate.

---

## 🧩 Project Structure

```text
SpaceBox/
├── spacebox_standalone.py   # Complete backend + embedded frontend
├── requirements.txt         # Runtime dependencies
├── README.md                # Project documentation
├── LICENSE                  # GNU AGPL-3.0 license
└── .gitignore               # Runtime/cache/editor exclusions
```

After the first launch:

```text
SpaceBox/
├── spacebox_standalone.py
└── social.db
```

The `social.db` file contains the application data, including uploaded image and video binaries.

---

## ⚙️ Application Architecture

```text
Browser
   |
   v
FastAPI
   |
   +-- Jinja2 page rendering
   +-- Embedded Bootstrap / CSS / JavaScript
   +-- Session and CSRF handling
   +-- User and profile routes
   +-- Post and visibility routes
   +-- Comment-tree routes
   +-- LCS search routes
   +-- Media streaming routes
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

SQLite foreign-key enforcement is enabled for database connections:

```sql
PRAGMA foreign_keys=ON;
```

Normal persistence uses SQLAlchemy ORM. Direct SQL operations use parameterized queries.

---

# 🗄️ Database Design

SpaceBox stores persistent application state in one SQLite database file:

```text
social.db
```

The core schema contains five tables:

1. `users`
2. `posts`
3. `media`
4. `comments`
5. `follows`

## 🔗 Relationship Overview

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

In practical terms:

- One user can author many posts.
- One user can author many comments.
- One post can contain many media attachments.
- One post can contain many comments.
- One comment can reference another comment as its parent.
- Users connect to other users through the `follows` association table.

---

## 👤 `users` Table

Stores account and profile information.

| Column | Type | Constraint / Default | Purpose |
| --- | --- | --- | --- |
| `id` | INTEGER | Primary Key | Internal user identifier |
| `username` | VARCHAR(32) | UNIQUE, INDEX | Unique account name used in profile URLs |
| `display_name` | VARCHAR(64) | Required | Human-readable profile name |
| `password_hash` | VARCHAR(255) | Required | Stored login credential digest |
| `bio` | TEXT | Default empty string | Profile biography |
| `profile_visibility` | VARCHAR(16) | Default `public` | Profile visibility field |
| `default_post_visibility` | VARCHAR(16) | Default `public` | Default visibility for new posts |
| `created_at` | DATETIME | Automatically populated | Account creation time |

Username length is limited to 3–32 characters and accepts letters, numbers, and underscores.

Display names are limited to 64 characters, and biographies are limited to 500 characters.

---

## 📝 `posts` Table

Stores post text, ownership, visibility, and publication timing.

| Column | Type | Constraint / Default | Purpose |
| --- | --- | --- | --- |
| `id` | INTEGER | Primary Key | Post identifier |
| `author_id` | INTEGER | FK → `users.id`, INDEX | Post author |
| `content` | TEXT | Default empty string | Post body with preserved whitespace |
| `visibility` | VARCHAR(16) | Default `public`, INDEX | `public`, `followers`, or `private` |
| `created_at` | DATETIME | INDEX | Post creation time |
| `updated_at` | DATETIME | Automatically updated | Most recent modification time |
| `scheduled_at` | DATETIME | NULL, INDEX | Requested scheduled publication time |
| `published_at` | DATETIME | NULL, INDEX | Actual publication time |

`author_id` references `users.id` and uses `ON DELETE CASCADE`.

---

## 🖼️ `media` Table

Stores image and video attachments.

| Column | Type | Constraint / Default | Purpose |
| --- | --- | --- | --- |
| `id` | INTEGER | Primary Key | Media identifier |
| `post_id` | INTEGER | FK → `posts.id`, INDEX | Owning post |
| `media_type` | VARCHAR(16) | Required | `image` or `video` |
| `original_name` | VARCHAR(255) | Default empty string | Original uploaded filename |
| `mime_type` | VARCHAR(128) | Default `application/octet-stream` | HTTP content type |
| `byte_size` | INTEGER | Default `0` | Binary size in bytes |
| `content_data` | BLOB | Nullable | Image or video binary data |
| `file_path` | VARCHAR(255) | Default empty string | Reserved path field; current uploads use BLOB storage |

The actual file content is stored in:

```text
media.content_data
```

When the browser requests `/media/{media_id}`, SpaceBox:

1. Loads the media record.
2. Resolves its owning post through `post_id`.
3. Applies the post visibility rules.
4. Returns the binary payload only when access is allowed.

`post_id` references `posts.id` with `ON DELETE CASCADE`.

---

## 💬 `comments` Table

Stores comments and the full nested discussion tree.

| Column | Type | Constraint / Default | Purpose |
| --- | --- | --- | --- |
| `id` | INTEGER | Primary Key | Comment identifier |
| `post_id` | INTEGER | FK → `posts.id`, INDEX | Post containing the comment |
| `author_id` | INTEGER | FK → `users.id`, INDEX | Comment author |
| `parent_id` | INTEGER | FK → `comments.id`, NULL, INDEX | Parent comment; `NULL` for a root comment |
| `content` | TEXT | Required | Comment body |
| `created_at` | DATETIME | INDEX | Comment creation time |
| `is_deleted` | BOOLEAN | Default `false`, INDEX | Soft-deletion state |
| `deleted_at` | DATETIME | NULL | Soft-deletion timestamp |

A root-level comment has:

```text
parent_id = NULL
```

A reply has:

```text
parent_id = <parent comment id>
```

Comment bodies are limited to 1,000 characters.

Normal user-triggered deletion does **not** execute a SQL `DELETE`. The row remains so its `id` can continue acting as the parent for descendant replies.

Foreign keys:

- `post_id` → `posts.id` with `ON DELETE CASCADE`
- `author_id` → `users.id` with `ON DELETE CASCADE`
- `parent_id` → `comments.id` with `ON DELETE CASCADE`

The self-referencing cascade applies only when the database row itself is actually deleted. It is not triggered by the normal application soft-delete operation.

---

## 🤝 `follows` Table

Represents the many-to-many following relationship between users.

| Column | Type | Constraint | Purpose |
| --- | --- | --- | --- |
| `follower_id` | INTEGER | Composite PK, FK → `users.id` | User who follows another account |
| `followed_id` | INTEGER | Composite PK, FK → `users.id` | User being followed |

Together they form the composite primary key:

```text
(follower_id, followed_id)
```

This prevents duplicate follow relationships.

Example:

```text
follower_id = 12
followed_id = 35
```

means user `12` follows user `35`.

Both foreign keys use `ON DELETE CASCADE`.

---

## 🔐 Foreign-Key Summary

```text
posts.author_id       -> users.id
media.post_id         -> posts.id
comments.post_id      -> posts.id
comments.author_id    -> users.id
comments.parent_id    -> comments.id
follows.follower_id   -> users.id
follows.followed_id   -> users.id
```

Because `PRAGMA foreign_keys=ON` is enabled, these relationships are active SQLite constraints rather than documentation-only relationships.

---

## 🛠️ Database Initialization

No separate database server, migration service, or manual schema script is required for a fresh installation.

At startup, SQLAlchemy creates the required tables when they do not already exist.

The database file is located at:

```text
<SpaceBox directory>/social.db
```

That single file stores:

- user accounts and profiles;
- posts and visibility settings;
- scheduled publication information;
- media metadata and image/video BLOB content;
- comments and comment-tree relationships;
- comment soft-deletion state;
- follow relationships.

Because media binaries are stored directly in SQLite, `social.db` grows as users upload images and videos. This keeps small deployments easy to back up and move because application data remains centralized in one file.

---

## 🕒 Time Handling

Application timestamps use UTC-based datetime values for:

- account creation;
- post creation;
- post updates;
- scheduled publication;
- actual publication;
- comment creation;
- comment deletion.

The browser converts timestamps to local time for display.

---

## 📦 Single-File Runtime

`spacebox_standalone.py` contains the complete runtime application:

- application constants and internal configuration;
- SQLite and SQLAlchemy initialization;
- ORM models;
- session authentication;
- CSRF handling;
- post visibility checks;
- account and profile routes;
- post routes;
- media routes;
- comment routes;
- LCS username search;
- Jinja2 templates;
- Bootstrap assets;
- project CSS;
- project JavaScript;
- Uvicorn startup logic.

After the Python packages are installed, no frontend compilation or asset deployment step is required.

---

## 📚 Dependencies

`requirements.txt` contains:

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

## 📄 License

SpaceBox is released under the **GNU Affero General Public License v3.0 (AGPL-3.0)**.

See [`LICENSE`](LICENSE) for the complete license text.

The embedded Bootstrap distribution remains subject to Bootstrap's own MIT License notice, which is preserved in the application source.

---

<div align="center">

## 🧡 Built With

⚡ **FastAPI** — modern Python web framework  
[GitHub: fastapi/fastapi](https://github.com/fastapi/fastapi)

🗄️ **SQLite** — embedded relational database engine  
[GitHub: sqlite/sqlite](https://github.com/sqlite/sqlite)

🐍 **Python** · 🧱 **SQLAlchemy** · 🎨 **Bootstrap** · 🧩 **Jinja2**

<br>

**SpaceBox — simple social publishing in one Python file.** 🚀

</div>
