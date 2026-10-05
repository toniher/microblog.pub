# Mastodon client apps

microblog.pub exposes a subset of the [Mastodon client REST
API](https://docs.joinmastodon.org/client/intro/), so you can read and post to your
instance from existing Mastodon apps ([Tusky](https://tusky.app/),
[Fedilab](https://fedilab.app/), [Ivory](https://tapbots.com/ivory/), [Ice
Cubes](https://github.com/Dimillian/IceCubesApp), the [official Mastodon
app](https://joinmastodon.org/apps)…) instead of (or alongside) the built-in web UI.

An app uses the same single actor, posts and followers as the web UI, with no
second identity. It gives you another window onto your existing instance.

## Connecting an app

1. In the app, enter your instance's domain (the same one you log into `/admin`
   with) wherever it asks for a server/instance.
2. The app registers itself and redirects you to your instance's login page.
   Log in with your **admin password**, then approve the app's access request.
3. The app now talks to your instance as it would to a Mastodon server.

You don't need to enable anything server-side: the API is always mounted, and
registrations/logins go through the same OAuth2 flow as
[IndieAuth](https://www.w3.org/TR/indieauth/), reusing your existing admin
credentials instead of a separate account system. "Log out" in the app calls
`POST /oauth/revoke`, which revokes the token server-side as well as removing it
from the device. Tokens are long-lived and come without a refresh token, and
there is no `client_credentials` grant, so an app always needs your login.

## What works

- **Timelines**: home, local/federated public, and hashtag timelines
  (`/api/v1/timelines/home`, `/public`, `/tag/:hashtag`), with `max_id`/`since_id`/
  `min_id` pagination and a `Link` header, as in Mastodon. The hashtag
  timeline takes Mastodon's multi-tag parameters (`any[]`, `all[]`, `none[]`), so
  clients that build saved searches out of several tags work. The public
  timeline reads `local` only: `remote=true` and `only_media` are ignored.
- **Statuses**: read, create, edit, delete; replies, content warnings,
  sensitive/media attachments, polls (including voting), and per-post language.
  Editing keeps full history (`/api/v1/statuses/:id/history`), so clients can
  show what a post looked like before each edit. An attachment's id in
  `media_attachments` is its underlying media id, so `media_ids` on an edit
  round-trips the same ids `GET`/`POST` handed the client (older cached ids in
  the `{status_id}-{index}` form are still accepted). `media_attributes[][id]`/
  `[description]`/`[focus]` on an edit updates an attachment's alt text/focal
  point in place, in either JSON or form bodies, and works even without
  `media_ids` in the same request.
- **Polls**: create one on a status (`poll[options][]`, `expires_in`,
  `multiple`), read it (`/api/v1/polls/:id`) and vote
  (`/api/v1/polls/:id/votes`), in either form-encoded or JSON bodies. The limits
  `/api/v1/instance` advertises (4 options, 100 characters each, 5 minutes to
  ~30 days) are the enforced limits, so a client that builds its composer from
  them won't get unexpected rejections. `votes_count` counts votes cast and
  `voters_count` counts people; they differ on a multiple-choice poll, and both
  are tracked for your own polls as answers federate in. One vote per poll, as
  in Mastodon: a second attempt is a `422` instead of a duplicate vote
  delivered to the poll's author, and voting in your own poll is a `422` too.
  **Not supported**: `poll[hide_totals]` is accepted and ignored. ActivityPub
  has no field for it, so every other server and this instance's own web UI
  would still show the tallies, and hiding them only in this API would mislead.
- **Interactions**: favourite, reblog, bookmark, pin, with their "who
  favourited/reblogged this" endpoints.
- **Quote posts**: `quote_id` on `POST /api/v1/statuses`, and a `quote` key next
  to `reblog` in the status entity: `{state, quoted_status}`, with `state` one of
  `pending`, `accepted`, `rejected`, `revoked`, `deleted` or `unauthorized`, and
  `quoted_status` populated only once `accepted`. Quoting your own post is
  auto-authorized; quoting anyone else's sends a federated `QuoteRequest` (FEP-044f)
  and the status starts out `pending` until the remote server responds. The
  `quote_policy` config key (`public`/`followers`/`manual`/`nobody`, default
  `public`) controls whether an incoming `QuoteRequest` for one of your own posts
  is auto-accepted, requires being a follower, needs manual approval (a
  notification with accept/reject buttons, like a follow request), or is always
  declined. A stamp you've granted can be revoked from `/admin`, which sends a
  `Delete` federating the revocation; the quote then reports `revoked` here (or,
  for a quote of a remote post, `unauthorized`, since the inbox side has no
  dedicated revoked state). The Mastodon 4.5 client surface is also implemented: `quoted_status_id`
  (the official name for `quote_id`, which still works), `quotes_count` and
  `quote_approval` on every status (for your own posts derived from
  `quote_policy`, for remote ones from the `interactionPolicy` they publish),
  `GET /api/v1/statuses/:id/quotes`, `POST .../quotes/:quoting_status_id/revoke`
  (the API twin of the `/admin` button), and the policy as
  `posting:default:quote_policy` / `source[quote_policy]` (`manual` reports as
  `nobody`, since Mastodon has no approval-queue default). **Not supported**:
  choosing a policy per post. `quote_approval_policy` on create/edit is refused
  with a `422` unless it equals the configured policy (ignored on private/direct
  posts, as in Mastodon), and `PUT /api/v1/statuses/:id/interaction_policy`
  doesn't exist. The `quote` and `quoted_update` notification types are never
  emitted.
- **Mastodon 4.6** (`api_versions.mastodon` 10) added these, all present: new
  entity keys (`avatar_description`, `show_media*`, `feature_approval`,
  `muting_expires_at`, `tagged_collections`, `missing_attribution`,
  `wrapstodon`, `configuration.accounts`) and `exclude_direct` on account
  statuses. `GET /api/v1/profile` returns the profile; `PUT`/`PATCH
  /api/v1/profile` and `DELETE /api/v1/profile/avatar`/`/header` are `422`
  (edit `data/profile.toml`). Collections are **not supported**: the account
  `collections` and `in_collections` lists are empty, `GET
  /api/v1/collections/:id` is `404`, and the writes are `422`. Annual reports:
  the index is empty, `/{year}/state` reports `ineligible`, `GET /{year}` and
  `POST /{year}/read` are `404`, and `POST /{year}/generate` is `422`.
  `supported_types[]` on notifications is ignored, since none of the new
  notification types are emitted.
- **Mastodon 4.7**: the instance reports `4.7.3` / `api_versions.mastodon` 11
  (11 only adds description params to the refused profile writes). 4.7 has no
  other client API changes; `invalid_handle` is never set here.
- **Link previews**: posts containing a link carry a Mastodon `card`, built from
  the OpenGraph metadata this instance scrapes for its own web UI, so
  clients render the same preview box. The thumbnail goes through the media
  proxy, like every other remote image. Mastodon 4.7+ servers also send the
  card URL as a `Link` attachment (FEP-8967). It feeds the card and no longer
  shows up as an "unknown" media attachment. Posts whose link exists only as
  that hint (e.g. Lemmy link posts) get a card from this version on; older ones
  are not re-fetched.
- **Direct messages**: surfaced as Mastodon "conversations"
  (`/api/v1/conversations`), grouped the same way the `Direct messages` admin page
  groups them, with mark-as-read support.
- **Notifications**: follows, follow requests (`follow_request`), favourites,
  reblogs, mentions, moves, new posts
  from an account you follow with `notify` set, edits to a post you favourited
  or boosted, and your own polls (or ones you voted in) ending; read state,
  per-type filtering, clear/dismiss, and an unread count
  (`/api/v1/notifications/unread_count`) for badge counts. A poll ending is the
  one notification with no activity to react to, so the existing
  `outgoing_worker` finds ended polls with a sweep as part of its poll; you
  don't need an extra process. The notification row itself is the watermark, so
  an ended poll is never notified twice.
- **Grouped notifications** (Mastodon 4.3+, `/api/v2/notifications*`): the
  "12 people favourited your post" screen. Favourites and reblogs group per
  post, follows group per UTC calendar day; everything else (mentions, status,
  update, poll, move, follow requests) stays one notification per group, same
  as v1. `GET /{group_key}`, `POST /{group_key}/dismiss`,
  `GET /{group_key}/accounts` and a groups-aware `GET /unread_count` are also
  implemented; `grouped_types[]` narrows which types group, matching the spec.
  `expand_accounts` is ignored: responses carry full accounts, never
  `partial_accounts`.
- **Read-position sync**: `/api/v1/markers` is persisted (home and
  notifications timelines), so "resume where I left off" survives across
  devices and reinstalls.
- **Accounts & social graph**: profile lookup (including the batch
  `/api/v1/accounts?id[]=...` form some clients use), your own and remote
  actors' statuses/followers/following (boosts included in your own profile,
  same as everyone else's), follow/unfollow, block/unblock, the list of accounts
  you've blocked (`/api/v1/blocks`, so you can review and undo blocks from a
  client), personal notes on an account, and incoming follow request
  approve/reject, with a real `follow_requests_count` badge on your own
  profile. You can also drop a follower without blocking them
  (`/api/v1/accounts/:id/remove_from_followers`): the instance sends their
  server a Reject of the original follow, and they're free to follow again.
  Opening a remote actor you don't follow yet backfills their recent posts and
  follower/following/post counts on demand (fetched and cached, throttled), so
  their profile isn't empty on first view. `POST /follow` accepts `reblogs`
  and `notify`, reflected back as `showing_reblogs`/`notifying` on the
  relationship entity: `reblogs=false` hides that account's boosts from every
  timeline (retroactively; toggling it back on unhides them), and
  `notify=true` generates a `status` notification for their new top-level
  posts. Re-`POST`ing `/follow` on an existing follow only changes the flags
  sent, and never sends a second `Follow` activity. Account statuses honour
  `exclude_replies`, `pinned` and `exclude_direct`; `only_media`,
  `exclude_reblogs` and `tagged` are ignored.
- **Mutes**: mute/unmute an account, with the `notifications` and `duration`
  options, plus the list of who you've muted (`/api/v1/mutes`) and the
  `muting`/`muting_notifications` relationship flags. A muted account
  disappears from every timeline (their boosts, and other people's boosts of
  them, included) but keeps following you and stays reachable from their
  profile. Nothing is federated, so they can't tell.
- **Domain blocks** (`/api/v1/domain_blocks`): the `blocked_servers` hostnames from
  `profile.toml`, sorted. Read-only: it's static config, so there's no `POST`/`DELETE`
  to add or remove a domain block from a client.
- **Conversation mute**: mute/unmute the thread a status belongs to
  (`/api/v1/statuses/:id/mute`/`unmute`), so replies to a noisy thread stop
  generating notifications. The status entity's `muted` flag reflects it, and
  the mute also covers replies that arrive after it.
- **Featured tags** (`/api/v1/featured_tags`): hashtags pinned to your profile via
  `featured_tags` in `profile.toml`, shown with their post counts. Read-only: this
  mirrors the config file, so there's no `POST`/`DELETE` to add or remove one from
  a client. The same list is federated as the actor's `toot:featuredTags`
  collection.
- **Search** (`/api/v2/search`): accounts, statuses, and hashtags. The older
  `/api/v1/accounts/search` is also implemented, including its `following=true`
  filter, since some clients (e.g. Tusky's "add account to list" picker) call it
  directly instead of `/api/v2/search`.
- **Media uploads**: images, video and audio, including descriptions/alt text.
  Video/audio gets a duration, a poster frame (extracted with `ffmpeg`,
  reused as `preview_url` and the AP `icon`), and a blurhash, the same as
  images. The instance doesn't transcode, so an uploaded file must already
  play in mainstream browsers. An instance-side compatibility check inspects
  the codec, container and chroma subsampling (as well as the mime type) and
  rejects files that are certainly broken (e.g. HEVC from an iPhone, or a
  QuickTime `.mov`) with a `422` naming the problem and the fix (typically:
  re-encode as H.264/AAC in an MP4). `ffmpeg` is optional; without it, the
  instance still accepts uploads but skips duration, poster, blurhash and the
  compatibility check. `supported_mime_types` and the size limits in
  `/api/v1/instance`'s `media_attachments` are enforced. Uploads are processed
  synchronously: `POST /api/v2/media` never returns Mastodon's
  `206`/still-processing shape, so a large upload holds the request for the
  whole transfer, probe and poster extraction. Media also accepts a `focus` cropping hint (`x,y`, each
  in `[-1.0, 1.0]`) on create and update, echoed back as `meta.focus` and
  federated (both ways) as the Pleroma-style `focalPoint` attachment extension.
  The v1 `POST /api/v1/media` works too, and `GET`/`PUT`/`DELETE
  /api/v1/media/:id` read, update and delete an upload. `DELETE` is a `422`
  while the media is attached to a post or a scheduled status.
- **Instance "about" extras**: `/api/v1/instance/rules` (empty, none configured),
  `/extended_description` (the same bio text as the instance description), the
  public `/instance/domain_blocks` transparency list (hostname, digest, reason;
  distinct from the authenticated `/api/v1/domain_blocks` above), and `/activity`
  (12 weeks of post counts and login counts, so "about this server" screens have
  data to plot).
- **Push notifications** (`/api/v1/push/subscription`, `GET`/`POST`/`PUT`/`DELETE`):
  Web Push, end-to-end encrypted (VAPID + `aes128gcm`), for mentions,
  favourites, boosts, follows, follow requests, new posts (`status`),
  favourited/boosted post edits (`update`) and poll endings (`poll`),
  honouring the same mute/conversation-mute filtering the in-app notification
  list applies. `standard: true`; the `alerts` map advertises all ten
  Mastodon keys, but the admin-only `admin.sign_up`/`admin.report` are always
  inert, since this instance has no admin surface to notify about. `policy`
  (`all`/`followed`/`follower`/`none`) is honoured. New subscriptions default every alert to
  `true`. Upstream Mastodon defaults them to `false`, which leaves a fresh
  subscription inert until the client calls update; real clients send explicit
  alerts anyway, so this instance picks the less surprising default.
  **Deployment note**: delivery runs in a separate `push_worker` process (see
  `docs/install.md` for the supervisord entry). If you skip wiring it up, the
  instance still accepts subscriptions and advertises a VAPID key but never
  delivers anything.
- **Scheduled posts**: `POST /api/v1/statuses` with `scheduled_at` queues the
  post instead of sending it, and returns Mastodon's `ScheduledStatus` entity;
  `/api/v1/scheduled_statuses` lists the queue, with `GET`/`PUT`/`DELETE` on a
  single entry (`PUT` changes the publication time, the only field Mastodon
  makes editable). Everything an immediate post supports carries over
  (attachments, CW/sensitive, visibility, language, replies, polls), and the
  instance validates it when you queue it, not when it comes due. Any future
  time is accepted; upstream Mastodon requires at least five minutes out. You
  don't need an extra process: the existing `outgoing_worker` publishes due
  posts as part of its poll, so a queued post goes out within a couple of
  seconds of its time. If publishing fails (say you deleted an attachment in the
  meantime), the worker retries with a growing backoff, then leaves the post in
  the queue instead of dropping it. Rescheduling it with `PUT` gives it a fresh
  set of attempts.
- **Streaming API** (`wss://…/api/v1/streaming`, WebSocket only, no SSE):
  `user`, `user:notification`, `public`, `public:local`, `public:remote`,
  `hashtag`, `direct` and `list` streams, delivering `update`, `status.update`,
  `delete`, `notification` and `conversation` events. Unlike Web Push, this
  needs **no separate process**: the server runs as a single process/event
  loop, so a small in-process task polls committed rows (~1s interval,
  `streaming_poll_interval`) and fans out over the open sockets, with the same
  filtering (mutes, visibility, list membership, `exclusive`) the REST
  timelines apply, since it re-queries through the same functions instead of
  duplicating the logic. `delete` and `status.update` are best-effort over a
  bounded window: the server seeds the newest 200 statuses per table (inbox
  and outbox) at startup, then tracks at most 500 per table, streamed statuses
  included, evicting the oldest. Deleting a status outside that window
  produces no frame, and the client's own list updates on its next REST
  fetch. Subscribing to an unknown `list` id gets an error frame. **Not
  supported**: the `public:*:media` variants and `hashtag:local`. One socket
  may hold at most 64 subscriptions (`hashtag` streams carry a client-supplied
  tag, so the set needs a bound); a 65th `subscribe` gets an error frame and
  is ignored. `streaming_max_connections` (default 32) caps open sockets;
  past it, a new connection closes with code `1013`.
  **Deployment note**: the reverse proxy must forward the WebSocket upgrade on
  this path (see the `location /api/v1/streaming` block in
  `docs/install.md`'s nginx snippet). `streaming_enabled = false` in
  `data/profile.toml` disables the endpoint and removes the advertisement.
- **Lists** (`/api/v1/lists` CRUD, `lists/{id}/accounts`, and the
  `/api/v1/timelines/list/:id` timeline): a local, curated view over
  who you follow; nothing here is federated. `replies_policy`
  (`followed`/`list`/`none`) and `exclusive` are both enforced:
  `replies_policy` filters which replies a list's own timeline shows (matching
  Mastodon's semantics: a reply always shows if it's a self-reply or a reply to
  you, and `list` also shows a reply to another list member), and `exclusive`
  removes a list's members from the home timeline (their own list, and your own
  posts, are unaffected). As in Mastodon, you can only add accounts you follow.
- **Other implemented endpoints**:
  - OAuth and apps: `POST /api/v1/apps`, `GET /api/v1/apps/verify_credentials`,
    `/oauth/authorize`, `/oauth/token`, `/.well-known/oauth-authorization-server`.
  - Instance: `/api/v1/instance`, `/api/v2/instance`, `/api/v1/custom_emojis`
    (the instance's real emoji set), `/api/v1/preferences`,
    `/api/v1/announcements` (empty), `/api/v1/streaming/health`.
  - Accounts: `relationships`, `lookup`, `:id`, `:id/lists`, and
    `:id/endorsements` plus `/api/v1/endorsements` (both empty).
  - Statuses: `:id/source`, `:id/context`, `/api/v1/bookmarks`,
    `/api/v1/favourites`, `/api/v1/tags/:id`.
  - Notifications: `GET /api/v1/notifications/:id` and
    `/api/v1/notifications/requests/merged`.
  - Write endpoints (statuses, poll votes, lists, follow, mute) fall back to
    the query string for params the body lacks, as Rails does. Ice Cubes votes
    with `POST /api/v1/polls/:id/votes?choices[]=0`, for example.

## What doesn't (single-user degradations)

microblog.pub is one instance with one actor, and several Mastodon API areas
cover things a single-user server has no data for. Reads mostly return an
empty list so clients render an empty state; writes with nothing behind them
return `404` (no route) or `422`:

- **Filters, suggestions, the directory, trends, and familiar
  followers**: always empty. Filter writes have no route (`404`).
- **Profile editing**: `PATCH /api/v1/accounts/update_credentials` doesn't
  exist (`404`); edit `data/profile.toml` instead. See the 4.6 bullet for the
  `/api/v1/profile` writes.
- **Followed hashtags**: `/api/v1/followed_tags` is empty and
  `/tags/:id/follow`/`unfollow` are `404`.
- **No route (`404`)**: `DELETE /api/v1/conversations/:id`,
  `POST /api/v1/reports`, and domain-block writes (`POST`/`DELETE
  /api/v1/domain_blocks`).
- **Profile curation (Mastodon 4.4)**: endorsing accounts
  (`/accounts/:id/endorse`, `/unendorse`) and featuring hashtags
  (`/tags/:id/feature`, `/unfeature`) return 404, since there is no storage
  behind them (featured tags come from `profile.toml`) and no one to curate
  for. `delete_media` on `DELETE /api/v1/statuses/:id` is also ignored.
- **Federated peers** (`/api/v1/instance/peers`): always empty, as a privacy
  choice. The data exists, but the instance doesn't publish which servers
  you've federated with.
- **Notification requests / policy**: this server never filters notifications,
  so the filtered-notifications queue (`/api/v1/notifications/requests`) is
  always empty and the policy (`/api/v2/notifications/policy`) always reports
  "accept everything"; nothing is held back for approval. `PUT
  /api/v2/notifications/policy` returns `200` and discards the body.

## Scopes

Standard Mastodon OAuth scopes are supported, including the granular
`read:*`/`write:*` forms. A token granted the top-level `read`/`write`/`follow`
scope satisfies any of the matching granular scopes underneath it, as in
Mastodon. Most apps request a broad `read write follow push` by default; `push`
also gates the push subscription endpoints above.

## Troubleshooting

- **A client shows "not mocked"/network errors on first login**: check that
  you entered your bare domain (no `https://`, no trailing slash) in the app's
  "instance" field.
- **Nothing shows up on first sync**: some clients only backfill a page or two
  of history on first login; pull to refresh.
- **Push notifications never arrive**: confirm the `push_worker` process is
  running (`supervisorctl status`) and check `data/push.log` for delivery
  errors. If it's running and logging clean 2xx/201 responses but nothing
  shows up on the device, check that the `server_key` your client
  subscribed with still matches `/api/v1/instance`'s
  `configuration.vapid.public_key`. A regenerated VAPID key invalidates
  every existing subscription, and the client needs to re-subscribe.
- **Streaming never connects (client stuck "connecting…")**: check that the
  reverse proxy has a dedicated location forwarding the WebSocket upgrade for
  `/api/v1/streaming`. A generic `proxy_pass` without
  `proxy_set_header Upgrade`/`Connection` accepts the TCP connection and
  then hangs, since ordinary HTTP proxying doesn't forward the upgrade. Also
  check that `proxy_read_timeout` is generous: an idle socket dying at the
  default 60s looks to the client like an unexplained disconnect.
- If a Mastodon client relies on something that 404s instead of degrading
  gracefully, please [report an
  issue](https://github.com/toniher/microblog.pub/issues). The API surface
  above is what's implemented today and can grow.
