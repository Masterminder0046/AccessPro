# AssessPro — Smart Recruitment Assessment Platform

**AssessPro** is a modern corporate online MCQ examination and proctoring web application engineered for campus placements, corporate recruitment drives, and academic assessments. Inspired by industry-standard enterprise systems like **TCS iON** and **Infosys Springboard**, AssessPro delivers a reliable, distraction-free examination engine with robust browser-based anti-malpractice deterrence, individual question timers, server-authoritative countdowns, automated evaluation, live proctoring telemetry, and **Common Assessment Links with Per-Candidate Independent Randomization**.

---

## 1. Architecture & Design Principles

AssessPro is intentionally engineered with a clean, decoupled **two-Python-file architecture** to ensure maximum maintainability, zero bloated dependencies, and complete separation of concerns:

```
AssessPro/
├── app.py                  # ALL Flask routes, authentication, business logic, question parsing, timers, monitoring, evaluation, and randomization
├── database.py             # ALL SQLAlchemy database models, relationships, and schema definitions
├── requirements.txt        # Python dependency specifications
├── README.md               # Complete setup, architecture, and operation guide
├── instance/
│   └── assesspro.db        # SQLite database (auto-created on startup)
├── templates/
│   ├── base.html           # Enterprise admin layout, responsive sidebar, navigation
│   ├── login.html          # Administrator authentication portal
│   ├── admin.html          # Operational dashboard & KPI analytics
│   ├── assessments.html    # Assessment portfolio, builder, policies & assignments
│   ├── questions.html      # Question bank & Bulk Smart Parser with live preview
│   ├── candidates.html     # Candidate directory & unique exam invitation links
│   ├── instructions.html   # Candidate pre-examination checklist & policy agreement
│   ├── exam.html           # TCS iON-inspired distraction-free examination cockpit
│   ├── monitoring.html     # Live real-time examination proctoring telemetry
│   └── results.html        # Candidate submission receipt & Admin audit evaluation
└── static/
    ├── style.css           # Modern Enterprise Design System stylesheet
    ├── admin.js            # Admin modal dialogs, bulk parser, and live polling
    └── exam.js             # Candidate exam cockpit, timer engine & anti-cheat hooks
```

---

## 2. Key System Capabilities

### A. Common Assessment Link (`/exam/<slug>`)
- **Single Shared Link**: Admins can distribute one shared URL (e.g. `/exam/gse-2026` or `/exam/join/gse-2026`) to multiple candidates across recruitment drives.
- **Admin Controls**:
  - Enable / disable shared link with instant toggle controls.
  - Optional examination access code / passkey (e.g. `PASS2026`).
  - Active date and expiry window enforcement.
  - Configurable maximum allowed attempts per candidate (default 1).
  - Enable/disable duplicate attempts and candidate self-registration.
- **Shared Link Telemetry**:
  - Live tracking of link visits, registered candidates, started attempts, and completed attempts.

### B. Per-Candidate Independent Question & Option Randomization
- When each candidate starts an examination, an **independently randomized question paper** is generated on the server:
  - **Random Question Ordering**: Distinct question sequences for each candidate.
  - **Random MCQ Option Ordering**: Shuffled option cards (`A`, `B`, `C`, `D`) for every question.
  - **Section-Wise Randomization**: Shuffles questions within sections (Technical, Aptitude, Verbal).
  - **Difficulty Distribution**: Consistently balances difficulty levels across all candidate papers.
  - **Evaluation Accuracy**: Options are evaluated via unique primary keys, guaranteeing 100% scoring precision regardless of presentation order.
  - **State Persistence**: The exact question sequence and option mappings (`question_order_json`, `option_order_json`) are stored in the database. Refreshing or reconnecting **never** alters the generated paper.

### C. Administrative Portal & Operational Dashboard
- **Dashboard Analytics**: High-level KPIs including Active Exams, Registered Candidates, Evaluated Submissions, Pass Rates, Average Scores, and Live Proctoring Sessions.
- **Assessment Management**: Create, edit, publish, and delete assessments with custom duration, passing benchmark, negative marking, question randomization, option randomization, and backtracking permissions.
- **Candidate Directory**: Add candidates individually or import hundreds via standard CSV (`candidate_id,name,email,department,phone,college`).
- **Unique Examination Links**: Supports both shared common URLs and individual candidate tokens.

### D. Bulk Question Smart Parser (Textarea & CSV)
- Paste 10, 50, or 100 questions into **ONE single textarea**:
  ```
  1. What is Python?
  A) Database
  B) Programming Language
  C) Browser
  D) Operating System
  Answer: B
  Time: 30
  Marks: 2
  Negative: 0.5
  ```
- **Interactive Editable Preview**: Inspect parsed questions, review validation errors/warnings in real-time, adjust marks, remove rows, and import directly to the Question Bank or attach to an assessment.

### E. Candidate Examination Cockpit (TCS iON / Infosys Springboard Inspired)
- **Corporate UI/UX**: Distraction-free light theme with high-contrast typography, clear status indicators, and focus-locking layout.
- **Dual Timers**:
  - **Total Examination Timer**: Authoritative server-side expiration (`due_at`). Refreshing never resets the timer; auto-submits on timeout.
  - **Individual Question Timer**: Enforces per-question countdowns with automatic saving and advancement upon timeout.
