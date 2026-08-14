---
name: signalhire-api
description: SignalHire API reference for finding and verifying email addresses, phone numbers, and professional profiles. Use when working with SignalHire integrations, candidate enrichment, contact lookups, or callback server setup.
---

# SignalHire API Skill

Complete reference for the SignalHire REST API for contact enrichment and profile search.

## Overview

- **Base URL:** `https://www.signalhire.com/api/v1`
- **Authentication:** Header `apikey: YOUR_API_KEY` on every request
- **Content-Type:** `application/json`
- **Architecture:** Async callback model — requests return immediately (201), results POSTed to your `callbackUrl`
- **Rate Limit:** 600 elements/minute (Person API); 3 concurrent requests (Search API)
- **Credits Header:** `X-Credits-Left` in every response

## Available Endpoints

| #   | Method | Endpoint                              | Description                                           |
| --- | ------ | ------------------------------------- | ----------------------------------------------------- |
| 1   | POST   | `/candidate/search`                   | Look up person by LinkedIn URL, email, phone, or UID  |
| 2   | GET    | `/credits`                            | Get remaining credits                                 |
| 3   | GET    | `/credits?withoutContacts=true`       | Get remaining "without contacts" credits              |
| 4   | POST   | `/candidate/searchByQuery`            | Search database by filters (requires separate access) |
| 5   | POST   | `/candidate/scrollSearch/{requestId}` | Paginate through searchByQuery results                |

---

## API Endpoints Reference

### 1. Person API — Lookup by Identifier

**Endpoint:** `POST https://www.signalhire.com/api/v1/candidate/search`

Looks up one or more individuals by LinkedIn URL, email, phone, or 32-character UID. Results are delivered asynchronously via your `callbackUrl`.

**Request Parameters:**

| Parameter         | Location | Type    | Required | Description                                                                         | Example                                                 |
| ----------------- | -------- | ------- | -------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------- |
| `apikey`          | Header   | string  | Yes      | Your secret API key                                                                 | `apikey: testapikey`                                    |
| `items`           | Body     | array   | Yes      | Array of LinkedIn URLs, emails, phones, or 32-char UIDs (max 100)                   | `["https://linkedin.com/in/john", "email@example.com"]` |
| `callbackUrl`     | Body     | string  | Yes      | URL on your server that SignalHire will POST results to                             | `"https://yourdomain.com/callback"`                     |
| `withoutContacts` | Body     | boolean | No       | If `true`, returns profile data without contact details (requires separate credits) | `true`                                                  |

**Request example:**

```bash
curl -X POST --include \
  -H 'apikey: your_secret_api_key' \
  https://www.signalhire.com/api/v1/candidate/search \
  --data '{
    "items": [
      "https://www.linkedin.com/in/profile1",
      "email@example.com",
      "+44 0 123 456 789",
      "10000000000000000000000000000001"
    ],
    "callbackUrl": "https://www.yourdomain.com/yourCallbackUrl"
  }'
```

**Successful response (HTTP 201):**

```json
{ "requestId": 1 }
```

**Without contacts example:**

```bash
curl -X POST --include \
  -H 'apikey: your_secret_api_key' \
  https://www.signalhire.com/api/v1/candidate/search \
  --data '{
    "items": ["https://www.linkedin.com/in/profile1"],
    "withoutContacts": true,
    "callbackUrl": "https://www.yourdomain.com/yourCallbackUrl"
  }'
```

---

### 2. Get Remaining Credits

**Endpoint:** `GET https://www.signalhire.com/api/v1/credits`

| Parameter         | Location    | Type    | Required | Description                                   |
| ----------------- | ----------- | ------- | -------- | --------------------------------------------- |
| `apikey`          | Header      | string  | Yes      | Your secret API key                           |
| `withoutContacts` | Query param | boolean | No       | Check credits for the "without contacts" plan |

**Request examples:**

```bash
# Regular credits
curl -X GET -H 'apikey: testapikey' \
  https://www.signalhire.com/api/v1/credits

# Without-contacts credits
curl -X GET -H 'apikey: testapikey' \
  https://www.signalhire.com/api/v1/credits?withoutContacts=true
```

**Response (HTTP 200):**

