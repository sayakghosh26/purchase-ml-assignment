# Purchase Recording & Return-Risk API

A small Django REST Framework service built for the **Mid-level Backend, Data & ML** assignment. It records purchases safely, imports historical orders, provides reporting endpoints, and trains a logistic-regression model to estimate return risk.

## Tech stack

- **Python:** 3.12+
- **Backend:** Django, Django REST Framework
- **Database:** SQLite
- **ML:** scikit-learn, pandas, joblib
- **Tests:** Django test runner

Dependencies and their pinned versions are listed in `requirements.txt`.

## Setup and run

From the repository root, create and activate a virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

On macOS/Linux, activate it with `source .venv/bin/activate` instead.

Run the following commands **in order**:

```powershell
# 1. Create the database and seed the product catalog
python manage.py migrate

# 2. Import historical orders
python manage.py import_history order_history.csv

# 3. Train the return-risk model using imported history
python manage.py train_return_model

# 4. Run automated tests and Django's system checks
python manage.py test
python manage.py check

# 5. Start the API
python manage.py runserver
```

The API is available at `http://127.0.0.1:8000/`.

Migration `0002_seed_catalog` creates the initial catalog:

| SKU | Product | Price (cents) | Initial stock |
| --- | --- | ---: | ---: |
| `lamp` | Maple lamp | 8,000 | 2 |
| `stool` | Cedar stool | 12,000 | 1 |

The stool also supports an optional `cushion` extra costing 2,500 cents. No separate fixture load is required.

The history importer can be rerun without duplicating previously imported orders. It emits a one-line JSON summary of records created, skipped, and rejected by reason. For the provided 122-row CSV, a first import into an empty history table creates **112** rows and rejects **10**; importing the same file again skips the 112 valid existing rows.

Training writes `models/return_model.joblib` and `models/metrics.json`. These are generated artifacts, so the setup instructions above regenerate them locally. Without a trained model, the prediction endpoint returns HTTP **503** with `model_not_trained`. The API caches the loaded model; restart the server after retraining to pick up the new version.

## API endpoints

| Method | Endpoint | Description |
| --- | --- | --- |
| `POST` | `/api/purchases/` | Record a purchase or replay an existing charge |
| `GET` | `/api/catalog/<sku>` | View product details and available stock |
| `GET` | `/api/outbox` | View outbox messages in creation order |
| `GET` | `/api/history/` | List historical orders (10 per page) |
| `GET` | `/api/history/stats` | Aggregate orders, revenue, and return rates by SKU |
| `POST` | `/api/predictions/return-risk` | Estimate the probability of a return |

### Record a purchase

`POST /api/purchases/` with a JSON body:

```json
{
  "email": "Ada@Example.com",
  "sku": "stool",
  "externalChargeId": "ch_12345678",
  "extraCode": "cushion"
}
```

A new purchase returns **201** with `replayed: false` and the order details. Reusing the same charge ID returns **200** with `replayed: true`, without reducing stock or writing new outbox messages. The service normalizes customer emails, checks allowed extras and duplicates, decrements stock, and writes outbox records in a single transaction. A sold-out or duplicate purchase returns **409**; invalid request data returns **400**.

The outbox represents email delivery for this exercise: no external email service is called.

### Browse historical orders and statistics

`GET /api/history/?page=1&sku=lamp` returns `count`, `page`, and `results`. Supported filters are `sku`, `channel`, `from`, `to`, and `page`; date filters use inclusive UTC calendar dates.

`GET /api/history/stats?from=2026-05-01&to=2026-05-31` returns per-SKU order counts, revenue in cents, and return rate. For the supplied CSV, the expected May 2026 totals are **43 lamp orders (344,000 cents)** and **20 stool orders (265,000 cents)**.

### Predict return risk

`POST /api/predictions/return-risk` with JSON:

```json
{
  "email": "new@example.com",
  "sku": "lamp",
  "extraCode": null,
  "channel": "app",
  "createdAt": "2026-07-01T22:15:00Z"
}
```

The response contains `probability` (0–1, rounded to three decimal places) and `modelVersion`.

The model is `LogisticRegression(max_iter=1000, random_state=0)`, trained using an 80/20 stratified split. Its **six** inputs are `is_stool`, `has_extra`, `channel_app`, `channel_web`, UTC `hour`, and the customer's number of strictly earlier historical orders (`prior_orders`). The training command saves holdout accuracy, majority-class baseline accuracy, feature names, row counts, and a new version identifier in `metrics.json`.

## Testing and verification

Run:

```powershell
python manage.py test
python manage.py check
python manage.py makemigrations --check --dry-run
```

The automated suite covers purchase success and rollback behavior, charge replay, stock and duplicate protections, CSV validation and repeat imports, May reporting totals, and predictions before and after training. The endpoints were also exercised manually against the local development server.

**Note:** The purchase endpoints change development-database stock. The test runner uses an isolated test database, so automated tests do not consume the local catalog's inventory.

## Time spent

Approximately **7.5 hours** in total:

- Part A — models, purchase service, APIs, and initial tests: **3 hours**
- Part B — historical import, reporting, and model pipeline: **2.5 hours**
- Review, fixes, and automated/manual verification: **2 hours**

## AI tools

I used **ChatGPT and Gemini** for implementation suggestions, code review, and help debugging a few edge cases and tests. I checked the changes against the brief and tested the endpoints locally. For example, I changed the suggested JSON test-request setup to use DRF's `APIClient`, fixing a `null` value being encoded as form data.

## What I'd improve

For a larger production workload, I would use **PostgreSQL** for stronger concurrent inventory handling and add concurrency-focused integration tests. For CSV files with millions of rows, I would move duplicate-ID tracking to an indexed staging table, retain batch inserts, and add checkpoints and resumable imports. I would also introduce a transactional outbox worker, stronger request validation, and explicit model-artifact versioning/deployment controls.

## Scope

As requested, this project does not integrate a payment provider, send real emails, implement user accounts beyond the customer record, or include an admin workflow. SQLite and a locally stored model are used to keep the assignment easy to run and review.
