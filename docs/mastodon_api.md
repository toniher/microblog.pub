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
credentials rather than a separate account system. "Log out" in the app calls
`POST /oauth/revoke`, which kills the token server-side too — it isn't just
forgotten on the device.

## What works

- **Timelines** — home, local/federated public, and hashtag timelines
  (`/api/v1/timelines/home`, `/public`, `/tag/:hashtag`), with `max_id`/`since_id`/
  `min_id` pagination and a `Link` header, like real Mastodon. The hashtag
  timeline takes Mastodon's multi-tag parameters (`any[]`, `all[]`, `none[]`), so
  clients that build saved searches out of several tags work.
- **Statuses** — read, create, edit, delete; replies, content warnings,
  sensitive/media attachments, polls (including voting), and per-post language.
  Editing keeps full history (`/api/v1/statuses/:id/history`), so clients can
  show what a post looked like before each edit.
- **Interactions** — favourite, reblog, bookmark, pin, with their "who
  favourited/reblogged this" endpoints.
- **Link previews** — posts containing a link carry a Mastodon `card`, built from
  the OpenGraph metadata this instance already scrapes for its own web UI, so
  clients render the same preview box. The thumbnail goes through the media
  proxy, like every other remote image.
- **Direct messages** — surfaced as Mastodon "conversations"
  (`/api/v1/conversations`), grouped the same way the `Direct messages` admin page
  groups them, with mark-as-read support.
- **Notifications** — follows, favourites, reblogs, mentions, moves; read
  state, per-type filtering, clear/dismiss, and an unread count
  (`/api/v1/notifications/unread_count`) for badge counts.
- **Read-position sync** — `/api/v1/markers` is genuinely persisted (home and
  notifications timelines), so "resume where I left off" survives across
  devices and reinstalls.
- **Accounts & social graph** — profile lookup (including the batch
  `/api/v1/accounts?id[]=...` form some clients use), your own and remote
  actors' statuses/followers/following (boosts included in your own profile,
  same as everyone else's), follow/unfollow, block/unblock, the list of accounts
  you've blocked (`/api/v1/blocks`, so blocks can be reviewed and undone from a
  client), personal notes on an account, and incoming follow request
  approve/reject, with a real `follow_requests_count` badge on your own
  profile. A follower can also be dropped without blocking them
  (`/api/v1/accounts/:id/remove_from_followers`) — their server is told with a
  Reject of the original follow, and they're free to follow again.
  Opening a remote actor you don't follow yet backfills their recent posts and
  follower/following/post counts on demand (fetched and cached, throttled), so
  their profile isn't empty on first view.
- **Mutes** — mute/unmute an account, with the `notifications` and `duration`
  options, plus the list of who you've muted (`/api/v1/mutes`) and the
  `muting`/`muting_notifications` relationship flags. A muted account
  disappears from every timeline (their boosts, and other people's boosts of
  them, included) but keeps following you and stays reachable from their
  profile — nothing is federated, so they can't tell.
- **Domain blocks** (`/api/v1/domain_blocks`) — the `blocked_servers` hostnames from
  `profile.toml`, sorted. Read-only: it's static config, so there's no `POST`/`DELETE`
  to add or remove a domain block from a client.
- **Conversation mute** — mute/unmute the thread a status belongs to
  (`/api/v1/statuses/:id/mute`/`unmute`), so replies to a noisy thread stop
  generating notifications. The status entity's `muted` flag reflects it, and it
  survives replies that arrive after the mute, not just the ones that exist yet.
- **Featured tags** (`/api/v1/featured_tags`) — hashtags pinned to your profile via
  `featured_tags` in `profile.toml`, shown with their post counts. Read-only: this
  mirrors the config file, so there's no `POST`/`DELETE` to add or remove one from
  a client.
- **Search** (`/api/v2/search`) — accounts, statuses, and hashtags.
- **Media uploads**, including descriptions/alt text — images, video and audio.
  Video/audio gets a real duration, a poster frame (extracted with `ffmpeg`,
  reused as `preview_url` and the AP `icon`), and a blurhash, the same as
  images. There's no transcoding: a file that uploads cleanly must already be
  playable in mainstream browsers, so an instance-side compatibility check
  runs against the codec/container/chroma subsampling (not just the mime
  type) and rejects confidently-broken files — e.g. HEVC from an iPhone, or a
  QuickTime `.mov` — with a `422` naming the specific problem and what to do
  about it (typically: re-encode as H.264/AAC in an MP4). `ffmpeg` is
  optional; without it, uploads are still accepted, just without duration,
  poster, blurhash or compatibility checking. `supported_mime_types` and the
  size limits in `/api/v1/instance`'s `media_attachments` are real and
  enforced, not just advisory. Uploads are still processed synchronously —
  `POST /api/v2/media` never returns Mastodon's `206`/still-processing shape,
  so a very large upload occupies the request for the whole transfer + probe
  + poster extraction.
- **Instance "about" extras** — `/api/v1/instance/rules` (empty, none configured),
  `/extended_description` (the same bio text as the instance description), the
  public `/instance/domain_blocks` transparency list (hostname, digest, reason —
  distinct from the authenticated `/api/v1/domain_blocks` above), and `/activity`
  (12 weeks of post counts and login counts, so "about this server" screens have
  something to plot instead of blanks).
