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
   - Stop This Shortcut

3. Get Contents of URL
   - URL: `http://YOUR_GCP_IP/upload`
   - Method: `POST`
   - Headers:
     - `X-API-Key`: your `ANDROID_API_KEY` value from `.env`
     - `X-Client-Platform`: `iOS`
     - `X-Device-Name`: `iPhone`
     - `X-VPN-Required`: `true`
   - Request Body: Form
   - Form field:
     - Key: `photo`
     - Type: File
     - Value: the photo from Step 1

   - Form field (added 2026-07-14):
     - Key: `captured_at`
     - Type: Text
     - Value: the photo's Creation Date formatted `yyyy-MM-dd HH:mm:ss`
       (lets a delayed/backfilled upload land on the day the photo was TAKEN;
       the server validates and falls back to upload-time dating)

4. Optional: Get Dictionary from Contents of URL
   - If `status` is `processing_in_background`, do nothing.
   - If `status` is `duplicate`, `already_processing`, or `already_saved_for_retry`, do nothing.
   - Otherwise show a notification with the response.

## Why This Shape

- The 10-minute photo filter prevents Camera-close events from uploading an old latest photo.
- `X-Client-Platform` and `X-Device-Name` let the server label the upload as iPhone even if Apple's User-Agent changes.
- `X-VPN-Required` tells the server to warn in Telegram only when network evidence looks direct/non-VPN. Unknown VPN provider ranges and non-China VPN exit countries do not warn by default.
- The server also ignores repeated triggers for the same photo while analysis is already running.
