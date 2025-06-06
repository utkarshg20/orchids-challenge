# Orchids SWE Intern Challenge

This project has a **backend** (FastAPI + Celery) and a **frontend** (Next.js + TypeScript).

---

## Backend

The backend runs in a Python virtual environment and uses Redis + Celery for task queuing.

### 1. Setup

```bash
# go to backend source
cd backend

# create & activate venv
python3 -m venv .venv
source .venv/bin/activate        # on Windows: .venv\Scripts\activate

# install Python packages
pip install -r requirements.txt

# install Playwright browsers
playwright install
```

### 2. Environment
```bash
# copy example env and add your keys / URLs
cp .env.example .env
# edit .env – set OPENAI_API_KEY and REDIS_URL
```

### 3. Run
Start Redis (e.g. redis-server) in the background, then:

```bash
# 1️⃣ Celery worker
cd backend
source .venv/bin/activate
celery -A backend.tasks worker --loglevel=info
```

```bash
# 2️⃣ FastAPI server
cd backend
source .venv/bin/activate
uvicorn backend.main:app --reload --port 8000
```

## Frontend

### 1. Setup
```bash
# go to frontend source
cd frontend

# install Node packages
npm install            # or: yarn install
```

### 2. Run

```bash
cd frontend
npm run dev  
```