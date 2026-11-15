# Architecture summary

This is a short summary of the design ideas behind the watchdog. It describes the approach, not any particular installation.

## One signal per site, chosen for what is actually available

There is no single ideal measurement that works everywhere. Each monitored site reports the best signal it can honestly provide:

- **Where the output can be tapped** (for example a multicast copy of the channel), the detector decodes the real output and looks for frozen picture, black and silence. This is the strongest signal.
- **Where there is no tap at all**, the detector falls back to software evidence: the playout system's own playlist durations compared with the timing of the player processes it starts. It detects that the engine stalled, not that the picture is bad, and the documentation says so rather than hiding it.
- **Where the player's own files are readable**, the detector adds the real "now playing" item and the engine's CPU activity. This proves the engine is working, not that the picture is moving.

Whatever the signal, the sender reports it in the same small format (a heartbeat plus classified events), so the listener does not care how a site measures.

## Hard invariants

- **Recovery authority belongs to exactly one system.** The watchdog detects and alerts. It performs no automatic recovery, and it never shares that authority with another agent on the same machines.
- **A missing heartbeat means "connectivity unconfirmed", never "the playout is broken".** It is not grounds for any automated action on its own.
- **Match on the most robust signal available.** For example, a player is identified by its executable path, not its process name, because process names turned out to be renamed per stream and name matching produced a false negative.
- **Alerts carry a time limit on silence.** The listener judges "three missed heartbeats" itself, holds an alarm until the condition clears, and has a startup grace period so that restarting the listener does not cause a storm of false alarms.

## Two keys, two jobs

Senders hold write keys. The public dashboard carries only a derived read key, so the page can never be used to post data. Keys can be rotated while a dashboard is open, using a short window in which both the old and the new key are accepted.

## A standby that watches the primary

A second listener on another network watches the primary's health. If the primary itself goes silent, the standby takes over alerting, so a failure of the watchdog is not silent either.
