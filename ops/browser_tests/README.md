# Browser tests

Real-Chrome tests of the dashboard. Each one starts its OWN local copy of the listener on a spare
port with a throwaway database and a test token, so none of them ever touches production.

| Test | Proves |
| --- | --- |
| `siren_test.mjs` | The siren: silent-siren warning, one tap starts it, sound-on remembered, silence not remembered and re-arms, Test siren, one oscillator ever |
| `offline_test.mjs` | The loading screen; the service worker saves exactly the offline placeholder and the two page shells and nothing that could hold status data; with the server DOWN the app still OPENS from its saved shell and says "Live feed lost" by itself (and `/topology` too); the placeholder still works for a direct visit; when the server returns the open app fills in by itself with no reload |
| `offline_hang_test.mjs` | A connection that accepts and never answers (a ship-style stall): with a saved shell the app opens in about 4 s; a first visit with nothing saved gets the placeholder after about 8 s |
| `offline_diagnosis_test.mjs` | The offline screen tells a dead INTERNET from a dead SERVER (the field bug: a phone on Wi-Fi with no uplink was told the server had not answered): three scenarios, the connectivity checks send nothing (no cookies, no referrer, no key), the removed note is gone, the ClarixTech brand and link are present, and the page returns to the dashboard by itself |
| `dashboard_redesign_test.mjs` | The counters, Core's CPU/load/memory/disk panel (fed by a fake /proc), a stale file name labelled "last reported" with an age that keeps counting between frames, the progress bar, tiles and open details built once and updated in place (selected text survives a live frame), a red alarm, "live feed lost" naming the right cause and recovering by itself, no outside request while healthy, reduced motion, no sideways scroll on a phone |
| `progress_awake_test.mjs` | A progress bar on all six players, always (live, "Length not reported", "Off air", "Live feed lost"), on the dashboard, the topology map and the phone layout; the map says On air / Off air exactly as the dashboard does; the screen stays awake (Wake Lock requested, re-requested when the browser drops it or the page returns, off on purpose and remembered, honest where unsupported); the live stream reopens by itself after a dead connection; the removed wording is gone |
| `key_rotation_test.mjs` | Changing the sender key does not blind an open dashboard: one page stays open through the window (old and new key both accepted, `/status` names who is still on the old key) and through retiring the old key, fetches its new view key by itself and goes live again without reloading; the old sender key and the old view key are refused afterwards |
| `standby_strip_test.mjs` | The standby's strip: a real standby listener watching a fake Core shows a blue "quiet second copy, sends no alerts" strip, turns red ("STANDBY is alerting") when the fake Core stops answering and blue again when it returns, all on the live stream without a reload; the topology view shows it too; a listener with no role (Core) shows no strip |
| `topology_deck_test.mjs` | The command-deck topology: with very long file names and an alarm active, at 1000 to 1920 px wide no card touches another, every line starts and ends on the real card edges, names clamp to 3 lines, live updates keep the same elements (animations keep running); under 980 px, and on phones either way up, the stacked layout with no sideways scroll |

Run (Windows, Chrome installed): `npm install playwright-core` in this folder, then
`node siren_test.mjs <output folder>`. The listener folder and Chrome path are at the top of each
file (`LISTENER_DIR` overrides the listener folder). The siren test uses a fake AudioContext that
refuses sound until the page has been touched, which is Chrome's real rule.