- **TCS Question Palette (Color-Coded)**:
  - 🟢 **Answered** (Green)
  - 🟠 **Not Answered** (Orange)
  - 🟣 **Marked for Review** (Purple)
  - ⚪ **Not Visited** (Slate/Gray)
- **Asynchronous Auto-Save**: Candidate choices and review flags are continuously synchronized with the backend.

### F. Automated Anti-Malpractice Proctoring (Webcam-Free Deterrence)
- **Strict Fullscreen Enforcement**: Exiting fullscreen mode triggers automated incident logging.
- **Tab Switching & Focus Loss**: Captures `visibilitychange` and window `blur` events.
- **Input Locking**: Prevents right-click context menu, clipboard cut/copy/paste, and DevTools shortcuts (F12, Ctrl+Shift+I, view-source).
- **Configurable Violation Escalation**:
  - 1st Violation: Warning notification banner.
  - 2nd Violation: Critical alert banner.
  - 3rd Violation (Threshold): Automatic examination lock, termination, and immediate evaluation.

### G. Live Examination Monitoring & Telemetry
- Real-time telemetry table auto-refreshes every 3.5 seconds via asynchronous polling.
- Displays active candidates, remaining time, current question index, connection heartbeat status, client IP, and violation counts.
- Administrators can remotely terminate any session with an official reason.

---

## 3. Quickstart & Installation

### Step 1: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 2: Configure Turso Database
Copy `.env.example` to `.env` and configure your Turso credentials:
```bash
cp .env.example .env
```
In `.env`:
```env
TURSO_DATABASE_URL=libsql://assessment-approtech-tech.aws-us-west-2.turso.io
TURSO_AUTH_TOKEN=your_newly_rotated_turso_auth_token_here
SECRET_KEY=assesspro-enterprise-secret-key-2026-xyz
```
> **Security Notice**: Database tokens are never committed to version control or exposed in frontend code. AssessPro loads credentials exclusively through environment variables / `.env`.

### Step 3: Run the Application
```bash
python app.py
```
Upon startup with `TURSO_AUTH_TOKEN` configured, AssessPro connects directly to Turso via the `sqlalchemy-libsql` dialect, initializes remote tables, seeds the default administrator account, populates 20 comprehensive questions across 3 sections, and registers 5 demo candidates in TursoDB.

### Step 4: Access the Application
Open your browser and navigate to:
```
http://localhost:5000
```

---

## 4. Default Credentials & Pre-Loaded Data

Upon initial launch, the system automatically provisions:

- **Admin Login**:
  - **URL**: `http://localhost:5000/login`
  - **Username**: `admin`
  - **Password**: `admin123`
- **Sample Assessment**:
  - **Title**: Graduate Software Engineer Assessment 2026 (`GSE-2026-T1`)
  - **Common Exam Link**: `http://localhost:5000/exam/gse-2026`
  - **Duration**: 45 Minutes
  - **Passing Benchmark**: 60%
  - **Total Marks**: 35.5 Marks
  - **Questions Attached**: 20 Questions (8 Technical, 7 Aptitude, 5 Verbal)
- **5 Pre-Enrolled Demo Candidates**:
  1. `CAND-2026-001` — Rohit Sharma (`rohit.sharma@example.com`)
  2. `CAND-2026-002` — Priya Patel (`priya.patel@example.com`)
  3. `CAND-2026-003` — Ananya Sen (`ananya.sen@example.com`)
  4. `CAND-2026-004` — Vikram Malhotra (`vikram.m@example.com`)
  5. `CAND-2026-005` — Sneha Reddy (`sneha.reddy@example.com`)

---

## 5. Verification of the 9 Acceptance Criteria

The system has been verified against all 9 acceptance criteria specified in `prompt.txt`:

1. ✅ **All candidates use the same URL**: All 5 candidates navigate to `http://localhost:5000/exam/gse-2026`.
2. ✅ **Each candidate gets an independently randomized paper**: Verified distinct question sequences and shuffled option cards for every candidate.
3. ✅ **All papers follow configured section and difficulty rules**: Each paper consistently delivers 8 Technical, 7 Aptitude, and 5 Verbal questions.
4. ✅ **Question order remains unchanged after refresh**: Verified that repeated browser reloads preserve the exact persisted question order.
5. ✅ **Option randomization does not break answer evaluation**: Answer evaluation evaluates by `Option.id` and verifies `is_correct` with 100% precision.
6. ✅ **Individual timers remain attached to the correct questions**: Each question preserves its individual duration limit and marks regardless of position.
7. ✅ **All candidates receive equivalent maximum marks**: All candidate papers total 35.5 marks.
8. ✅ **Duplicate attempts are rejected according to settings**: Re-submitting from a completed candidate redirects to the submission receipt without permitting duplicate attempts.
9. ✅ **Admin can access individual results**: Administrators can view detailed question breakdowns, candidate choices, correct keys, and malpractice logs.

---

## 6. License
Designed and developed for enterprise recruitment platforms, college placement drives, and high-stakes corporate online assessments.
