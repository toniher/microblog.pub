# microblog.pub

A self-hosted, single-user, [ActivityPub](https://activitypub.rocks/)-powered
microblog. One instance is one actor: it federates with the fediverse (Mastodon,
Pleroma, PeerTube, PixelFed…) and doubles as an [IndieWeb](https://indieweb.org/)
citizen.

## Features

- Implements the [ActivityPub](https://activitypub.rocks/) server-to-server protocol
  - Federate with all the other popular ActivityPub servers like Pleroma, PixelFed,
    PeerTube, Mastodon…
  - Consume most of the content types available (notes, articles, videos, pictures…)
- Exposes your ActivityPub profile as a minimalist microblog
  - Author notes in Markdown, with code highlighting support
  - Dedicated section for articles/blog posts (enabled when the first article is posted)
- [Mastodon client API](mastodon_api.md) compatibility — log in from apps like
  Tusky or Fedilab to read, post, and interact without touching the web UI
- Lightweight
  - Uses SQLite, and Python 3.12 (3.10+ supported)
  - Can be deployed on a small VPS
- Privacy-aware
  - EXIF metadata (like GPS location) is stripped before storage
  - Every media is proxied through the server
  - Strict access control for your outbox enforced via HTTP signature
- **Little** JavaScript — the UI is mostly pure HTML/CSS
- IndieWeb citizen
  - [IndieAuth](https://www.w3.org/TR/indieauth/) support (OAuth2 extension)
  - [Microformats](http://microformats.org/wiki/Main_Page) everywhere
  - [Micropub](https://www.w3.org/TR/micropub/) support
  - Sends and processes [Webmentions](https://www.w3.org/TR/webmention/)
  - RSS/Atom/[JSON](https://www.jsonfeed.org/) feed
- Easy to back up — everything lives in the `data/` directory: config, uploads,
  secrets, and the SQLite database.

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
