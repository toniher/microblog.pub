# Mastodon client apps

microblog.pub exposes a subset of the [Mastodon client REST
API](https://docs.joinmastodon.org/client/intro/), so you can read and post to your
instance from existing Mastodon apps — [Tusky](https://tusky.app/),
[Fedilab](https://fedilab.app/), [Ivory](https://tapbots.com/ivory/), [Ice
Cubes](https://github.com/Dimillian/IceCubesApp), the [official Mastodon
app](https://joinmastodon.org/apps)… — instead of (or alongside) the built-in web UI.

This is **not** a second identity: it's the same single actor, the same posts, the
same followers. The app just becomes another window onto your existing instance.

## Connecting an app

1. In the app, enter your instance's domain (the same one you log into `/admin`
   with) wherever it asks for a server/instance.
2. The app registers itself and redirects you to your instance's login page —
   log in with your **admin password**, then approve the app's access request.
3. You're in. The app now talks to your instance exactly like it would to a real
   Mastodon server.

There's nothing to enable server-side — the API is always mounted, and
registrations/logins go through the same OAuth2 flow as
[IndieAuth](https://www.w3.org/TR/indieauth/), reusing your existing admin
credentials rather than a separate account system.

## What works

- **Timelines** — home, local/federated public, and single-hashtag timelines
  (`/api/v1/timelines/home`, `/public`, `/tag/:hashtag`), with `max_id`/`since_id`/
  `min_id` pagination and a `Link` header, like real Mastodon.
- **Statuses** — read, create, edit, delete; replies, content warnings,
  sensitive/media attachments, polls (including voting), and per-post language.
- **Interactions** — favourite, reblog, bookmark, pin, with their "who
  favourited/reblogged this" endpoints.
- **Direct messages** — surfaced as Mastodon "conversations"
  (`/api/v1/conversations`), grouped the same way the `Direct messages` admin page
  groups them, with mark-as-read support.
- **Notifications** — follows, favourites, reblogs, mentions, moves; read
  state, per-type filtering, clear/dismiss.
- **Accounts & social graph** — profile lookup, your own and remote actors'
  statuses/followers/following, follow/unfollow, block/unblock, mute/unmute,
  personal notes on an account, and incoming follow request approve/reject.
- **Search** (`/api/v2/search`) — accounts, statuses, and hashtags.
- **Media uploads**, including descriptions/alt text.

## What doesn't (single-user degradations)

microblog.pub is one instance, one actor — several Mastodon API areas exist for
things a single-user server has no data for. These degrade gracefully (an empty
list, or a harmless no-op) rather than erroring, so clients render an empty state
instead of crashing:

- **Lists, filters, suggestions, mutes, the directory, and trends** — always
  empty.
- **Push notifications** — there's no `/api/v1/push` endpoint; apps fall back to
  polling. If your client shows a "notifications unavailable" toggle for this
  instance, that's expected.
- **Streaming API** — not implemented; `/api/v1/instance` omits `streaming_api`
  on purpose so clients know not to try. Everything works over polling instead.
- **Scheduled posts** — not supported.

## Scopes

Standard Mastodon OAuth scopes are supported, including the granular
`read:*`/`write:*` forms — a token granted the top-level `read`/`write`/`follow`
scope satisfies any of the matching granular scopes underneath it, same as real
Mastodon. Most apps request a broad `read write follow push` by default, which
works fine even though `push` itself is a no-op here.

## Troubleshooting

- **A client shows "not mocked"/network errors on first login**: double-check
  you entered your bare domain (no `https://`, no trailing slash) in the app's
  "instance" field.
- **Nothing shows up on first sync**: some clients only backfill a page or two
  of history on first login; give it a pull-to-refresh.
- If something a real Mastodon client relies on 404s instead of degrading
  gracefully, that's a gap worth [reporting an
  issue](https://github.com/toniher/microblog.pub/issues) for — the API surface
  above is what's implemented today, not a hard ceiling.
