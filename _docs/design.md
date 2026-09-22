# Role-Based Autonomous Meeting Agents — Technical Design & Architecture

**Stack status: 🔒 LOCKED** (reviewed 2026-09-20). One conditional: the meeting bridge (Attendee) must pass the Phase 0 spike in §4. If it fails, switch to the fallback listed there and re-lock.

## 1. Tech Stack Overview

| Component | Selected Technology | Alternative Considered | Rationale | Status |
| :--- | :--- | :--- | :--- | :--- |
| **Primary Language** | Python 3.12 | Node.js / TypeScript | Native ecosystem support for AI streaming, audio processing, and FastAPI. | 🔒 Locked |
| **API Framework** | FastAPI + WebSockets | Express.js | Async performance and native WebSocket support for streaming audio frames. | 🔒 Locked |
| **LLM Reasoning** | Gemini 3.8 Flash (text) | GPT-4o / Claude | Standup drafting, claim validation, post-meeting summarisation. Cost-efficient. Confirm exact model ID against the API before coding. | 🔒 Locked |
| **Real-Time Voice Engine** | Gemini Live API (`gemini-3.8-live`) | Deepgram + ElevenLabs (cascaded) | Native bidirectional speech-to-speech over WebSockets; no separate STT/TTS hop. 16 kHz PCM in, 24 kHz PCM out; built-in barge-in (`interrupted` flag) and function calling. | 🔒 Locked |
| **Meeting Bot Bridge** | Attendee (self-hosted Docker) | Joinly.ai, Vexa, Recall.ai, custom Playwright bot | Runs Meet/Zoom/Teams in real Chrome and exposes a bidirectional realtime audio WebSocket (base64 16-bit mono PCM at 8/16/24 kHz), which is the raw-audio hook Gemini Live needs. No per-minute vendor fees. See §5. | 🔒 Locked (conditional on Phase 0 spike) |
| **Unified Credential** | Single `GOOGLE_API_KEY` | Multi-provider keys | One key covers reasoning and real-time audio. | 🔒 Locked |
| **Database** | PostgreSQL + `pgvector` | MongoDB / SQLite | Structured storage for agents, roles, sessions, plus embeddings for long-term memory. | 🔒 Locked |
| **Queue / Cache** | Redis | — | Required by Attendee; also usable for session state. | 🔒 Locked |
| **Containerization** | Docker Compose | Local execution | Isolates Attendee (Chrome), Python runtime, Postgres and Redis. | 🔒 Locked |

### Cost note (NFR-03)
No license fees anywhere in the stack. Remaining cost is Gemini API usage (Live audio is billed per minute/hour of audio) plus compute for headless Chrome, one instance per bot.

---

## 2. System Architecture

```text
                               +─────────────────────────+
                               |    roles/*.md Files     |
                               +------------┬------------+
                                            │
                                            v
+──────────────────────+       +─────────────────────────+
| GitHub & Jira APIs   | ────> |  Work Context Builder   |
+──────────────────────+       +------------┬------------+
                                            │ Role + verified facts (JSON)
                                            v
                               +─────────────────────────+
                               |   FastAPI Orchestrator  |
                               |  - Standup Drafter      |
                               |    (Gemini 3.8 Flash)   |
                               |  - Claim Validator      |
                               |  - Live Session Mgr     |
                               +------┬-------------┬----+
                    Audio WebSocket   │             │  Bidirectional WebSocket
              (PCM, base64 JSON)      │             │  (+ tool calls)
                                      v             v
                  +─────────────────────+   +─────────────────────────+
                  | Attendee (Docker)   |   | Gemini Live API         |
                  | headless Chrome bot |   | gemini-3.8-live         |
                  +──────────┬──────────+   +─────────────────────────+
                             │ WebRTC
                             v
                  +─────────────────────────+
                  | Google Meet / Zoom / MS |
                  | Teams call              |
                  +─────────────────────────+
```

The orchestrator relays audio between Attendee and Gemini Live. Attendee's audio settings allow 16 kHz or 24 kHz PCM, so the resampler may reduce to a pass-through (confirm in the spike).

---

## 3. Key Design Decisions

**Grounded standup delivery (NFR-01).** Speech-to-speech gives no text stage to check before audio is spoken, so the two stages are split:
1. The Standup Drafter (Gemini 3.8 Flash) writes the update as text from the GitHub/Jira JSON.
2. The Claim Validator checks every "completed" claim against a commit, PR or ticket ID. Unsupported claims are dropped.
3. The Live model delivers the validated script.

For follow-up Q&A the Live session is given function-calling tools (`get_ticket`, `get_pr`, `get_commit`, and so on) so it looks facts up. Its system instruction requires it to say it doesn't know when a lookup returns nothing (FR-06). Output transcription is enabled and stored as an audit trail.

**Session length.** Gemini Live audio-only sessions are capped at 15 minutes by default. Enable context-window compression and session resumption from Phase 2 onward.

**Barge-in (FR-05).** On the Live API `interrupted` flag, the bridge stops forwarding audio and flushes queued output. It must also clear any audio already buffered in the bot.

**Multi-agent (NFR-05).** One Attendee bot participant and one Gemini Live session per agent. Sessions are isolated, with per-agent GitHub and Jira credentials (fine-grained tokens or a GitHub App, plus a scoped Jira OAuth token), so agents can't reach each other's repos or projects (NFR-04). Deploy one bot per container to avoid audio bleed.

**Role as configuration.** Roles live in `roles/*.md` (frontmatter plus guidelines). Adding a role means adding a file, not code.

---

## 4. Phase 0 Spike: Attendee Exit Criteria

Attendee is locked only if all of the following pass with a real Google Meet:
1. A self-hosted bot joins and is admitted (guest join with host admission, or a dedicated signed-in Google account for the bot).
2. Inbound meeting audio arrives on the WebSocket as PCM.
3. PCM sent back on the WebSocket is heard in the meeting, at the sample rate we intend to use with Gemini (24 kHz preferred).
4. A way to flush or interrupt queued output audio exists (needed for barge-in).
5. Measured speech-to-speech round trip is under 1,000 ms (target under 800 ms).
6. Two bots in the same meeting run without audio bleed.

**Fallback if it fails:** a thin custom bot (Playwright + Chromium + PulseAudio virtual devices) with our own Gemini Live WebSocket. If that is also rejected, Joinly with a cascaded STT, LLM and TTS pipeline is possible, but NFR-02 would have to be revised.

---

## 5. Meeting Bridge Decision Record

| Option | Raw audio in/out | Google Meet | Verdict |
| :--- | :--- | :--- | :--- |
| **Attendee** | Yes, WebSocket PCM | Yes (per docs) | **Selected** |
| Joinly.ai | No. Text-based STT/TTS pipeline only (Whisper/Deepgram in; Kokoro/ElevenLabs/Deepgram out) | Yes | Rejected: cannot carry Gemini Live speech-to-speech |
| Vexa | Transcripts plus TTS "speak"; no raw audio hook found | Yes | Rejected: same limitation |
| Recall.ai / MeetingBaas | Yes | Yes | Rejected: per-minute fees (NFR-03) |

**License caveat:** Attendee is under the Elastic License 2.0 (source-available, not OSI open source). Self-hosting for our own use is free. The license forbids offering the software to third parties as a hosted or managed service. If RBAA later becomes a multi-tenant product sold to customers, get legal review or negotiate an enterprise license with Attendee before then. This is fine for the MVP.

**Sources unverified:** Attendee's README and its docs disagreed on Meet and WebSocket audio support, which is why the §4 spike gates the lock.
