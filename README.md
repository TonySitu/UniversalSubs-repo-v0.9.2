# UniversalSubs

**Live translated subtitles for anything on your computer.**

Someone speaks Chinese (or any language) in your game's voice chat, on
Discord, or in a video — and a subtitle in *your* language appears on top of
your screen, while they're still talking. That's the whole tool.

---

## How it works (30-second version)

UniversalSubs listens to the sound coming out of your computer, sends the
speech it hears to two AI services over the internet, and shows the
translation as a subtitle floating above your game. The subtitle never
blocks your mouse — clicks go straight through it.

The AI services are **free** for personal use, but they need you to create
your own accounts (like signing up for any website). You do this once,
during setup. Your audio goes directly from your PC to those services under
your own accounts — this tool has no servers of its own and never sees your
data.

**It's smart about what it sends.** Silence costs you nothing: audio is only
transmitted while someone is actually making sound (you'll see 🎙 / 💤 in the
status bar). And if it hears non-stop sound for a whole minute — which is
music or a video, not conversation — the **Music guard** pauses transmission
to protect your free credit and tells you. Between those two features, your
free Deepgram credit realistically lasts *years* of normal gaming.

---

## What you need

- Windows 10 or 11
- Python (free — the setup below covers it)
- Internet connection
- 10 minutes for first-time setup

---

## First-time setup

**Step 1 — Install Python** (skip if you have it)
Download from https://python.org → run the installer →
**IMPORTANT: tick the "Add Python to PATH" checkbox** before clicking Install.

**Step 2 — Get your two free AI keys**
A "key" is a long code that proves the AI service is talking to *your*
account. You'll paste each one into the app once, and it's remembered.

1. **Gemini key** (does the translating):
   https://aistudio.google.com/app/apikey → sign in with any Google account
   → "Create API key" → copy the code.
2. **Deepgram key** (turns speech into text, live):
   https://console.deepgram.com/signup → create an account (no credit card —
   you get $200 of free usage) → "API Keys" → "Create a New API Key" →
   copy it **immediately, it's only shown once**.

Treat both codes like passwords: don't post them in screenshots or chats.
If one ever leaks, delete it on that website and make a new one.

**Step 3 — Install and run**
Double-click **`install_and_run.bat`**. First run installs what's needed
(a few minutes, progress shown); after that the same file — or `run.bat` —
starts the app in seconds.

**Step 4 — In the app**
1. Paste your two keys and press START once — they're saved into Windows'
   own secure password storage. The boxes will look empty next time and the
   label will say "✓ saved". That's normal and good.
2. **"Spoken lang"** = the language people will speak: `zh-CN` Mandarin ·
   `zh-TW` Traditional Chinese · `ja` Japanese · `ko` Korean … or `multi`
   for auto-detect (English/Spanish/French/German/Hindi/Italian/Japanese/
   Dutch/Russian/Portuguese — **auto mode cannot do Chinese**; use zh-CN).
3. **"Translate into"** = your language (English by default).
4. Press **▶ START CAPTIONING**, play any foreign-language video, and watch
   a subtitle appear *while* the person talks, then settle when they finish.

**Step 5 — Your game**
Set the game's display mode to **"Borderless Windowed"** (looks identical
to fullscreen). Subtitles can't appear over "Exclusive Fullscreen" — true
of every overlay app, not just this one.

---

## Pick how subtitles look — three styles

Change anytime in the **Caption style** dropdown; use **Test caption** to
preview each without any audio playing.

- **Bar (bottom)** — one clean subtitle bar near the bottom of the screen,
  like movie subtitles. Text appears while the person talks and corrects
  itself as more words arrive. The default.
- **Chat box** — a see-through panel showing the last few messages, each
  speaker in their own color (`S1 ›` teal, `S2 ›` pink, …). Best for busy
  lobbies where several people talk — conversations stay readable as
  separate lines instead of overwriting each other.
