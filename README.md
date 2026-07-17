# microblog.pub

> **This is a personal fork** of [tinyBlogPub/microblog.pub](https://github.com/tinyBlogPub/microblog.pub),
> which is itself the community continuation of the original project. It may contain
> local changes, experiments, or work-in-progress not present upstream. For the
> canonical version, see the upstream repository linked above.

A self-hosted, single-user, ActivityPub-powered microblog created by [@tsileo](https://github.com/tsileo/microblog.pub).
This repo and collective is a respectful attempt by the users of the project to keep it going!

[![AGPL 3.0](https://img.shields.io/badge/license-AGPL_3.0-blue.svg?style=flat)](LICENSE)

[![Contributor Covenant](https://img.shields.io/badge/Contributor%20Covenant-2.1-4baaaa.svg)](code_of_conduct.md) 

Instances in the wild (this fork or close relatives):

 - [blog.joaocosta.eu](https://blog.joaocosta.eu/)
 - [bw3.dev](https://bw3.dev/)
 - [chrichri.ween.de](https://chrichri.ween.de)
 - [toniher@cau.cat](https://micro.cau.cat)

## Features

 - Implements the [ActivityPub](https://activitypub.rocks/) server to server protocol
    - Federate with all the other popular ActivityPub servers like Pleroma, PixelFed, PeerTube, Mastodon...
    - Consume most of the content types available (notes, articles, videos, pictures...)
 - Exposes your ActivityPub profile as a minimalist microblog
    - Author notes in Markdown, with code highlighting support
    - Dedicated section for articles/blog posts (enabled when the first article is posted)
 - Mastodon client API compatibility
    - Log in from apps like [Tusky](https://tusky.app/) or [Fedilab](https://fedilab.app/) using your admin password
    - Read/post/interact, notifications, direct messages, search — no separate account, same actor as the web UI
 - Localizable interface
    - Public pages follow a visitor's browser language (falling back to the instance default); the admin UI always uses the instance's configured language
    - Bundled translations: English, Catalan, Spanish, French, Italian, Romanian
 - Lightweight
    - Uses SQLite, and Python 3.12 (3.10+ supported)
    - Can be deployed on small VPS
 - Privacy-aware
    - EXIF metadata (like GPS location) are stripped before storage
    - Every media is proxied through the server
    - Strict access control for your outbox enforced via HTTP signature
 - **Little** Javascript
    - The UI is mostly pure HTML/CSS
    - Except for tiny bits of hand-written JS
      - or some localized and optional JS libraries to improve usability
 - IndieWeb citizen
    - [IndieAuth](https://www.w3.org/TR/indieauth/) support (OAuth2 extension)
    - [Microformats](http://microformats.org/wiki/Main_Page) everywhere
    - [Micropub](https://www.w3.org/TR/micropub/) support
    - Sends and processes [Webmentions](https://www.w3.org/TR/webmention/)
    - RSS/Atom/[JSON](https://www.jsonfeed.org/) feed
 - Support to [schema.org microdata](https://schema.org/docs/gs.html)
 - Easy to backup
    - Everything is stored in the `data/` directory: config, uploads, secrets, and the SQLite database.

## Getting started

Check out the [online documentation](https://toniher.github.io/microblog.pub/)

## Credits

 - Emoji from [Twemoji](https://github.com/jdecked/twemoji)
 - Awesome custom goose emoji from [@pamela@bsd.network](https://bsd.network/@pamela)


## License

The project is licensed under the [GNU AGPL v3 LICENSE](LICENSE).
