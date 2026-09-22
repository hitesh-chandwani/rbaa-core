# Role-Based Autonomous Meeting Agents — Product Requirements & Implementation Plan

## 1. Project Overview & Product Vision

The goal is to build a platform that instantiates AI agents with defined professional roles (e.g., Senior Software Engineer, Product Manager, Quality Assurance Engineer). Each agent's identity, communication style, and rules are defined via Markdown files (e.g., `software_engineer.md`).

The platform fetches real work context from tools like GitHub and Jira, prepares standup updates, and deploys agents into live meetings (Google Meet, Zoom, Teams). Agents participate via real-time spoken voice, answer follow-up questions, handle human interruptions, and summarize outcomes.

### Core Design Principles
1. **Role as Configuration:** Adding a new professional identity must require creating a `.md` file, not writing new application code.
2. **Factual Grounding:** Agents must rely on verified external work activity (commits, PRs, tickets) and never invent work.
3. **Separation of Scope:** Clear boundaries between Role (Identity), Agent (Instance Configuration), and Session (Runtime Participation).

---

## 2. Functional Requirements (FR)

### FR-01: Role Definition (.md Format)
- The system MUST load agent role definitions from Markdown files containing structured frontmatter and operational guidelines.
- The role definition MUST specify job responsibilities, communication style, standup duration limits, default voice selection, and tool permissions.

### FR-02: Agent Instance Creation
- The platform MUST support creating distinct agent instances mapped to a specific role file and external identity credentials (e.g., GitHub username, Jira assignee ID).

### FR-03: Work Context Gathering
- Before entering a meeting, the system MUST pull recent work activity from connected systems (GitHub, Jira).
- The system MUST summarize commits, merged PRs, open reviews, and assigned ticket statuses into a structured JSON payload to feed into the agent's context.

### FR-04: Meeting Attendance & Real-Time Voice Participation
- The agent MUST be able to join scheduled video meetings (Google Meet, Zoom, MS Teams).
- The agent MUST send and receive low-latency spoken audio directly within the meeting.
- The agent MUST be able to speak its status update clearly within a configured time limit (e.g., 60–90 seconds).

### FR-05: Dynamic Turn-Taking & Interruption (Barge-In)
- The agent MUST continuously listen to incoming meeting audio.
- The agent MUST detect when it is spoken to or when its name/role is called.
- If a human speaks over the agent while the agent is talking, the agent MUST immediately cease speaking and yield the floor.

### FR-06: Question Handling & Multi-Turn Conversation
- The agent MUST answer direct follow-up questions based on its role identity and fetched work context.
- If an agent lacks information to answer a question, it MUST explicitly acknowledge the missing context rather than fabricating an answer.

### FR-07: Meeting Memory & Artifact Generation
- Following a meeting, the system MUST compile the meeting transcript into structured outcomes: summary, decisions, action items, and blockers.
- The system MUST persist key meeting outcomes to long-term memory for future standup context.

---

## 3. Non-Functional Requirements (NFR)

### NFR-01: Reliability & Anti-Hallucination
- The agent MUST enforce strict guardrails: completed work claims require verifiable ticket or commit evidence.

### NFR-02: Sub-Second Audio Latency
- The roundtrip voice interaction latency (speech-to-speech) MUST remain under 1,000 ms (target: <800 ms) to preserve natural conversational dynamics.

### NFR-03: Cost Optimization
- The platform MUST avoid per-minute meeting bot vendor fees where possible, utilizing open-source or self-hosted meeting bridges and a single unified LLM API key.

### NFR-04: Security & Permission Isolation
- Tool execution credentials (OAuth tokens, API keys) MUST be isolated per agent. Agents MUST NOT access unauthorized repositories or Jira projects.

### NFR-05: Scalability
- The system architecture MUST support running multiple agents simultaneously in the same meeting or across different meetings without code changes.

---

## 4. MVP Scope & Boundaries

### In Scope for MVP
- **1 Recurring Meeting:** Engineering Daily Standup (Google Meet).
- **3 Defined Roles:** Product Manager, Senior Software Engineer, Quality Assurance Engineer.
- **Integrations:** GitHub (PRs/Commits) + Jira (Tickets/Blockers).
- **Voice Engine:** Bidirectional real-time audio interaction.
- **Execution:** Automated join, standup update delivery, follow-up Q&A, post-meeting transcript summary.

### Out of Scope for MVP
- Video avatar rendering / visual synthesized video.
- Multi-tenant web UI dashboard (CLI/file-based configuration acceptable for Phase 1).
- Complex multi-cluster Kubernetes deployment (Docker Compose is sufficient for MVP).

---

## 5. Implementation Roadmap & Phases

```text
Phase 0: Environment & Bot Bridge Spike
 └── Deploy self-hosted Attendee (Docker + Postgres + Redis) and run the design.md §4 spike
     against a real Google Meet: join/admission, inbound PCM, outbound PCM, output flush
     for barge-in, <1,000 ms round trip, two bots without audio bleed.
     Gate: pass = Attendee stays locked; fail = switch to fallback in design.md §4.

Phase 1: Role Loader & Context Parser
 └── Build markdown frontmatter parser and async GitHub/Jira activity fetchers.

Phase 2: Gemini Live Audio Integration
 └── Implement WebSocket audio bridge (PCM 16kHz in / 24kHz out, resample only if the spike requires it)
     with Gemini Live API. Enable context-window compression + session resumption (15-min audio cap).

Phase 3: End-to-End Voice Standup Execution
 └── Connect Attendee meeting audio to the Gemini Live bridge; add standup drafter + claim validator
     (design.md §3); test spoken standup delivery.

Phase 4: Multi-Agent Orchestration & Barge-In
 └── Run SWE, PM, and QA agents sequentially in one call; handle human interjections.

Phase 5: Persistence & Memory
 └── Save transcripts, action items, and standup history to PostgreSQL.