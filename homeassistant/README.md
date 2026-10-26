# Magic Import + Home Assistant

What you get:
- **Phone notifications** the moment an email is read. Each event comes with **Approve** and **Skip** buttons. If review mode is off, you get an "Added to calendar" notification with an **Undo** button instead.
- **A dashboard card** that lists what's waiting, with Approve all / Skip all, plus per-event Approve / Skip / Undo buttons.
- `sensor.magic_import_waiting`: how many events are waiting, so you can build your own automations.

Phone notifications need the Home Assistant Companion app on the phone.

Turn on **Review before adding** (on the Magic Import home page, or `REVIEW_MODE: "true"` in the compose file) so events wait for your Approve tap.

---

## Step 1: Tell the app where Home Assistant is

In your Magic Import compose settings, fill in these two lines (`HA_IP` = your Home Assistant's IP address, `ZIMA_IP` = your Zima's):

```
HA_WEBHOOK_URL: "http://HA_IP:8123/api/webhook/magic_import_q8v3k2"
APP_URL: "http://ZIMA_IP:8088"
```

Then redeploy the app (in ZimaOS: save the app's settings, or `sudo docker compose up -d`).

## Step 2: Add the Home Assistant file

1. Install the **File editor** add-on (Settings > Add-ons > Add-on store) if you don't have a way to edit files yet, and open it.
2. Open `configuration.yaml` and add these two lines at the very top:
   ```
   homeassistant:
     packages: !include_dir_named packages
   ```
   If the file already has a line that says `homeassistant:`, don't add a second one. Just put the `packages:` line (indented 2 spaces) under the existing one.
3. Create a folder called `packages` next to `configuration.yaml`, and inside it a new file called `magic_import.yaml`.
4. Paste in everything from [`magic_import.yaml`](magic_import.yaml), then replace:
   - `ZIMA_IP` with your Zima's IP address (5 places)
   - `notify.YOUR_PHONE` with your phone's notify action (3 places). To find it, go to **Developer tools > Actions** and type `notify.mobile_app`. Yours looks like `notify.mobile_app_burkes_iphone`.
5. Go to **Developer tools > YAML**, click **Check configuration**, then **Restart**.

To notify two phones, make a notify group, or copy each `notify.YOUR_PHONE` block and put the second phone in the copy.

## Step 3: Add the dashboard card

Open a dashboard, click the pencil (Edit) > **Add card** > **Manual**, and paste everything from [`dashboard_card.yaml`](dashboard_card.yaml). Replace `ZIMA_IP` (1 place) and save.

The bottom part of the card (per-event buttons) shows the app's page inside Home Assistant. It works when your phone or computer is on your home network. If you open Home Assistant through a secure `https://` address from outside (for example Nabu Casa), that part stays blank, but the notifications and the Approve all / Skip all buttons still work.

---

## If you set a web page password (UI_PASSWORD)

Home Assistant then has to log in too. Add these two lines under the `resource:` line and under each `url:` line in `magic_import.yaml` (indented the same as `url:`):
```
    username: ha
    password: YOUR_UI_PASSWORD
```

## Testing it

Forward a school email, or upload a PDF on the Magic Import web page. Within a minute or two your phone should get one notification per event. Press **Approve** and check the calendar.

If nothing arrives, open the Magic Import **Activity** page. A line saying "Could not notify Home Assistant" means `HA_WEBHOOK_URL` is wrong or Home Assistant can't be reached from the Zima.
