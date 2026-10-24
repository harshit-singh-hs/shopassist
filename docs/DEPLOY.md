# Deploying ShopAssist for free

ShopAssist is one container: FastAPI serves both the API and the web UI. **Measured memory use is about 350 MB**, including the local embedding model, so it fits a 512 MB free instance.

| Option | Cost | Notes |
|---|---|---|
| **Render free web service (recommended)** | Free | 512 MB, slow shared CPU. Sleeps after 15 min without traffic; the next visit takes about 1–2 min to wake. Deploys from GitHub using `render.yaml` |
| Hugging Face Spaces | **$9/month (PRO)** | Since Oct 2026, Docker Spaces on the free CPU need PRO (creating one returns "402 Payment Required"). `scripts/deploy_hf.py` deploys in one command if you subscribe |
| Google Cloud Run | Free tier | Faster than Render, but a billing account (card) is required |
| Vercel | Free | Only for the UI; the agent itself can't run serverless (see the end of this file) |

Free tiers change, so check the current limits when you sign up.

---

## Render in 4 steps

**1. Put the code on GitHub.** Create an **empty** repository on github.com (for example `shopassist`) with no README, .gitignore or license. Then, in the project folder:
```bash
git remote add origin https://github.com/<your-github-username>/shopassist.git
git push -u origin main
```
Git opens a browser window to sign in to GitHub the first time. What gets pushed and what doesn't:
- **Never pushed** (`.gitignore`): `.env`, the database, and the crawled Amazon pages.
- **Built on Render:** the server crawls the pages itself while building.

**2. Create the service from the blueprint.** On render.com (sign in with GitHub), go to **New → Blueprint**, pick the repository and click **Apply**. Render reads `render.yaml`, which sets up a Docker web service on the free plan in Singapore, with the health check on `/health`.

**3. Enter the two secrets when Render asks:**
- `GEMINI_API_KEY`: your key from https://aistudio.google.com/apikey
- `DEMO_ACCESS_CODE`: any code, for example `interview-2026`. Visitors must enter it.

The access code matters: a public demo on a free Gemini key would otherwise be drained by random visitors, since the free tier allows about 100 LLM requests a day across the fallback chain.

**4. Wait for the first build** (about 10 minutes). It installs dependencies, crawls the 43 help pages, downloads the embedding model and builds the index. Then open `https://shopassist.onrender.com` (or whatever URL Render shows) and share the link and the access code with your interviewer.

Every `git push` to `main` redeploys automatically.

### What visitors get
- **An access-code gate:** without the code, only the gate page loads.
- **Abuse limits:**
  - 10 messages per minute per IP
  - 20 turns per conversation
  - 150 turns per day for the whole demo

  These are configurable with `DEMO_IP_RPM`, `DEMO_TURNS_PER_CONVERSATION` and `DEMO_DAILY_TURN_CAP`.
- **Fresh demo data:** it's generated at start-up and refreshed daily, because dates are relative to "today". The support desk also has a *Reset demo data* button. On the free plan, data also resets whenever the service goes to sleep, which is fine for a demo.
- **Graceful behaviour when the Gemini quota runs out:** the bot says it's having trouble and offers a human, instead of hanging. *Support desk → Model availability* shows which models are benched.

### Before an interview
- **Open the link 2–3 minutes early.** A sleeping free instance needs that long to wake and load the models.
- **Don't run the eval suite that day.** It uses the same daily Gemini quota.

### Troubleshooting
- **The build fails with "Knowledge base incomplete".** The crawler refuses to ship a bot without its core policy pages, and Amazon sometimes rate-limits requests, especially from datacenter IPs. You have two options:
  - Click **Manual Deploy → Clear build cache & deploy** a little later.
  - Commit the pages you crawled locally to a **private** repository: `git add -f knowledge_base/amazon_in` and push. The Dockerfile then skips the crawl. Keep the repository private, because the pages are Amazon's text.
- **The bot replies "having trouble".** The daily Gemini quota is used up; it resets at midnight Pacific time (about 12:30 PM IST).
- **The service restarts with "out of memory".** That's unlikely at ~350 MB. Make sure `RETRIEVAL_MODE` isn't set to `hybrid_rerank`, which loads a second model.

---

## Hugging Face Spaces (PRO, $9/month)
If you have or buy PRO, one command does everything: it creates the Space, stores the secrets encrypted, and uploads the code without `.env`, the database or the Amazon pages.
```bash
hf auth login --force      # token with write access, or log in through the browser
python -m scripts.deploy_hf --space <hf-user>/shopassist --access-code interview-2026 --set-secret
```
If the build is blocked from crawling, add `--private --include-kb` to upload your local pages; the script refuses to do that for a public Space.

---

## Google Cloud Run
This needs the `gcloud` CLI and a project with billing enabled. The free tier covers a low-traffic demo. `--max-instances 1` keeps conversations on a single instance.
```bash
gcloud secrets create gemini-key --data-file=-          # paste the key, then press Ctrl+Z and Enter (Windows)
gcloud run deploy shopassist --source . --region asia-south1 --memory 1Gi --cpu 1 --max-instances 1 \
  --allow-unauthenticated --set-env-vars DEMO_ACCESS_CODE=interview-2026 --set-secrets GEMINI_API_KEY=gemini-key:latest
```

---

## Vercel: UI only
**Why not the whole app on Vercel?** Vercel runs Python as *serverless functions*. Size isn't the problem: about 176 MB of libraries plus a 189 MB model, under Vercel's 500 MB Python limit. The problem is that each request can land on a different short-lived instance, and this app keeps two things in one place:
- **Conversation memory, including the two-step confirmation.** "Return this?" and the next "yes" could reach different instances, and the second one wouldn't know about the pending return.
- **The SQLite order database.** Each instance would have its own copy.

Going fully serverless would mean moving sessions and data into Postgres or Redis.

What works is **the UI on Vercel and the API on Render**:
1. In Render's dashboard, add the environment variable `CORS_ORIGINS = https://<your-project>.vercel.app` (it needs `https://` and no trailing slash).
2. On Vercel, import the repository, choose Framework **Other**, and set `SHOPASSIST_API = https://shopassist.onrender.com`. `vercel.json` uploads only `web/`, and the build fails if `SHOPASSIST_API` is missing.

## Running the container locally
```bash
docker compose up --build        # http://localhost:8000
```

## A note on the content
The knowledge base is Amazon.in's public help text. It's fetched by the deployment itself and never committed to a public repository. The UI labels the app as an unofficial demo with synthetic orders, and it uses no Amazon branding. Keep it that way, and keep the access-code gate on for anything public.
