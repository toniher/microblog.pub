# microblog.pub

> **This is a personal fork** of [tinyBlogPub/microblog.pub](https://github.com/tinyBlogPub/microblog.pub),
> which is itself the community continuation of the original project. It may contain
> local changes, experiments, or work-in-progress not present upstream. For the
> canonical version, see the upstream repository linked above.

A self-hosted, single-user, ActivityPub-powered microblog created by [@tsileo](https://github.com/tsileo/microblog.pub).

[![AGPL 3.0](https://img.shields.io/badge/license-AGPL_3.0-blue.svg?style=flat)](LICENSE)

[![Contributor Covenant](https://img.shields.io/badge/Contributor%20Covenant-2.1-4baaaa.svg)](code_of_conduct.md) 

Instances in the wild (this fork or close relatives):

 - [blog.joaocosta.eu](https://blog.joaocosta.eu/)
 - [bw3.dev](https://bw3.dev/)
 - [chrichri.ween.de](https://chrichri.ween.de)
 - [toniher@cau.cat](https://micro.cau.cat)

## Features

 - Implements the [ActivityPub](https://activitypub.rocks/) server to server protocol
    - Federate with other popular ActivityPub servers like Pleroma, PixelFed, PeerTube, Mastodon...
    - Consume most of the content types available (notes, articles, videos, pictures...)
    - Quote posts ([FEP-044f](https://codeberg.org/fediverse/fep/src/branch/main/fep/044f/fep-044f.md)): quote others, answer quote requests per your `quote_policy`, revoke approvals
    - Remote reports (`Flag`) arrive as notifications
    - Publishes featured hashtags and a canonical WebFinger handle (FEP-2c59)
    - Builds preview cards from link attachments (FEP-8967)
 - Exposes your ActivityPub profile as a minimalist microblog
    - Author notes in Markdown, with code highlighting support and a formatting toolbar in the composer
    - Publish polls, images, video and audio (if installed, ffmpeg adds poster frames, duration and playability checks)
    - Dedicated section for articles/blog posts (appears once you post your first article)
    - Edit history with a diff viewer between revisions
    - Assign a human-readable URL alias to any post from its admin edit page
    - Optional `/about` page
 - [Mastodon client API](docs/mastodon_api.md) compatibility (advertises Mastodon 4.7.3)
    - Log in from apps like [Tusky](https://tusky.app/) or [Fedilab](https://fedilab.app/) using your admin password
    - Read/post/interact, notifications (grouped too), direct messages, search, lists, polls, quotes, with no separate account (same actor as the web UI)
    - Real-time streaming and Web Push notifications
    - Scheduled posts (from Mastodon apps only; the web composer has no scheduling)
 - Localizable interface
    - Public and admin pages both follow a visitor's browser language, falling back to the instance's configured default language
    - Bundled translations: English, Catalan, Spanish, French, Italian, Romanian (see the [developer guide](docs/developer_guide.md#translations-i18n))
 - Accessible
    - Markup and styles follow WCAG guidance
    - Follows the system dark mode and respects `prefers-reduced-motion`
 - Lightweight
    - Uses SQLite, and Python 3.12 (3.10+ supported)
    - Can be deployed on a small VPS
 - Privacy-aware
    - microblog.pub strips EXIF metadata (like GPS location) before storage
    - The server proxies all media
    - Strict access control for your outbox enforced via HTTP signature
 - **Little** JavaScript
    - The UI is mostly pure HTML/CSS
    - Every page loads small hand-written scripts plus [htmx](https://htmx.org/); the composer adds two more
 - IndieWeb citizen
    - [IndieAuth](https://www.w3.org/TR/indieauth/) support (OAuth2 extension)
    - [Microformats](http://microformats.org/wiki/Main_Page) everywhere
    - [Micropub](https://www.w3.org/TR/micropub/) support
    - Sends and processes [Webmentions](https://www.w3.org/TR/webmention/)
    - RSS/Atom/[JSON](https://www.jsonfeed.org/) feed
 - Optional [schema.org microdata](https://schema.org/docs/gs.html) (`enable_microdata`, off by default)
 - Easy to back up
    - microblog.pub stores everything in the `data/` directory: config, uploads, secrets, and the SQLite database.

## Getting started

Check out the [online documentation](https://toniher.github.io/microblog.pub/)

## Credits

 - Emoji from [Twemoji](https://github.com/jdecked/twemoji)
 - Custom goose emoji from [@pamela@bsd.network](https://bsd.network/@pamela)


## License

The project is licensed under the [GNU AGPL v3 LICENSE](LICENSE).
