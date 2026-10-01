# DKU CoursePilot

> 🏆 **A DKU hackathon project, built by a team of three.**

DKU CoursePilot is a bulletin-aware course planning and schedule-building application for Duke Kunshan University students. It combines the supplied course catalogs, degree requirements, personal academic history, course preferences, instructor reviews, and requisite checks in one English-language interface.

The application is built with Python, Flask, SQLite, HTML, CSS, and JavaScript.

## Product areas

- **Home** — a focused starting point with personalized next-course suggestions
- **Courses** — search the active academic term and compare personal fit
- **Profile** — save a major track, completed courses, year information, and learning priorities
- **Reviews** — rate each course-instructor combination across seven dimensions
- **Schedule** — generate feasible schedules ranked by degree progress, availability, schedule style, and community fit

## Academic data

Seven supplied database files represent four distinct academic terms: 2025 Fall, 2026 Spring, 2026 Summer, and 2026 Fall. Duplicate snapshots are automatically reduced to the newest snapshot for each term. All four terms contribute to course history and recommendation signals, while scheduling uses the selected active term.

Major requirements are extracted from the **DKU Undergraduate Bulletin 2025-2026** into `data/bulletin_2025_2026.json`. The extraction can be reproduced with:

```bash
python scripts/extract_bulletin_pdf.py /path/to/ug_bulletin_2025-2026.pdf data/bulletin_2025_2026.json
```

## Persistence

The application creates `data/planner.db` at runtime. Personal profiles are partitioned by a random, HttpOnly browser identifier, so different browsers and devices using the same deployment cannot read or overwrite one another's academic information. Submitted course reviews remain shared community data. The database file is ignored by Git. Course sections added from Course Explorer are kept in browser storage until they are used by Schedule Builder.

## Start locally

```bash
pip install flask requests
python app.py
```

Open [http://localhost:5000](http://localhost:5000).

On macOS, you can also double-click `start_coursepilot.command`. On its first run, it creates an isolated environment and installs the required packages; later launches reuse that environment.

## Deploy

The repository includes a Render Blueprint at the project root. Connect the GitHub repository in Render and select **New Blueprint Instance** to create a hosted web service. The bundled catalog databases are included in the deployment; profile and review data use the service's local runtime storage unless a persistent disk is configured.

## Project structure

```text
app.py                 Flask routes and page orchestration
planner_data.py        Profiles, reviews, course history, and recommendations
scheduler.py           Schedule generation and ranking
bulletin.py            Bulletin data access
data/                  Extracted bulletin data and runtime planner database
templates/             English product pages
static/css/app.css     Shared visual system
databases/             Academic course catalogs
```

## Team

Built by a team of three Duke Kunshan University students as a hackathon project.
