# UniversalSubs — Feature Verification Checklist (BYOK build)

Work top to bottom; each test assumes the ones before it passed.
For any failure: note the status-bar message and what you did, and report it.

## 1. Install & launch
- [ ] `install_and_run.bat` → "Core OK." and app launches
- [ ] Step 2/3 says "Per-app capture OK (bundled wheel)"
      (requires the `wheels/` folder — see BUILD_WHEEL_ONCE.md)
      — a NOTICE here is acceptable on this Win10 dev PC; feature is Win11-only
- [ ] Second launch via `run.bat` works without reinstalling

## 2. Key security (keyring)
- [ ] Open `universalsubs_config.json` in Notepad → contains NO api keys
- [ ] Windows: Credential Manager → Windows Credentials → two "UniversalSubs" entries
- [ ] In-app key fields are EMPTY, labels read "✓ saved in Credential Manager"
- [ ] START works with fields left blank (keys load from keyring)
- [ ] Type a junk key, START → captions fail; retype the real key, START → recovers
      (proves "type to replace" works)

## 3. Overlay basics
- [ ] Test caption button → live line appears, then commits to solid style
- [ ] Caption auto-hides ~7s after the last line
- [ ] Move mode ON → caption is draggable; OFF → clicks pass through to windows underneath
- [ ] Font size spinner changes caption size immediately
- [ ] Overlay visible over a game in BORDERLESS WINDOWED (and confirm it does
      NOT show over exclusive fullscreen — expected limitation)

## 4. Chunked engine (Gemini only)
- [ ] Engine = Chunked, play a Mandarin video → caption appears ~1–2s after
      each sentence ends
- [ ] Status shows "translating …s of speech" → "caption shown"
- [ ] Music/game audio alone → no captions (Gemini NO_SPEECH filtering)

## 5. Streaming engine (Deepgram + Gemini)
- [ ] Engine = Streaming, Spoken lang = zh-CN → status reaches
      "streaming — captions appear as people speak"
- [ ] Caption appears WHILE the speaker talks (muted style + "…"), revises
      itself, then commits when the sentence ends
- [ ] No caption ever sticks around unfinished (watchdog: max ~3s after
      speech stops, it commits)
- [ ] Busy audio: captions may update in ~2s steps but never freeze for 10s+
- [ ] Kill your internet briefly → "Deepgram connection lost — retrying" →
      restore → captions resume on their own

## 6. Capture device selector (Win10 per-app routing)
- [ ] Dropdown lists your real output devices; "(Default speakers)" works as before
- [ ] Install VB-Cable → reopen dropdown (no app restart) → CABLE entries appear
- [ ] Route a browser to CABLE Input (Settings → Sound → App volume and device
      preferences), enable Listen on CABLE Output so you still hear it
- [ ] Select the CABLE loopback device in-app → browser audio captions;
      Spotify on normal speakers does NOT
- [ ] Select a device, unplug/remove it, START → warning + graceful fallback
      to default speakers

## 7. Per-app capture (Windows 11 only — needs a Win11 tester or VM)
- [ ] "Choose apps…" lists apps currently playing audio
- [ ] Selecting one app captions ONLY that app
- [ ] Two apps selected → both caption; unselected app is ignored
- [ ] Selected app not running at START → warning names it, others proceed
- [ ] On Win10: "Selected apps only" fails with a clear status message and
      system audio still works (graceful degradation)

## 8. Settings persistence
- [ ] Change model, languages, engine, device, font size → STOP → close app →
      relaunch → all restored
- [ ] Selected apps list survives relaunch (by name)

## 9. Repo hygiene (before pushing)
- [ ] `git status` does NOT list universalsubs_config.json
- [ ] Search the repo for your keys (Ctrl+Shift+F in PyCharm) → zero hits
