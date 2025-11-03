# Secrets Management Guide

**CRITICAL:** This guide explains how to properly manage API keys and secrets for the SignalHire MCP Server. **NEVER** commit secrets to version control.

## Table of Contents

1. [Quick Start](#quick-start)
2. [Local Development](#local-development)
3. [DigitalOcean Deployment](#digitalocean-deployment)
4. [GitHub Actions CI/CD](#github-actions-cicd)
5. [Security Best Practices](#security-best-practices)
6. [Troubleshooting](#troubleshooting)

---

## Quick Start

### The Golden Rules

1. ✅ **DO:** Use `.env` files for local development (never commit them)
2. ✅ **DO:** Use platform secrets (DigitalOcean, GitHub) for deployment
3. ✅ **DO:** Use `.env.example` as template (safe to commit)
4. ❌ **NEVER:** Hardcode secrets in YAML, JSON, or code files
5. ❌ **NEVER:** Commit `.env` files to git
6. ❌ **NEVER:** Share secrets in documentation or chat

---

## Local Development

### Step 1: Create `.env` File

```bash
cd servers/business-productivity/signalhire
cp .env.example .env
```

### Step 2: Add Your API Key

Edit `.env`:

```bash
# Get this from: https://www.signalhire.com/settings/api
SIGNALHIRE_API_KEY=your_actual_api_key_here

# Optional: External callback server
EXTERNAL_CALLBACK_URL=https://your-callback-server.com/signalhire/callback

# Optional: Enhanced storage
MEM0_API_KEY=your_mem0_key
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_supabase_key
```

### Step 3: Verify `.env` is Ignored

Check `.gitignore` includes:

```bash
.env
.env.*
!.env.example
```

### Step 4: Test Configuration

```bash
python3 test_configuration.py
```

You should see:
```
✅ API Key configured: ••••••••••...
✅ Configuration valid
```

---

## DigitalOcean Deployment

**NEVER** put secrets in `app.yaml` or `digitalocean-app.yaml`!

### Step 1: Prepare Deployment File

Your `app.yaml` should look like this:

```yaml
name: signalhire-callback
region: nyc

services:
  - name: callback-server
    envs:
      - key: SIGNALHIRE_API_KEY
        scope: RUN_TIME
        type: SECRET  # <-- This tells DigitalOcean it's a secret
        # DO NOT add 'value' here - set in DigitalOcean UI

      - key: HOST
        value: "0.0.0.0"
        scope: RUN_TIME

      - key: PORT
        value: "8000"
        scope: RUN_TIME
```

### Step 2: Add Secret in DigitalOcean UI

1. Go to: https://cloud.digitalocean.com/apps
2. Select your app: `signalhire-callback`
3. Click **Settings** → **App-Level Environment Variables**
4. Click **Edit** → **Add Variable**
5. Configure:
   - **Key:** `SIGNALHIRE_API_KEY`
   - **Value:** [Paste your actual API key]
   - **Type:** **Secret** (select from dropdown)
   - **Scope:** All Components
6. Click **Save**
7. **Deploy** your app (it will automatically use the encrypted secret)

### Step 3: Verify Deployment

Check logs:
```bash
doctl apps logs [APP_ID] --type run
```

You should see:
```
✓ SIGNALHIRE_API_KEY loaded from environment
✓ Server starting...
```

---

## GitHub Actions CI/CD

### Step 1: Add GitHub Secret

1. Go to your repository: https://github.com/[username]/[repo]
2. Click **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret**
4. Add:
   - **Name:** `SIGNALHIRE_API_KEY`
   - **Value:** [Your actual API key]
5. Click **Add secret**

### Step 2: Use in Workflow

`.github/workflows/deploy.yml`:

```yaml
name: Deploy to DigitalOcean

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run tests
        env:
          SIGNALHIRE_API_KEY: ${{ secrets.SIGNALHIRE_API_KEY }}
        run: |
          python3 -m pytest tests/

      - name: Deploy to DigitalOcean
        env:
          DIGITALOCEAN_TOKEN: ${{ secrets.DIGITALOCEAN_TOKEN }}
        run: |
          doctl apps update ${{ secrets.APP_ID }}
```

**Note:** The `${{ secrets.SIGNALHIRE_API_KEY }}` syntax references the encrypted secret stored in GitHub.

---

## Security Best Practices

### 1. Secret Rotation

**Rotate your API keys regularly:**

1. Generate new key in SignalHire dashboard
2. Update in all locations:
   - Local `.env` file
   - DigitalOcean App Platform (Settings → Environment Variables)
   - GitHub Secrets (Settings → Secrets)
3. Verify new key works
4. **Revoke old key** in SignalHire dashboard

### 2. Access Control

**Limit who can access secrets:**

- DigitalOcean: Use team permissions
- GitHub: Use environment protection rules
- Local: Use file permissions: `chmod 600 .env`

### 3. Monitoring

**Monitor for unauthorized usage:**

- Check SignalHire dashboard for unusual activity
- Monitor API usage and credit consumption
- Set up alerts for unexpected usage patterns

### 4. Git Hooks

**Install pre-commit hooks to catch secrets:**

```bash
# From repository root
/foundation:hooks-setup
```

This installs hooks that:
- Scan for API keys before commit
- Block commits with exposed secrets
- Validate commit message format

Test it:
```bash
echo "SIGNALHIRE_API_KEY=test123" > test.env
git add test.env
git commit -m "test: check secret detection"
# Should be BLOCKED by pre-commit hook ✅
```

---

## Troubleshooting

### Error: "SIGNALHIRE_API_KEY not found"

**Cause:** Environment variable not set

**Solutions:**

**Local:**
```bash
# Check .env file exists
ls -la .env

# Verify it contains the key
cat .env | grep SIGNALHIRE_API_KEY

# Load it manually
export SIGNALHIRE_API_KEY=your_key_here
python3 server.py
```

**DigitalOcean:**
1. Check App Platform → Settings → Environment Variables
2. Verify `SIGNALHIRE_API_KEY` is listed as **Secret** type
3. Redeploy app to apply changes

**GitHub Actions:**
1. Check Settings → Secrets → Actions
2. Verify `SIGNALHIRE_API_KEY` exists
3. Check workflow file uses `${{ secrets.SIGNALHIRE_API_KEY }}`

### Error: "Permission denied" when accessing .env

**Cause:** File permissions too restrictive

**Solution:**
```bash
chmod 600 .env  # Owner read/write only
```

### Secret Accidentally Committed

**URGENT - Follow these steps:**

1. **Immediately revoke the exposed key:**
   - Log in to SignalHire dashboard
   - Go to Settings → API Keys
   - Click **Revoke** on exposed key

2. **Generate new key:**
   - Create new API key
   - Update in all secure locations (.env, DigitalOcean, GitHub)

3. **Remove from git history:**
   ```bash
   # WARNING: This rewrites history - coordinate with team
   git filter-branch --force --index-filter \
     "git rm --cached --ignore-unmatch path/to/file/with/secret" \
     --prune-empty --tag-name-filter cat -- --all

   git push origin --force --all
   ```

4. **Document incident:**
   - Create security incident report
   - Review how it happened
   - Implement prevention measures

---

## Environment Variables Reference

### Required

| Variable | Description | Where to Get | Example |
|----------|-------------|--------------|---------|
| `SIGNALHIRE_API_KEY` | SignalHire API authentication | [SignalHire Dashboard](https://www.signalhire.com/settings/api) | `202.xxx...` |

### Optional

| Variable | Description | Where to Get | Example |
|----------|-------------|--------------|---------|
| `EXTERNAL_CALLBACK_URL` | External webhook server | Your DigitalOcean app URL | `https://callback.example.com/signalhire/callback` |
| `MEM0_API_KEY` | Mem0 memory platform | [Mem0 Dashboard](https://app.mem0.ai) | `m0-xxx...` |
| `SUPABASE_URL` | Supabase project URL | Supabase project settings | `https://xxx.supabase.co` |
| `SUPABASE_KEY` | Supabase anon/service key | Supabase project settings | `eyJxxx...` |

---

## Quick Reference

### Safe to Commit ✅
- `.env.example` (template with placeholders)
- `app.yaml` (with `type: SECRET`, no values)
- Documentation (with redacted examples)
- Code (loading from environment)

### NEVER Commit ❌
- `.env` (actual secrets)
- `app.yaml` with hardcoded secrets
- API keys in any file
- Database credentials
- Private tokens

---

## Related Documentation

- [SECURITY_INCIDENT.md](./SECURITY_INCIDENT.md) - Security incident report and remediation
- [DEPLOY.md](./docs/deployment/DEPLOY.md) - Deployment guide
- [EXTERNAL_CALLBACK_SETUP.md](./docs/setup/EXTERNAL_CALLBACK_SETUP.md) - Callback server setup

---

**Remember:** When in doubt, use secrets management. It's always better to be safe than to expose credentials.
