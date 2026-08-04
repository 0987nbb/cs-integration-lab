# Odoo Integration Lab (`cs_integration_lab`)

This repository contains the solution for the **Odoo Integration Lab** technical assessment.

## Project Structure

* `cs_integration_lab/`: Odoo 19 addon module containing data models, views, security rules, and business logic inside Odoo.
* `integration_service/`: External Python integration service responsible for consuming external APIs and communicating with Odoo 19 via its JSON-2 API.
* `tests/`: Automated unit and integration test suite using mock HTTP responses.
* `postman/`: Postman collections, environment configurations, and CLI proof scripts demonstrating API interactions.
* `docs/`: Project documentation, architectural diagrams, API mapping details, and setup instructions.

## External APIs Integrated

1. **GitHub REST API**
2. **JSONPlaceholder**
3. **Frankfurter v2**
4. **Open-Meteo**
5. **Nager.Date**

## Quick Start

1. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Follow setup guidelines in `docs/` for configuring Odoo 19 and external service credentials via environment variables.