```json
{ "credits": 27 }
```

---

### 3. Search API — Search by Filters

**Endpoint:** `POST https://www.signalhire.com/api/v1/candidate/searchByQuery`

> **Access:** Must be requested separately by contacting support@signalhire.com

Searches the SignalHire database using multiple filters. Returns brief profile overviews (no contacts). Use `scrollSearch` to paginate results.

**Request Parameters:**

| Parameter                          | Type               | Description                                                                                               |
| ---------------------------------- | ------------------ | --------------------------------------------------------------------------------------------------------- |
| `apikey`                           | Header string      | Your secret API key                                                                                       |
| `currentTitle`                     | string             | Boolean query for current job titles (e.g. `"(Software AND Engineer) OR Developer"`)                      |
| `currentPastTitle`                 | string             | Boolean query for current or past job titles                                                              |
| `location`                         | string or string[] | City/state/country or array of locations (e.g. `"Los Angeles, California"` or `["India", "Rome, Italy"]`) |
| `latitude` / `longitude`           | float              | Specific geo point; overrides `location`; searches 10km radius                                            |
| `coordinates`                      | array of objects   | Multiple geo points: `[{"latitude": 41.9, "longitude": 12.5}]`; overrides `location`                      |
| `currentCompany`                   | string             | Boolean query for current company name                                                                    |
| `currentPastCompany`               | string             | Boolean query for current or past company name                                                            |
| `fullName`                         | string             | Search by full name                                                                                       |
| `keywords`                         | string             | Boolean query for skills, description, education                                                          |
| `industry`                         | string             | Industry category (see allowed values in API docs)                                                        |
| `yearsOfCurrentExperienceFrom`     | number             | Min years in current role/company                                                                         |
| `yearsOfCurrentExperienceTo`       | number             | Max years in current role/company                                                                         |
| `yearsOfCurrentPastExperienceFrom` | number             | Min total experience years                                                                                |
| `yearsOfCurrentPastExperienceTo`   | number             | Max total experience years                                                                                |
| `openToWork`                       | boolean            | Filter by open-to-work status                                                                             |
| `excludeRevealed`                  | boolean            | Exclude profiles already contacted for details                                                            |
| `excludeWatched`                   | boolean            | Exclude profiles already viewed                                                                           |
| `excludeInLists`                   | boolean            | Exclude profiles already added to lists                                                                   |
| `excludeInProgress`                | boolean            | Exclude profiles already added to a job                                                                   |
| `excludeEmailed`                   | boolean            | Exclude profiles already emailed                                                                          |
| `size`                             | number             | Results per batch (default 10, max 100)                                                                   |

> **Important:** Exclude filters (`excludeRevealed`, etc.) must be combined with at least one non-exclude filter.

**Boolean operators supported** in `currentTitle`, `currentPastTitle`, `currentCompany`, `currentPastCompany`, `keywords`:

- `AND` — both terms must appear: `"PHP AND HTML"`
- `OR` — at least one term: `"Python OR Java"`
- `NOT` — exclude a term: `"Manager NOT Assistant"`
- `()` — grouping: `"(Java AND Spring) OR Python"`
- `""` — exact phrase: `"\"Software Engineer\""`

**Request example:**

```bash
curl -X POST --include \
  -H 'apikey: your_secret_api_key' \
  https://www.signalhire.com/api/v1/candidate/searchByQuery \
  --data '{
    "currentTitle": "(Software AND Engineer) OR Developer",
    "location": "New York, New York, United States",
    "keywords": "PHP AND JavaScript"
  }'
```

**Response example (HTTP 200):**

```json
{
  "requestId": 3,
  "total": 12,
  "scrollId": "abc123",
  "profiles": [
    {
      "uid": "10000000000000000000000000001006",
      "fullName": "Aaron Smith",
      "location": "London, United Kingdom",
      "experience": [{ "company": "Saward Dawson", "title": "Accountant" }],
      "skills": ["Accounting", "Analysis"],
      "contactsFetched": null,
      "openToWork": false
    }
  ]
}
```

- `scrollId` is only included when `total > size` (more pages exist)
- Concurrency limit: **3 requests at a time** (429 if exceeded)

---

### 4. Search API — Scroll Search (Pagination)