- **Danmaku (sliding)** — finished messages glide across the top of the
  screen, bullet-comment style (弹幕), colored per speaker. The most
  game-native feel; shows completed sentences only.

Speaker colors are automatic and consistent — the same voice keeps the same
color. (Telling voices apart is AI guesswork; very similar voices may
occasionally swap colors.)

## Choosing WHICH apps get subtitled

By default the tool hears **everything** your computer plays. Usually fine —
but for control (subtitle **Discord + your game**, never listen to
**Spotify**):

### Windows 11 — built in, easiest
Pick **"Selected apps only"** → **"Choose apps…"** → tick the apps you want.
(An app appears in the list only if it's open and has made a sound — play
one second of audio and reopen the list if it's missing.)

### Windows 10 — needs one extra step
Windows 10 can't hand one app's audio to a program directly, so use this
trick: **split your apps across two "outputs" and only listen to one.**
Two rooms — the tool's microphone is only in Room A; anything sent to
Room B can't be heard.

**Option 1 — you already own two audio outputs (no installs).**
Headphones + monitor speakers = two rooms. Best for excluding one app:
1. Windows Settings → System → Sound → **App volume and device preferences**
2. Set Spotify's **Output** to the monitor speakers (volume zero if you
   don't want to hear it there)
3. Leave **Capture device** in the app on "(Default speakers)"

**Option 2 — VB-Cable (free, best for "ONLY these apps").**
VB-Cable adds an extra *virtual* output — a Room B in software. Tiny (1 MB),
trusted by streamers for a decade. **Music apps are the #1 thing you'll
want to route away** — music defeats every automatic filter, so keeping it
out of the captured room entirely is the real fix.
1. https://vb-audio.com/Cable → install → restart once
2. Settings → Sound → **App volume and device preferences** → set
   **Discord** and **your game** to output to **"CABLE Input"**; leave
   everything else on your normal headphones
3. To still *hear* those apps: right-click the speaker icon → Sounds →
   **Recording** tab → double-click **CABLE Output** → **Listen** tab →
   tick **"Listen to this device"** → choose your headphones → OK
4. In the app, set **Capture device** to the **CABLE** entry (if no
   subtitles, try the other CABLE entry — naming varies)

## Everyday use

- **Move the subtitle:** tick "Move mode", drag it, untick.
- **Size:** the number box next to Move mode.
- **Engines:** "Streaming" = subtitles while people talk (both keys).
  "Chunked" = subtitle after each sentence (Gemini key only).
- **Music guard** (on by default): pauses transmission when it hears a solid
  minute of non-stop sound (music/videos), resumes automatically at the next
  pause or within 3 minutes. If your friends talk over constant background
  music and you'd rather have every word than save credit, untick it.
- Everything you set is remembered.

## If something's off

- **No subtitles** → read the grey status line at the bottom of the app —
  it says what's happening. A wrong/expired key is the most common cause.
- **Subtitles behind the game** → switch the game to Borderless Windowed.
- **Chinese comes out as gibberish** → "Spoken lang" is on `multi`; set zh-CN.
- **Status shows 🎵 and subtitles stopped** → the Music guard heard non-stop
  sound. Route the music away (see above) or untick Music guard.
- **Busy chat, subtitles update in steps** → normal on the free Gemini tier.
- **Wrong colors between two similar voices** → known limit of speaker
  detection; the words are still right.

## Privacy, plainly

While captioning is ON and sound is detected, that audio is sent to Deepgram
(speech-to-text) and Google Gemini (translation) under **your** accounts.
Silence is never transmitted. On Gemini's free tier, Google may use what's
sent to improve its products. Nothing is sent when the tool is stopped.
Your keys live in Windows Credential Manager (where Windows keeps saved
passwords) — never in a plain file, never shown on screen after saving.

---

*Developers: see `BUILD_WHEEL_ONCE.md` (building the Windows 11 per-app
capture wheel) and `TESTING.md` (release checklist).*
