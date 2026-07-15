# iOS Shortcut: Upload to GCP

This is the expected logic for the `Upload to GCP` Shortcut/automation.

If Shortcuts shows both `Upload to GCP` and `Upload to GCP 1`, use `Upload to GCP`.
The older pre-update shortcut was renamed to `Upload to GCP 1` during import.

## Automation

Trigger:

- App: Camera
- Event: Is Closed
- Run Immediately: On

## Shortcut Steps

1. Find Photos
   - Filter: Media Type is Image
   - Filter: Creation Date is in the last 10 minutes
   - Sort by: Creation Date, latest first
   - Limit: 1 photo

2. If Find Photos has no value
   - Show Alert: "No photo from the last 10 minutes is visible to Shortcuts"
     (names the Full Access settings path — a silent stop here is
     indistinguishable from success, which cost a debugging session on 2026-07-14)
   - Stop This Shortcut

3. Get Contents of URL
   - URL: `http://YOUR_GCP_IP/upload`
   - Method: `POST`
   - Headers:
     - `X-API-Key`: your `ANDROID_API_KEY` value from `.env`
     - `X-Client-Platform`: `iOS`
     - `X-Device-Name`: `iPhone`
     - `X-VPN-Required`: `true`
     - `X-Captured-At`: the photo's Creation Date formatted `yyyy-MM-dd HH:mm:ss`
       (lets a delayed/backfilled upload land on the day the photo was TAKEN;
       the server validates and falls back to upload-time dating)
     - `X-Original-Hash` (optional): MD5 of the photo file (Shortcuts "Hash"
       action). Declare it if your Shortcut converts/recompresses the image, so
       dedup and `/reconcile` key on the ORIGINAL file's hash.
   - Request Body: `File`
   - File: the photo from Step 1

   Do NOT use `Request Body: Form` with a `photo` File field. An iOS bug
   silently coerces Shortcut Form file fields to ~50 bytes of text, which the
   server rejects with a 400; the raw-body `File` shape above is the
   workaround the server was built to accept. Because a raw body has no form
   fields, `captured_at` and `original_hash` travel as the headers listed
   above instead.

4. Optional: Get Dictionary from Contents of URL
   - If `status` is `processing_in_background`, do nothing.
   - If `status` is `duplicate`, `already_processing`, or `already_saved_for_retry`, do nothing.
   - Otherwise show a notification with the response.

## Why This Shape

- The 10-minute photo filter prevents Camera-close events from uploading an old latest photo.
- `X-Client-Platform` and `X-Device-Name` let the server label the upload as iPhone even if Apple's User-Agent changes.
- `X-VPN-Required` tells the server to warn in Telegram only when network evidence looks direct/non-VPN. Unknown VPN provider ranges and non-China VPN exit countries do not warn by default.
- The server also ignores repeated triggers for the same photo while analysis is already running.