**Endpoint:** `POST https://www.signalhire.com/api/v1/candidate/scrollSearch/{requestId}`

Fetches the next batch of results from a `searchByQuery` request. The `requestId` from the initial response goes in the URL path.

**Request Parameters:**

| Parameter   | Location | Type   | Required | Description                                            |
| ----------- | -------- | ------ | -------- | ------------------------------------------------------ |
| `apikey`    | Header   | string | Yes      | Your secret API key                                    |
| `requestId` | URL path | number | Yes      | `requestId` from the original `searchByQuery` response |
| `scrollId`  | Body     | string | Yes      | `scrollId` from the previous response                  |

**Request example:**

```bash
curl -X POST --include \
  -H 'apikey: your_secret_api_key' \
  https://www.signalhire.com/api/v1/candidate/scrollSearch/3 \
  --data '{"scrollId": "abc123"}'
```

**Response example (HTTP 200):**

```json
{
  "requestId": 3,
  "total": 12,
  "scrollId": "xyz456",
  "profiles": [
    {
      "uid": "10000000000000000000000000001001",
      "fullName": "John Smith",
      "location": "Brisbane Area, Australia",
      "experience": [{ "company": "Super company", "title": "PHP Developer" }],
      "skills": ["PHP", "HTML", "Management"],
      "openToWork": true
    }
  ]
}
```

**Scroll Search Status Codes:**

| Code | Description                        |
| ---- | ---------------------------------- |
| 200  | Success, results returned          |
| 402  | Daily search quota exceeded        |
| 404  | Invalid or expired scrollId        |
| 429  | Only 3 concurrent requests allowed |

> **Critical:** `scrollId` expires after **15 seconds**. Must paginate quickly.

---

## Callback URL — How It Works

The Person API (`/candidate/search`) is **fully asynchronous**. The flow is:

1. You POST to `/candidate/search` with a `callbackUrl`
2. SignalHire returns `201` with `{ "requestId": N }` immediately
3. SignalHire processes your items asynchronously
4. SignalHire POSTs results to your `callbackUrl`
5. Your server must respond with **HTTP 200** within **10 seconds**

**Retry behavior:** If your callback server fails to return 200, SignalHire makes **3 sequential retry attempts**. After all retries fail, the callback is **permanently discarded** — you must re-trigger the lookup.

**Tracking:** The callback includes a `Request-Id` response header matching the original `requestId`, for correlation and debugging.

### Callback Body Format

Array of objects, one per item submitted:

```json
[
  {
    "item": "https://www.linkedin.com/in/url1",
    "status": "success",
    "candidate": { ... }
  },
  {
    "item": "email@email.com",
    "status": "failed"
  },
  {
    "item": "+44 0 808 189 3171",
    "status": "success",
    "candidate": { ... }
  },
  {
    "item": "10000000000000000000000000000001",
    "status": "credits_are_over"
  }
]
```

**Status values:**

| Status             | Description                                     |
| ------------------ | ----------------------------------------------- |
| `success`          | Found — `candidate` object is populated         |
| `failed`           | Not found or error — `error` field explains why |
| `credits_are_over` | Ran out of credits before processing this item  |
| `timeout_exceeded` | Processing timed out (10s)                      |
| `duplicate_query`  | Same request made within a short period         |

### Full Candidate Object Example

