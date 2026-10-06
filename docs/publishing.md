# Calendar, images and Telegram scheduling

Open **Calendar**, the fourth sidebar icon, for month, week or list views. Filter by channel/status, search the text and select a timezone. Mobile starts with the list. Calendar includes schedules created in TGhost; it does not import unrelated Telegram schedules.

## Schedule a draft

1. Create or edit a draft in Studio. Add still JPEG, PNG or WebP images with **Add images**, drop files into the image area, or focus that area and paste an image. Each upload is limited to 10 MB and 25 million pixels. Images are decoded, orientation is corrected, metadata is removed and photos are normalized to JPEG (maximum 2560 pixels per side).
2. Arrange, replace or remove images; a photo album supports up to 10. Images belong to saved draft versions. Removing one from the current version does not remove it from older versions or scheduled snapshots.
3. Press **Schedule** next to Copy. Unsaved changes are saved as a new version first. In Calendar, an empty day/time slot or **Schedule post** opens the draft picker.
4. Choose a channel, date, time and IANA timezone. Publishing permission is separate from access to analytics or References. The connected Telethon account must be a creator or have the channel's posting right.
5. For image posts, choose a caption or **images, then a separate text message**. The latter previews both messages explicitly; Telegram receives the text one second after the photos. The server checks the account's Telegram caption limit and the 4096 UTF-16-unit message limit. Text is never truncated or silently split.
6. Review the preview and press **Confirm schedule**. The initial status is **Queued**; only Telegram acceptance changes it to **Scheduled**.

Dates are stored in UTC together with the selected timezone. Nonexistent daylight-saving times are rejected; duplicated local times require selecting an occurrence. A minimum two-minute lead time is checked at confirmation and again immediately before each Telegram operation. Expired transfers cannot publish immediately.

## Change a schedule

Click a calendar card to view the complete post, its activity log, original draft and confirmed Telegram publication link. **Edit / reschedule** changes its text/time after explicit confirmation. **Cancel post** cancels every constituent scheduled message. Draft edits made after scheduling never change the scheduled snapshot. To change its images, cancel the schedule, update the draft and schedule its new version.

Queued posts can be cancelled before transfer starts. A transferring/updating/cancelling operation must settle before another command. Failed operations with no Telegram message IDs may be retried by confirming a new future time. Partial operations and ambiguous network results require reviewing Telegram's queue; they are never blindly resent. If Telegram images were replaced externally, the original asset preview is marked for review rather than silently presented as current.

## Runtime and storage

Run migration **0019_calendar_publishing** explicitly before starting the new app image:

```sh
docker compose --profile migrate run --rm migrate
```

Use the existing `all` community process or the documented split web/worker topology. Only the worker owns the Telethon user session. Web requests write workspace-scoped PostgreSQL commands; the worker checks rights, transfers the schedule to Telegram and records each message ID. Once accepted, Telegram can publish while TGhost is offline. No separate bot or second user session is required.

Images are private PostgreSQL binary assets, protected by authentication and workspace scope; include them in normal database backups. Storage is bounded to 500 MB per workspace and 100 historical uploads per draft. The split topology therefore does not need an additional shared media filesystem.

The worker checks schedule history periodically and consumes Telegram's deletion/publication updates. An item disappearing from the queue is not by itself evidence of publication. When the confirmed mapping was missed while offline, the item stays **Needs review** rather than guessing. A different connected Telegram account cannot manage schedules made by the original account. Channel deletion is blocked until active/uncertain schedules are resolved. If an interrupted operation has no recoverable IDs, the workspace owner can use **Resolve after checking Telegram** only after manually removing every remaining related message from Telegram’s queue. This closes the local record and logs the owner’s confirmation; it does not claim or undo earlier publication.

This version supports text, single images and photo albums. Video, recurring schedules, drag-to-reschedule and importing schedules created outside TGhost are not included.
