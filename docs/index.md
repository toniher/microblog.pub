# microblog.pub

A self-hosted, single-user, [ActivityPub](https://activitypub.rocks/)-powered
microblog. One instance is one actor: it federates with the fediverse (Mastodon,
Pleroma, PeerTube, PixelFed…) and doubles as an [IndieWeb](https://indieweb.org/)
citizen.

## Features

- Implements the [ActivityPub](https://activitypub.rocks/) server-to-server protocol
  - Federate with other popular ActivityPub servers like Pleroma, PixelFed,
    PeerTube, Mastodon…
  - Consume most of the content types available (notes, articles, videos, pictures…)
  - Quote posts ([FEP-044f](https://codeberg.org/fediverse/fep/src/branch/main/fep/044f/fep-044f.md)):
    quote others, answer quote requests per your `quote_policy`, revoke approvals
  - Remote reports (`Flag`) arrive as notifications
  - Publishes featured hashtags and a canonical WebFinger handle (FEP-2c59)
  - Builds preview cards from link attachments (FEP-8967)
- Exposes your ActivityPub profile as a minimalist microblog
  - Author notes in Markdown, with code highlighting support and a formatting
    toolbar in the composer
  - Publish polls, images, video and audio (if installed, ffmpeg adds poster
    frames, duration and playability checks)
  - Dedicated section for articles/blog posts (appears once you post your first article)
  - Edit history with a diff viewer between revisions
  - Assign a human-readable URL alias to any post from its admin edit page
  - Optional `/about` page
- [Mastodon client API](mastodon_api.md) compatibility (advertises Mastodon 4.7.3):
  log in from apps like Tusky or Fedilab to read, post, and interact without
  touching the web UI. Covers notifications (grouped too), direct messages,
  search, lists, polls, quotes, real-time streaming, Web Push, and scheduled
  posts (the web composer has no scheduling)
- Localizable interface: public and admin pages both follow a visitor's browser
  language, falling back to the instance default. Bundled translations: English,
  Catalan, Spanish, French, Italian, Romanian (see [Developer guide](developer_guide.md#translations-i18n))
- Accessible: markup and styles follow WCAG guidance, follow the system dark
  mode, and respect `prefers-reduced-motion`
- Lightweight
  - Uses SQLite, and Python 3.12 (3.10+ supported)
  - Can be deployed on a small VPS
- Privacy-aware
  - microblog.pub strips EXIF metadata (like GPS location) before storage
  - The server proxies all media
  - Strict access control for your outbox enforced via HTTP signature
- **Little** JavaScript: the UI is mostly pure HTML/CSS. Every page loads small
  hand-written scripts plus [htmx](https://htmx.org/); the composer adds two more
- IndieWeb citizen
  - [IndieAuth](https://www.w3.org/TR/indieauth/) support (OAuth2 extension)
  - [Microformats](http://microformats.org/wiki/Main_Page) everywhere
  - [Micropub](https://www.w3.org/TR/micropub/) support
  - Sends and processes [Webmentions](https://www.w3.org/TR/webmention/)
  - RSS/Atom/[JSON](https://www.jsonfeed.org/) feed
- Optional [schema.org microdata](https://schema.org/docs/gs.html) (`enable_microdata`, off by default)
- Easy to back up: everything lives in the `data/` directory (config, uploads,
  secrets, and the SQLite database).

## Documentation

```{toctree}
:maxdepth: 2

install.md
user_guide.md
mastodon_api.md
developer_guide.md
```

## License

The project is licensed under the [GNU AGPL v3](https://github.com/toniher/microblog.pub/blob/main/LICENSE).
