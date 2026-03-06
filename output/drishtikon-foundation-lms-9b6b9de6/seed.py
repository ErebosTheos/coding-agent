import asyncio
import os
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.session import AsyncSessionLocal, engine
from src.models.user import User, Role
from src.models.content import Program, Course
from src.db.base_class import Base
import bcrypt

def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

async def seed_db():
    async with engine.begin() as conn:
        # Ensure tables exist
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        # Check if admin exists
        from sqlalchemy import select
        admin_check = await session.execute(select(User).where(User.email == "admin@drishtikon.org"))
        if admin_check.scalar_one_or_none():
            print("Database already seeded.")
            return

        print("Seeding database...")

        # Create Admin
        admin = User(
            email="admin@drishtikon.org",
            hashed_password=get_password_hash("Admin@123"),
            full_name="System Administrator",
            role=Role.ADMIN,
            is_active=True
        )
        session.add(admin)

        # Create sample program and course
        program = Program(
            title="Digital Literacy",
            description="Foundational skills for the digital age"
        )
        session.add(program)
        await session.flush() # Get program ID

        course = Course(
            title="Introduction to Computers",
            program_id=program.id
        )
        session.add(course)

        await session.commit()
        print("Seeding complete successfully.")

if __name__ == "__main__":
    asyncio.run(seed_db())