```json
{
  "uid": "abc123def456gh789ijk012lmn345op6",
  "fullName": "John Doe",
  "gender": null,
  "photo": { "url": "https://media.cdn.com/image/..." },
  "locations": [{ "name": "New York, New York, United States" }],
  "skills": ["Civil Litigation", "Corporate Law", "Legal Research"],
  "education": [
    {
      "faculty": "Law",
      "university": "New York University School of Law",
      "url": "https://www.linkedin.com/school/...",
      "startedYear": 2005,
      "endedYear": 2008,
      "degree": ["JD"]
    }
  ],
  "experience": [
    {
      "position": "Owner / Managing Attorney",
      "location": null,
      "current": true,
      "started": "2015-01-01T00:00:00+00:00",
      "ended": null,
      "company": "Doe Law Offices LLC",
      "summary": "...",
      "companyUrl": "https://www.linkedin.com/company/doe-law-offices",
      "companyId": null,
      "companySize": "1-10",
      "staffCount": 5,
      "industry": "Law Practice",
      "website": "http://www.doe-law.com"
    }
  ],
  "headLine": "Founder / Owner Doe Law Offices",
  "summary": "Experienced lawyer with expertise in family law...",
  "language": [
    { "name": "English", "proficiency": "Native or bilingual" }
  ],
  "contacts": [
    { "type": "phone", "value": "+1 555-123-4567", "rating": "100", "subType": "work_phone" },
    { "type": "email", "value": "john.doe@doelaw.com", "rating": "100", "subType": "work" },
    { "type": "email", "value": "john.doe@gmail.com", "rating": "100", "subType": "personal" }
  ],
  "social": [
    { "type": "li", "link": "https://www.linkedin.com/in/john-doe-12345678", "rating": "100" },
    { "type": "fb", "link": "https://www.facebook.com/johndoe", "rating": "100" }
  ],
  "organization": [...],
  "course": [...],
  "project": [...],
  "certification": [...],
  "patent": [...],
  "publication": [...],
  "honorAward": [...]
}
```

---

## HTTP Response Codes

| Code | Description                                                                    |
| ---- | ------------------------------------------------------------------------------ |
| 200  | Success — data returned                                                        |
| 201  | Accepted — processing started (Person API)                                     |
| 204  | Still in progress                                                              |
| 401  | Authentication failed — check API key                                          |
| 402  | Out of credits                                                                 |
| 403  | Account disabled or unauthorized request                                       |
| 404  | Endpoint not found / invalid JSON / non-existent request                       |
| 406  | More than 100 items in a single request                                        |
| 422  | Malformed or incorrect parameters                                              |
| 429  | Rate limit exceeded (600 elem/min for Person API; 3 concurrent for Search API) |
| 500  | Internal SignalHire server error                                               |

---

## Rate Limits

### Person API (`/candidate/search`)

- Max **100 items** per request
- Max **600 elements/minute** across all requests
- No hard limit on parallel requests, but excessive concurrency may trigger throttling
- **429** returned if limit exceeded — use exponential backoff retry

### Search API (`/candidate/searchByQuery`, `/candidate/scrollSearch`)

- Max **3 concurrent** requests at any time
- `scrollId` expires after **15 seconds**
- **429** if more than 3 concurrent requests

---

## Callback Server Requirements

For the Person API to work, your callback server must:

1. **Respond with HTTP 200** within **10 seconds** of receiving the POST
2. **Respond immediately** — do all async processing (Airtable writes, etc.) after sending the 200
3. **Accept HTTPS** with a valid SSL certificate
4. **Parse JSON bodies** — SignalHire sends `Content-Type: application/json`
5. **Be publicly reachable** — no firewall blocking inbound POST on port 443

### Minimal Express.js Callback Handler

```javascript
app.post('/signalhire/callback', express.json(), (req, res) => {
  // Respond immediately — SignalHire has a 10s timeout
  res.status(200).send('ok');

  // Process async AFTER responding
  const results = req.body; // array of { item, status, candidate }
  const requestId = req.headers['request-id'];

  setImmediate(async () => {
    for (const result of results) {
      if (result.status === 'success' && result.candidate) {
        await processCandidate(result.candidate);
      }
    }
  });
});
```

### Callback Server Debugging Checklist

- [ ] Server process is running (`pm2 status` / `systemctl status`)
- [ ] SSL certificate is valid and not expired (`curl -I https://yourdomain.com/callback`)
- [ ] Port 443 open in firewall (`sudo ufw status`)
- [ ] JSON body parser middleware is configured
- [ ] Server responds with 200 (not 201, 204, etc.)
- [ ] Response sent within 10 seconds
- [ ] Use [webhook.site](https://webhook.site) as a temporary `callbackUrl` to verify SignalHire is sending the payload

---

## Authentication Setup

1. Register at [signalhire.com](https://www.signalhire.com)
2. Navigate to **Integrations & API** from your account dashboard
3. Request API access and generate your API key
4. Pass the key as a header: `apikey: YOUR_KEY`
5. Never expose the key in public repos — it authorizes paid credit usage