- **Push notifications** (`/api/v1/push/subscription`, `GET`/`POST`/`PUT`/`DELETE`) —
  real Web Push, end-to-end encrypted (VAPID + `aes128gcm`), for mentions,
  favourites, boosts, follows and follow requests, honouring the same
  mute/conversation-mute filtering the in-app notification list applies.
  `standard: true`; the `alerts` map advertises all ten Mastodon keys, but
  `status`/`poll`/`update` and the admin-only `admin.sign_up`/`admin.report`
  are always inert — this instance never generates those notification types
  and has no admin surface to notify about. `policy` (`all`/`followed`/
  `follower`/`none`) is honoured. New subscriptions default every alert to
  `true` (upstream Mastodon defaults them `false`, which leaves a fresh
  subscription silently inert until the client calls update — every real
  client sends explicit alerts anyway, so this instance opts for the less
  surprising default). **Deployment note**: delivery runs in a separate
  `push_worker` process — see `docs/install.md` for the supervisord entry. An
  install that skips wiring it up will still accept subscriptions and
  advertise a VAPID key, just never deliver anything.
- **Scheduled posts** — `POST /api/v1/statuses` with `scheduled_at` queues the
  post instead of sending it, and returns Mastodon's `ScheduledStatus` entity;
  `/api/v1/scheduled_statuses` lists the queue, with `GET`/`PUT`/`DELETE` on a
  single entry (`PUT` changes the publication time, the only field Mastodon
  makes editable). Everything an immediate post supports carries over —
  attachments, CW/sensitive, visibility, language, replies, polls — and is
  validated when you queue it, not when it comes due. Any time in the future is
  accepted, where upstream Mastodon insists on at least five minutes out. No
  extra process is needed: the existing `outgoing_worker` publishes due posts as
  part of its poll, so a queued post goes out within a couple of seconds of its
  time. If publishing fails (say an attachment was deleted in the meantime) it's
  retried with a growing backoff and then left in the queue rather than
  disappearing — rescheduling it with `PUT` gives it a fresh set of attempts.
- **Streaming API** (`wss://…/api/v1/streaming`, WebSocket only — no SSE) —
  `user`, `user:notification`, `public`, `public:local`, `public:remote`,
  `hashtag` and `direct` streams, delivering `update`, `status.update`,
  `delete`, `notification` and `conversation` events. Unlike Web Push, this
  needs **no separate process**: the server runs as a single process/event
  loop, so a small in-process task polls committed rows (~1s interval,
  `streaming_poll_interval`) and fans out over the open sockets — the same
  filtering (mutes, visibility) the REST timelines apply, since it re-queries
  through the same functions rather than duplicating the logic. `delete` and
  `status.update` are best-effort over a bounded window (the newest ~500
  statuses per table plus anything streamed since connecting) — a much older
  status, deleted, produces no frame; the client's own list still updates on
  its next REST fetch. **Not supported**: `list` streams (Lists are an empty
  stub, see below) and the `public:*:media` variants. One socket may hold at
  most 64 subscriptions (`hashtag` streams carry a client-supplied tag, so the
  set needs a bound); a 65th `subscribe` gets an error frame and is ignored.
  **Deployment note**:
  the reverse proxy must forward the WebSocket upgrade on this path
  specifically — see the `location /api/v1/streaming` block in
  `docs/install.md`'s nginx snippet. `streaming_enabled = false` in
  `data/profile.toml` disables the endpoint and removes the advertisement.

## What doesn't (single-user degradations)

microblog.pub is one instance, one actor — several Mastodon API areas exist for
things a single-user server has no data for. These degrade gracefully (an empty
list, or a harmless no-op) rather than erroring, so clients render an empty state
instead of crashing:

- **Lists, filters, suggestions, the directory, trends, and familiar
  followers** — always empty.
- **Federated peers** (`/api/v1/instance/peers`) — always empty. This one's a
  deliberate privacy choice rather than a missing feature: the data exists,
  but publishing which servers you've federated with is worth opting out of.
- **Notification requests / policy** — this server never filters notifications,
  so the filtered-notifications queue (`/api/v1/notifications/requests`) is
  always empty and the policy (`/api/v2/notifications/policy`) always reports
  "accept everything"; nothing is held back for approval.

## Scopes

Standard Mastodon OAuth scopes are supported, including the granular
`read:*`/`write:*` forms — a token granted the top-level `read`/`write`/`follow`
scope satisfies any of the matching granular scopes underneath it, same as real
Mastodon. Most apps request a broad `read write follow push` by default; `push`
is a real scope here too, gating the push subscription endpoints above.

## Troubleshooting

- **A client shows "not mocked"/network errors on first login**: double-check
  you entered your bare domain (no `https://`, no trailing slash) in the app's
  "instance" field.
- **Nothing shows up on first sync**: some clients only backfill a page or two
  of history on first login; give it a pull-to-refresh.
- **Push notifications never arrive**: confirm the `push_worker` process is
  running (`supervisorctl status`) and check `data/push.log` for delivery
  errors. If it's running and logging clean 2xx/201 responses but nothing
  shows up on the device, double-check the `server_key` your client
  subscribed with still matches `/api/v1/instance`'s
  `configuration.vapid.public_key` — a regenerated VAPID key invalidates
  every existing subscription, and the client needs to re-subscribe.
- **Streaming never connects (client stuck "connecting…")**: check that the
  reverse proxy has a dedicated location forwarding the WebSocket upgrade for
  `/api/v1/streaming` specifically — a generic `proxy_pass` without
  `proxy_set_header Upgrade`/`Connection` will accept the TCP connection and
  then hang, since ordinary HTTP proxying doesn't forward the upgrade. Also
  check `proxy_read_timeout` is generous (an idle socket dying at the default
  60s reads to the client as an unexplained disconnect).
- If something a real Mastodon client relies on 404s instead of degrading
  gracefully, that's a gap worth [reporting an
  issue](https://github.com/toniher/microblog.pub/issues) for — the API surface
  above is what's implemented today, not a hard ceiling.
