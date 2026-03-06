# Drishtikon Foundation LMS

A modern, accessible Learning Management System designed for the Drishtikon Foundation. This platform provides a comprehensive environment for students, teachers, parents, and administrative staff.

## Core Features

- **Inclusive Design**: High-contrast modes, dyslexia-friendly fonts, and keyboard-navigable UI.
- **Robust RBAC**: Role-based access control for Students, Teachers, Parents, and Admins.
- **Content Management**: Hierarchical structure: Programs > Courses > Modules > Lessons.
- **Assessment Engine**: Integrated quizzes and assignments with automated grading hooks.
- **Real-time Notifications**: Keep stakeholders informed about deadlines and announcements.

## Tech Stack

- **Backend**: FastAPI (Python 3.12+), SQLAlchemy (Async), PostgreSQL.
- **Frontend**: Vanilla HTML/CSS/JS with a focus on high-fidelity, accessible UI.
- **Database**: PostgreSQL with Alembic migrations.
- **Security**: JWT-based authentication, Bcrypt password hashing.

## Getting Started

1. Install dependencies: `pip install -r requirements.txt`
2. Configure `.env` based on `.env.example`.
3. Run migrations: `alembic upgrade head`
4. Start the server: `python run.py`

## Project Structure

- `src/`: Backend source code.
- `static/`: Frontend assets (HTML, CSS, JS).
- `tests/`: Automated test suite.
- `docs/`: Technical documentation and architecture maps.
