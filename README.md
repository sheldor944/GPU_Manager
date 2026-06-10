# Lab GPU Manager

A small full-stack web app to coordinate GPU usage in a research lab:

- Google sign-in for all members of the lab.
- Each user has a **GPU-hour quota** (set by the PI; default configurable).
- Users **request a GPU** for N hours. If the GPU is free → it becomes active
  immediately. If it's busy → they're queued.
- Queue priority: people **within quota** get served before people **over quota**
  (then FIFO). So heavy users naturally yield to others without being blocked.
- When the active session ends (early release or expiry), the **next person is
  offered the slot** via email + in-app notification, with a confirmation window.
- The PI gets an **admin dashboard** to manage users (quota, role,
  enable/disable), GPUs (add/remove, mark maintenance), and to **force-release**
  stuck sessions.

Stack: FastAPI + SQLite + SQLAlchemy + Jinja2 + Tailwind (via CDN) + APScheduler.

---

## 1. Setup

```bash
cd /home/sheldor/Documents/GPU_Manager
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit values — see below
```

### Configure Google OAuth

1. Go to https://console.cloud.google.com/apis/credentials.
2. Create OAuth 2.0 Client ID → "Web application".
3. Authorized redirect URI: `http://localhost:8000/auth/google/callback`
4. Copy the Client ID and Secret into `.env`.

### Configure SMTP (optional)

Set `EMAIL_ENABLED=false` if you want to test without email. Otherwise, for
Gmail, generate an [App Password](https://myaccount.google.com/apppasswords)
and put it in `SMTP_PASSWORD`.

### Set PI email and admin password

- `PI_EMAIL=your.address@yourlab.edu` — the first time that email logs in via
  Google, they're automatically given the PI role.
- `ADMIN_PASSWORD=...` — enables a separate **admin-only** login at
  `/auth/admin-login` (no Google needed). The PI user is auto-created when you
  log in this way. Leave blank to require Google login for the PI too.

---

## 2. Initialise the database

```bash
python -m scripts.init_db --seed-gpus 4   # creates tables + 4 sample GPUs
# or without seed data:
python -m scripts.init_db
```

This creates `gpu_manager.db` (SQLite file) in the project root.

---

## 3. Run

```bash
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 and sign in. The PI can now go to **Admin** to:
- Add real GPUs (name, model, host).
- Adjust each user's quota (or add hours / reset used).
- Promote another user to PI.

---

## Account approval

Every new Google sign-in creates an account in **awaiting-approval** state.
The user sees a "your account is pending" page and can't request GPUs until
the PI approves them on the Admin page (Pending account approvals section).
On approval, the user is emailed and gains full access. The PI account
(`PI_EMAIL`) is auto-approved.

## Quotas + queue

GPU requests are **auto-assigned** by default — admin doesn't need to approve
each one. The admin can flip the **"Require admin approval for every GPU
request"** toggle on the Admin page if they want to review each request
(PI-owned requests still bypass the toggle).

| Situation                                | What happens                                     |
| ---------------------------------------- | ------------------------------------------------ |
| Toggle OFF (default), GPU is free        | Request becomes ACTIVE immediately               |
| Toggle OFF, GPU is busy, user under quota | QUEUED with priority 0                          |
| Toggle OFF, GPU is busy, user over quota | QUEUED with priority 1 (back of the line)        |
| Toggle ON, non-PI user requests          | Status = PENDING_APPROVAL (admin gets emailed)   |
| PI requests a GPU                        | Always auto-approved                             |
| Active session ends                      | Next in queue gets OFFERED + emailed             |
| Offer not confirmed in `QUEUE_CONFIRM_MINUTES` | OFFER is SKIPPED, next person is offered   |
| Reservation runs past `expected_end_at`  | Auto-expired by the scheduler (full hours charged) |

The background scheduler runs every 30 seconds to expire overdue reservations
and skip stale offers.

A user can have **one active or offered reservation at a time** plus any number
of queued ones (one per GPU max).

---

## Project layout

```
GPU_Manager/
├── app/
│   ├── main.py             # FastAPI app + scheduler lifespan
│   ├── config.py           # .env-driven settings
│   ├── database.py         # SQLAlchemy engine / session
│   ├── models.py           # User / Gpu / Reservation / Notification
│   ├── auth.py             # Google OAuth + role guards
│   ├── queue_logic.py      # core reservation + quota queue logic
│   ├── scheduler.py        # APScheduler tick (expiry, offer-skip)
│   ├── email_utils.py      # async SMTP sender
│   ├── routers/
│   │   ├── auth_routes.py  # /auth/login, /auth/google/callback, /auth/logout
│   │   ├── dashboard.py    # /, /dashboard
│   │   ├── reservations.py # /reservations/request|release|cancel|confirm
│   │   └── admin.py        # /admin (PI only)
│   └── templates/          # Jinja2 (base / login / dashboard / admin)
├── scripts/init_db.py
├── requirements.txt
├── .env.example
└── README.md
```

---

## Common things to change later

- **More than one slot per GPU**: extend `Gpu` with a `capacity` field and
  change `_active_on_gpu` / `_promote_next` in `queue_logic.py` to allow N
  concurrent ACTIVE reservations.
- **Per-GPU pricing**: add a `rate` column to `Gpu` and multiply when charging
  used hours in `release_reservation` / `expire_overdue`.
- **Slack / Teams pings**: add a webhook call alongside `send_email` in
  `scheduler._notify_offered` and in `routers/reservations.py::release`.
- **Switch SQLite → Postgres**: set `DATABASE_URL=postgresql+psycopg://…` in
  `.env` and `pip install psycopg[binary]`. SQLAlchemy handles the rest.
# GPU_Manager
