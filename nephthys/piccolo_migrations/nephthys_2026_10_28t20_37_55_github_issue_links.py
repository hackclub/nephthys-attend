from nephthys.database.raw_migration import raw_migration

ID = "2026-10-28T20:37:55:865263"
VERSION = "1.34.0"
DESCRIPTION = "Add GitHub issue links to tickets"


async def forwards():
    return raw_migration(
        migration_id=ID,
        app_name="nephthys",
        description=DESCRIPTION,
        forwards="""
ALTER TABLE "Ticket" ADD COLUMN "githubIssueUrl" TEXT;
ALTER TABLE "Ticket" ADD COLUMN "githubIssueLinkedById" INTEGER
    REFERENCES "User" (id) ON DELETE SET NULL ON UPDATE CASCADE;
""",
        backwards="""
ALTER TABLE "Ticket" DROP COLUMN "githubIssueLinkedById";
ALTER TABLE "Ticket" DROP COLUMN "githubIssueUrl";
""",
    )
