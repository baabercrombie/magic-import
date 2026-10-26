# Magic Import

Forward a school email, newsletter or PDF to one email address, and the events in it show up on your shared iCloud calendar. It is a home-made version of Skylight's "Magic Import" that runs on your ZimaOS server. The AI runs on your own graphics card, so emails never leave your house (except to reach your iCloud calendar).

## What it does

1. Every 2 minutes it checks a mailbox you set up just for this.
2. When a forwarded email arrives, a local AI model (Qwen3-VL 8B, running in Ollama on your RTX 3060) reads the email text plus any PDF or picture attachments and lists the events in it. Scanned PDF pages and photos of flyers work too.
3. For each event it looks at your iCloud calendar on that day. If a matching event is already there, it skips it (marked "duplicate").
4. It adds the rest to the calendar. If "Review before adding" is on, they wait for you to click Approve instead.
5. A web page at `http://YOUR-ZIMA-IP:8088` shows every email it handled, when, and what it did. From there you can:
   - edit an event (the calendar is updated too)
   - remove an event from the calendar, or undo everything from one email
   - add a duplicate anyway, or skip an event
   - have the AI read an email again
   - add an event the AI missed
   - upload a PDF or picture, or paste text, without emailing it
   - turn "Review before adding" on or off

Only emails sent **from** the addresses in `ALLOWED_SENDERS` are read. Anything else is listed as "ignored" and never touches the calendar.

---

## Part 1: Get three things ready

### 1. A new email address for the app
A fresh Gmail account is easiest (for example `smithfamily.calendar@gmail.com`).

1. Create the Gmail account.
2. Turn on 2-Step Verification: https://myaccount.google.com/signinoptions/twosv
3. Make an app password: https://myaccount.google.com/apppasswords. Name it "Magic Import". Google shows a 16-letter password. Copy it; that is your `IMAP_PASSWORD`.

### 2. An Apple app-specific password
The public share link of an iCloud calendar is read-only, so the app logs in as someone who can **edit** the calendar: the owner, or a person it was shared with who has edit rights.

1. Go to https://account.apple.com and sign in with that Apple ID.
2. Open **Sign-In and Security > App-Specific Passwords**, click **+**, and name it "Magic Import".
3. Copy the password (looks like `abcd-efgh-ijkl-mnop`). That is your `ICLOUD_APP_PASSWORD`.
4. Write down the calendar's name exactly as it appears in the Calendar app. That is your `ICLOUD_CALENDAR_NAME`.

### 3. The addresses you'll forward from
Usually yours and your partner's. These go in `ALLOWED_SENDERS`.

---

## Part 2: Install on your Zima

1. **Open a terminal on the Zima.** From a Mac, open Terminal. From Windows, open PowerShell. Type this, using your Zima's IP address and your ZimaOS login name, then enter your ZimaOS password when asked:
   ```
   ssh YOUR-ZIMA-USERNAME@YOUR-ZIMA-IP
   ```
   (If it can't connect, turn SSH on in the ZimaOS settings first.)

2. **Check the graphics card.** Paste:
   ```
   nvidia-smi
   ```
   You should see a table that mentions "RTX 3060". If you get "command not found", the NVIDIA driver isn't set up on ZimaOS yet. Install it first using ZimaOS's GPU instructions, then come back.

3. **Download the settings file.** Paste these three lines:
   ```
   sudo mkdir -p /DATA/AppData/magic-import
   cd /DATA/AppData/magic-import
   sudo curl -fsSL -o docker-compose.yml https://raw.githubusercontent.com/baabercrombie/magic-import/main/docker-compose.yml
   ```

4. **Fill in your settings.** Paste:
   ```
   sudo nano docker-compose.yml
   ```
   Use the arrow keys to move. In the section marked **FILL IN THE VALUES BELOW**, replace each example value with yours, keeping the quote marks. To save: press `Ctrl+O`, then `Enter`. To exit: press `Ctrl+X`.

5. **Start it.** Paste:
   ```
   sudo docker compose up -d
   ```
   It downloads the app and the AI engine. It starts again by itself whenever the Zima reboots.

6. **Wait for the AI model.** The first time, the app downloads the AI model (about 6 GB). Open `http://YOUR-ZIMA-IP:8088/setup` in a browser. The "AI (local)" line says it's downloading, and turns ✅ when done (often 5 to 20 minutes, depending on your internet).

7. **Check everything.** On the same Setup check page, every line should have a ✅. If the calendar line has a ❌, the page lists the calendar names it can see. Copy the right one into `ICLOUD_CALENDAR_NAME` (step 4), then run `sudo docker compose up -d` again.

8. **Try it.** Forward a school email to the new address, wait about 2 minutes (or click **Check mail now**), and watch it appear on the home page.

Suggestion: turn **Review before adding** on for the first week, so you can see what the AI finds before anything lands on the calendar.

---

## Everyday tasks

All of these start with logging in (`ssh YOUR-ZIMA-USERNAME@YOUR-ZIMA-IP`) and then:
```
cd /DATA/AppData/magic-import
```

**Get the latest version of the app:**
```
sudo docker compose pull
sudo docker compose up -d
```
This never touches your settings or history.

**Change a setting:**
```
sudo nano docker-compose.yml
sudo docker compose up -d
```

**See the app's log** (useful if something seems stuck):
```
sudo docker logs --tail 100 magic-import
```

**Check the AI is using the graphics card** (while it is reading an email):
```
sudo docker exec magic-import-ollama nvidia-smi
```
You should see an "ollama" process using several GB of memory.

**Stop it:**
```
sudo docker compose down
```

**Back up.** Everything the app remembers is in the `data` folder inside `/DATA/AppData/magic-import`. Copy that folder somewhere safe. (The `ollama` folder holds the downloaded AI model; it can be downloaded again, so no need to back it up.)

---

## Good to know

- **Speed.** On an RTX 3060 an email usually takes 10 to 60 seconds. Emails with many scanned pages take longer.
- **How duplicates are spotted.** It looks at your calendar on the same day and compares titles. "Picture Day" and "Picture day!" count as the same; for timed events the start times also have to be within 90 minutes. If it wrongly calls something a duplicate, open it and click **Add it anyway**.
- **Year guessing.** If an email says "Nov 5" with no year, the AI picks the next upcoming Nov 5.
- **Read or unread doesn't matter.** The app looks at the last 3 days of mail in the inbox and remembers what it has already handled, so opening the mailbox in Gmail yourself won't confuse it.
- **Events it creates** have "Added by Magic Import from: (email subject)" at the bottom of their notes, so you can always tell where one came from.
- **Supported attachments:** PDF (typed or scanned), JPG, PNG, GIF, WebP pictures, plain text and .ics files. Word documents are skipped (the web page notes which files were skipped).
- **If you delete an event in the Calendar app yourself**, the app doesn't notice until you click Remove on it (which then just marks it removed).
- **Using Claude instead.** If you ever want a stronger cloud AI, set `AI_PROVIDER` to `"claude"`, `AI_MODEL` to `"claude-opus-5-5"`, and put a key from https://console.anthropic.com in `ANTHROPIC_API_KEY`. Each email then costs a few cents.
