# Restaurant Phone Ordering

An application that lets customers call a restaurant, place a pickup order,
confirm the details, and pay when collecting it.

## First release

- One restaurant and one location.
- English-language telephone ordering.
- ASAP pickup with a restaurant-configured preparation estimate.
- Menu items with supported modifiers.
- Full order readback and explicit confirmation.
- Payment at pickup.
- Staff kitchen dashboard and order status updates.
- Transfer to restaurant staff when needed.

## Planned technology

- Backend: Python, FastAPI, and Pydantic.
- Database: PostgreSQL with asyncpg and parameterized native SQL.
- Migrations: dbmate SQL files.
- Voice: Twilio Voice/Media Streams and OpenAI Realtime.
- Staff interface: Next.js and TypeScript.
- Deployment: AWS, Terraform, and GitHub Actions.

## Application rules

- The backend validates items, modifiers, prices, totals, and availability.
- The AI requests allowed functions; application code executes them.
- An order requires confirmation of the current quote.
- A cart can create only one order.
- Historical orders retain their original item and price snapshots.
- Call status, order status, and payment status are separate.

## Project layout

- app/api/: HTTP routes and provider webhooks.
- app/services/: business rules and orchestration.
- app/repositories/: database reads and writes using native SQL.
- app/schemas/: Pydantic request, response, and function schemas.
- app/db/: connection pool and database helpers.
- app/voice/: live audio and voice-provider integration.
- tests/: automated checks.
- migrations/: versioned SQL schema changes.
- web/: kitchen and administration frontend.
- infra/: infrastructure definitions.

## Current checkpoint

Phase 1, Lesson 1: repository, project structure, and MVP scope.
Python dependencies and the FastAPI application are added in Lesson 2.
