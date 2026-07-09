# Development and authorship

AX Relay is an AI-assisted project. This repository is intentionally explicit about that fact.

## My role

I defined the product direction, detailed scope, architecture, operating constraints, provider boundaries, test scenarios, failure analysis, and acceptance criteria. The defining system decision—using the macOS Accessibility tree to enumerate interface elements rather than asking a model for coordinates—was part of that architecture and validation work.

I also coordinated the implementation work, tested behavior against real failure cases, and directed revisions when behavior did not meet the acceptance criteria.

## AI assistance

Claude/Opus was used for architecture and reasoning support. GLM-5.2, used through Claude Code, assisted with implementation and iteration. AI-generated code was reviewed and validated against the project’s documented constraints and tests.

## What this project demonstrates

- Systems design and reliability reasoning.
- AI-assisted delivery with explicit ownership and verification.
- Practical attention to observable state, failure modes, and safety boundaries.

## What it does not claim

It does not claim that every implementation line was written manually by one person, nor that desktop automation is universally reliable or production-ready. The repository is presented as a transparent technical case study, not as a substitute for independent coding evidence.

