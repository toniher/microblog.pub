# User's guide

```{contents}
:local:
:depth: 2
```

## ActivityPub

Using microblog.pub efficiently requires knowing a bit about how [ActivityPub](https://activitypub.rocks/) works.

Skimming over the [Overview section of the ActivityPub specification](https://www.w3.org/TR/activitypub/#Overview) should be enough.

Also, you should know that the **Fediverse** is a common name used to describe all the interconnected/federated instances of servers supporting ActivityPub (like Mastodon, Pleroma, PeerTube, PixelFed...).

## Configuration

### Profile

You initial profile configuration is generated via the setup wizard.

You can manually edit the configuration file stored in `data/profile.toml` ([TOML](https://toml.io/en/)), note that the following config items cannot be updated (without breaking federation):

 - `domain`
 - `username`

As these two config items define your ActivityPub handle `@handle@domain`.

You can tweak your profile by tweaking these items:

 - `name`: The name shown with your profile.
 - `summary`: The summary or 'bio' part of your profile, written in Markdown.
 - `icon_url`: Your profile image or avatar.
 - `image_url`: This provides a 'header' or 'banner' image. Note that it is not shown by the default Microblog.pub templates. It will be used by Mastodon (which uses a 3:1 ratio image) and Pleroma. Pixelfed and Peertube, for example, don't show these images by default.

Whenever one of these config items is updated, an `Update` activity will be sent to all known servers to update your remote profile.

The server will need to be restarted for taking changes into account.

Before restarting the server, you can ensure you haven't made any mistakes by running the [configuration checking task](#configuration-checking).

Note that currently `image_url` is not used anywhere in microblog.pub itself, but other clients/servers do occasionally use it when showing remote profiles as a background image.
Also, this image _can_ be used in microblog.pub - just add this:

```html
<img src="{{ local_actor.image_url | media_proxy_url }}">
```

to an appropriate place of your template (most likely, `header.html`).
For more information, see a section about [custom templates](#custom-templates) further in this document.

#### Local avatar/header files

If you'd rather not host your avatar/header elsewhere and reference it with an
absolute URL, you can drop image files directly into the instance and let it
serve them. The avatar and header are resolved in this priority order:

1. **`icon_url` / `image_url` in `data/profile.toml`** — an explicit URL here
   always wins. If set, the files below are ignored.
2. **`data/avatar.jpg` / `data/profile.image.jpg`** — your own files, kept with
   the rest of your mutable data (and therefore preserved across upgrades and
   Docker image rebuilds).
3. **`app/static/avatar.jpg` / `app/static/profile.image.jpg`** — packaged
   defaults shipped with the instance; a `data/` file of the same name overrides
   them.

When one of these files is used, it is served from `/img/avatar.jpg` (or
`/img/profile.image.jpg`) on your own domain, and that URL is what gets
advertised as your `icon`/`image` over ActivityPub and as the `og:image` meta
tag. Only those two exact filenames are served this way. As with any profile
change, restart the server for it to take effect (an `Update` activity is then
federated to known servers).

### Profile metadata

You can add metadata to your profile with the `metadata` config item.

Markdown is supported in the `value` field.

Be aware that most other software like Mastodon will limit the number of key/value to 4.

```toml
metadata = [
  {key = "Documentation", value = "[https://docs.microblog.pub](https://docs.microblog.pub)"},
  {key = "Source code", value = "[https://github.com/toniher/microblog.pub](https://github.com/toniher/microblog.pub)"},
]
```

### Manually approving followers

If you wish to manually approve followers, add this config item to `profile.toml`:

```toml
manually_approves_followers = true
```

The default value is `false`.

### Hiding followers

If you wish to hide your followers, add this config item to `profile.toml`:

```toml
hides_followers = true
```

The default value is `false`.

### Hiding who you are following

If you wish to hide who you are following, add this config item to `profile.toml`:

```toml
hides_following = true
```

The default value is `false`.

### Privacy replace

You can define domains to be rewritten to more "privacy friendly" alternatives, like [Invidious](https://invidious.io/)
or [Nitter](https://nitter.net/about).

To do so, add these extra config items. This is a sample config that rewrite URLs for Twitter, Youtube, Reddit and Medium:

```toml
privacy_replace = [
    {domain = "youtube.com", replace_by  = "yewtu.be"},
    {domain = "youtu.be", replace_by  = "yewtu.be"},
    {domain = "twitter.com", replace_by = "nitter.fdn.fr"},
    {domain = "medium.com", replace_by = "scribe.rip"},
    {domain = "reddit.com", replace_by = "teddit.net"},
]
```

### Disabling certain notification types

All notifications are enabled by default.

You can disabled specific notifications by adding them to the `disabled_notifications` list.

This example disables likes and shares notifications:

```
disabled_notifications = ["like", "announce"]
```

#### Available notification types

 - `new_follower`
 - `rejected_follower`
 - `unfollow`
 - `follow_request_accepted`
 - `follow_request_rejected`
 - `move`
 - `like`
 - `undo_like`
 - `announce`
 - `undo_announce`
 - `mention`
 - `new_webmention`
 - `updated_webmention`
 - `deleted_webmention`
 - `blocked`
 - `unblocked`
 - `block`
 - `unblock`

### Customization

#### Default emoji

If you don't like cats, or need more emoji, you can add your favorite emoji in `profile.toml` and it will replace the default ones:

```
emoji = "🙂🐹📌"
```

You can copy/paste them from [getemoji.com](https://getemoji.com/).

#### Custom emoji

You can add custom emoji in the `data/custom_emoji` directory and they will be picked automatically.
Do not use exotic characters in filename - only letters, numbers, and underscore symbol `_` are allowed.

#### Custom CSS

The CSS is written with [SCSS](https://sass-lang.com/documentation/syntax).

You can override colors and the font by editing `data/_theme.scss`:

```scss
$primary-color: #e14eea;                                                                            
$secondary-color: #32cd32;
```

The following variables are available to override (see `app/scss/main.scss` for the
authoritative list and their default values):

| Variable | Purpose |
| --- | --- |
| `$font-stack` | Font family used across the site |
| `$background` | Page background |
| `$light-background` | Alternate/secondary background (e.g. cards) |
| `$text-color` | Main body text |
| `$primary-color` | Primary accent (links, default favicon) |
| `$secondary-color` | Secondary accent |
| `$muted-color` | Muted/secondary text (e.g. timestamps) |
| `$form-background-color` | Form field background |
| `$form-text-color` | Form field text |
| `$nav-button-background-color` | Navigation button background |
| `$nav-button-text-color` | Navigation button text |
| `$primary-button-text-color` | Text on primary buttons |
| `$code-highlight-background` | Background behind highlighted code blocks |

Only set the variables you want to change — anything you leave out keeps its default.
If you need to go further than variables, you can add arbitrary SCSS after the variable
definitions in `data/_theme.scss`.

You will need to [recompile CSS](#recompiling-css-files) after doing any CSS changes (for actual css files to be updates) and restart microblog.pub (for css link in HTML documents to be updated with a new checksum - otherwise, browsers that downloaded old CSS will keep using it).

#### Custom favicon

By default, microblog.pub favicon is a square of `$primary-color` CSS color (see above section on how to redefine CSS colors).
You can change it to any icon you like - just save a desired file as `data/favicon.ico`.
It is served the same way as the [avatar/header images](#profile): a `data/favicon.ico`
overrides the packaged `app/static/favicon.ico` default (the themed square) at
serve time, so you only need to restart microblog.pub for it to take effect — no
recompile step required. As with your data, it is preserved across upgrades and
Docker image rebuilds.

#### Custom footer

By default, the footer shows a "Powered by microblog.pub" line.
You can replace it with your own text by adding a `custom_footer` config item to `profile.toml`:

```toml
custom_footer = "Content licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) — running microblog.pub {version}"
```

Markdown is supported, and the `{version}` placeholder is replaced by the running microblog.pub version.

#### Custom templates

If you'd like to customize your instance's theme beyond CSS, you can modify the app's HTML by placing templates in `data/templates` which overwrite the defaults in `app/templates`.

A template placed in `data/templates/<name>.html` fully replaces `app/templates/<name>.html`.
The ones you are most likely to want to override are:

 - `header.html` — the site header shown at the top of every public page (name, avatar, navigation)
 - `layout.html` — the overall page skeleton (`<head>`, footer, common markup)
 - `index.html` — the public homepage
 - `utils.html` — shared macros (see the per-macro override trick below)

Templates are written using [Jinja](https://jinja.palletsprojects.com/en/latest/templates/) templating language.
Moreover, `utils.html` has scoped blocks around the body of every macro.
This allows macros to be overridden individually in `data/templates/utils.html`, without copying the whole file.
For example, to only override the display of a specific actor's name/icon, you can create `data/templates/utils.html` file with following content:

{% raw %}
```jinja
{% extends "app/utils.html" %}
{% block display_actor %}
	{% if actor.ap_id == "https://me.example.com" %}
		<!-- custom actor display -->
	{% else %}
		{{ super() }}
	{% endif %}
{% endblock %}
```
{% endraw %}

#### Custom Content Security Policy (CSP)

You can override the default Content Security Policy by adding a line in `data/profile.toml`:

```toml
custom_content_security_policy = "default-src 'self'; style-src 'self' 'sha256-{HIGHLIGHT_CSS_HASH}'; frame-ancestors 'none'; base-uri 'self'; form-action 'self';"
```

This example will output the default CSP, note that `{HIGHLIGHT_CSS_HASH}` will be dynamically replaced by the correct value (the hash of the CSS needed for syntax highlighting).

#### Code highlighting theme

You can switch to one of the [styles supported by Pygments](https://pygments.org/styles/) by adding a line in `data/profile.toml`:

```toml
code_highlighting_theme = "solarized-dark"
```

#### Custom routes

Custom routes can be added in `data/custom_routes.py` (the file is imported
automatically at startup if it exists).

The simplest option is to serve a static HTML page. The `html_file` is read from the
`data/` directory and its content is inserted verbatim (as raw HTML) into the page
body, wrapped in the site layout:

```python
from app.customization import register_html_page

register_html_page(
    "/testcustom",
    title="test html page",
    html_file="test.html",  # relative to data/
    show_in_navbar=True,
)
```

If you register a page at `/`, it replaces the homepage, and the default stream of
notes is moved to `/notes` automatically.

For anything more dynamic, you can register a raw [FastAPI](https://fastapi.tiangolo.com/)
handler instead:

```python
from starlette.responses import PlainTextResponse
from app.customization import register_raw_handler

async def my_handler(request):
    return PlainTextResponse("hello from a custom route")

register_raw_handler(
    "/hello",
    title="Hello",
    handler=my_handler,
    show_in_navbar=False,
)
```

Routes registered with `show_in_navbar=True` (the default) get a link added to the
navigation bar automatically.

#### Custom stream visibility

The main public stream (the homepage) shows, by default, non-reply notes from people
you follow, plus anything mentioning you and replies within your own conversations.

You can override exactly what appears in the stream by defining a
`custom_stream_visibility_callback` in `data/stream.py`. It receives an `ObjectInfo`
for each incoming object and returns `True` to show it in the stream or `False` to
hide it:

```python
from app.customization import ObjectInfo

def custom_stream_visibility_callback(object_info: ObjectInfo) -> bool:
    # e.g. only show posts tagged #mycommunity from people you follow
    return (
        object_info.is_from_following
        and "mycommunity" in object_info.hashtags
    )
```

The `ObjectInfo` passed to the callback exposes:

| Field | Description |
| --- | --- |
| `is_reply` | Whether the object is a reply |
| `is_local_reply` | Whether it's a reply to one of your own objects |
| `is_mention` | Whether it mentions you (the local actor) |
| `is_from_following` | Whether it's from someone you follow |
| `hashtags` | List of hashtags on the object, e.g. `["microblogpub"]` |
| `actor_handle` | The author's handle, e.g. `@dev@microblog.pub` |
| `remote_object` | The full remote object |

Restart microblog.pub after adding or changing `data/stream.py`.

### Blocking servers

In addition to blocking "single actors" via the admin interface, you can also prevent any communication with entire servers.

Add a `blocked_servers` config item into `profile.toml`.

The `reason` field is just there to help you document/remember why a server was blocked.

You should unfollow any account from a server before blocking it.

```toml
blocked_servers = [
    {hostname = "bad.tld", reason = "Bot spam"},
]
```

## Public website

Public notes will be visible on the homepage.

Only the last 20 followers/follows you have will be shown on the public website.

And only the last 20 interactions (likes/shares/webmentions) will be displayed, to keep things simple/clean.

## Admin section

You can login to the admin section by clicking on the `Admin` link in the footer or by visiting `https://yourdomain.tld/admin/login`.
The password is the one set during the initial configuration.

By default, an admin session stays valid for 3 days. You can change this by setting `session_timeout` (in seconds) in `profile.toml`:

```toml
session_timeout = 86400  # stay logged in for 1 day
```

### Lookup

The `Lookup` section allows you to interact with any remote remote objects/content on the Fediverse.

The lookup supports:

 - profile page, like `https://testing.microblog.pub`
 - content page, like `https://testing.microblog.pub/o/4bccd2e31fad43a7896b5a33f0b8ded9`
 - username handle like `@testing@testing.microblog.pub`
 - ActivityPub ID, like `https://testing.microblog.pub/o/4bccd2e31fad43a7896b5a33f0b8ded9`

## Authoring notes

Notes are authored in [Markdown](https://commonmark.org/). There is no imposed characters limit.

If you fill the content warning, the note will be automatically marked as sensitive.

You can add attachments/upload files.
When attaching pictures, EXIF metadata (like GPS location) will be removed automatically before being stored.

Consider marking attachments as sensitive using the checkbox if needed.

## Webmentions

Public notes that link to "Webmention-compatible" website will trigger an outgoing webmention.
Most websites that support Webmention will display your profile on the mentioned page.

### Fenced code blocks

You can include code blocks in notes,  using the triple backtick syntax.

The code will be highlighted using [Pygments](https://pygments.org/).

Example:

~~~
Hello

```python
print("I will be highlighted")
```

~~~

## Interactions

microblog.pub supports the most common interactions supported by the Fediverse.

### Shares

Sharing (or announcing) an object will relay it to your followers and notify the author.
It will also be displayed on the homepage.

Most receiving servers will increment the number of shares.

Receiving a share will trigger a notification, increment the shares counter on the object and the actor avatar will be displayed on the object permalink.

### Likes

Liking an object will notify the author.

Unlike sharing, liked objects are not displayed on the homepage.

Most receiving servers will increment the number of likes.

Receiving a like will trigger a notification, increment the likes counter on the object and the actor avatar will be displayed on the object permalink.

### Bookmarks

Bookmarks allow you to like objects without notifying the author.

It is basically a "private like", and allows you to easily access them later.

It will also prevent objects to be pruned.

### Webmentions

Sending webmentions to ping mentioned websites is done automatically once a public note is authored.

Receiving a webmention will trigger a notification, increment the webmentions counter on the object and the source page will be displayed on the object permalink.

## Backup and restore

All the data generated by the server is located in the `data/` directory:

 - Configuration files
 - Server secrets
 - SQLite3 database
 - Theme modifications
 - Custom emoji
 - Uploaded media

Restoring is as easy as adding your backed up `data/` directory into a fresh deployment.

## Moving from another instance

If you want to move followers from your existing account, ensure it is supported in your software documentation.

For [Mastodon you can look at Moving or leaving accounts](https://docs.joinmastodon.org/user/moving/).

If you wish to move **to** another instance, see [Moving to another instance](#moving-to-another-instance).

First you need to grab the "ActivityPub actor URL" for your existing account:

### Python edition

```bash
# For a Python install
poetry run inv webfinger username@instance-you-want-to-move-from.tld
```

Edit the config.

### Docker edition

```bash
# For a Docker install
make account=username@instance-you-want-to-move-from.tld webfinger
```

Edit the config.

### Edit the config

And add a reference to your old/existing account in `profile.toml`:

```toml
also_known_as = "https://instance-you-want-to-move-form.tld/users/username"
```

Restart the server, and you should be able to complete the move from your existing account.

Note that if you already have a redirect in place on Mastodon, you may have to remove it before initiating the migration.

## Import follows from Mastodon

You can import the list of follows/following accounts from Mastodon.

It requires downloading the "Follows" CSV file from your Mastodon instance via "Settings" / "Import and export" / "Data export".

Then you need to run the import task:

### Python edition

```bash
# For a Python install
poetry run inv import-mastodon-following-accounts following_accounts.csv
```

### Docker edition

```bash
# For a Docker install
make path=following_accounts.csv import-mastodon-following-accounts
```

## Tasks

### Configuration checking

You can confirm that your configuration file (`data/profile.toml`) is valid using the `check-config`

#### Python edition

```bash
poetry run inv check-config
```

#### Docker edition

```bash
make check-config
```

### Recompiling CSS files

You can ensure your custom theme is valid by recompiling the CSS manually using the `compile-scss` task.

#### Python edition

```bash
poetry run inv compile-scss
```

#### Docker edition

```bash
make compile-scss
```


### Password reset

If have lost your password, you can generate a new one using the `reset-password` task.

#### Python edition

```bash
# shutdown supervisord
poetry run inv reset-password
# edit data/profile.toml
# restart supervisord
```

#### Docker edition

```bash
docker compose stop
make reset-password
# edit data/profile.toml
docker compose up -d
```

### Pruning old data

You should prune old data from time to time to free disk space.

The default retention for the inbox data is 15 days.

It's configurable via the `inbox_retention_days` config item in `profile.toml`:

```toml
inbox_retention_days = 30
```

Data owned by the server will never be deleted (at least for now), along with:

 - bookmarked objects
 - liked objects
 - shared objects
 - inbox objects mentioning the local actor
 - objects related to local conversations (i.e. direct messages, replies) 

For now, it's recommended to make a backup before running the task in case it deletes unwanted data.

You should shutdown the server before running the task.

#### Python edition

```bash
# shutdown supervisord
cp -r data/microblogpub.db data/microblogpub.db.bak
poetry run inv prune-old-data
# relaunch supervisord and ensure it works as expected
rm data/microblogpub.db.bak
```

#### Docker edition

```bash
docker compose stop
cp -r data/microblogpub.db data/microblogpub.db.bak
make prune-old-data
docker compose up -d
rm data/microblogpub.db.bak
```

### Moving to another instance

If you want to migrate to another instance, you have the ability to move your existing followers to your new account.

Your new account should reference the existing one, refer to your software configuration (for example [Moving or leaving accounts from the Mastodon doc](https://docs.joinmastodon.org/user/moving/)).

If you wish to move **from** another instance, see [Moving from another instance](#moving-from-another-instance).

Execute the Move task:

#### Python edition

```bash
# For a Python install
poetry run inv move-to username@domain.tld
```

#### Docker edition

```bash
# For a Docker install
make account=username@domain.tld move-to
```

### Deleting the instance

If you want to delete your instance, you can request other instances to delete your remote profile.

Note that this is a best-effort delete as some instances may not delete your data.

The command won't remove any local data, it just broadcasts account deletion messages to all known servers.

After executing the command, you should let the server run until all the outgoing delete tasks are sent.

Once deleted, you won't be able to use your instance anymore, but you will be able to perform a fresh re-install of any ActivityPub software.

#### Python edition

```bash
# For a Python install
poetry run inv self-destruct
```

#### Docker edition

```bash
# For a Docker install
make self-destruct
```

## Troubleshooting

If the server is not (re)starting, you can:

 - [Ensure that the configuration is valid](#configuration-checking).
 - [Verify if you haven't any syntax error in the custom theme by recompiling the CSS](#recompiling-css-files).
 - Look at the log files (in `data/uvicorn.log`, `data/incoming.log` and `data/outgoing.log`).
 - If the CSS is not working, ensure your reverse proxy is serving the static file correctly.
