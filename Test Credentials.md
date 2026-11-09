# EduGrade Test Credentials

Last checked: **7 November 2026**. These are local development and test credentials.

## Current local application

| Item | Value |
| --- | --- |
| Frontend, English | http://localhost:3000/en |
| Frontend, Hindi | http://localhost:3000/hi |
| Python API | http://localhost:8000/api/v1 |
| School | Demo Academy |
| School subdomain | `demo-school` |
| Admin / teacher tab key | `demo-admin-key` |

The Python seed creates **Demo Academy (`demo-school`)** and **Demo Academy 2 (`demo-school-2`)**. Each school has four active accounts with separate identities and data. The seed does not reset existing accounts or passwords. Both schools are present in the current local database. All eight logins and cross-school access restrictions were verified through the frontend gateway on the date above.

### Demo Academy accounts — available now

| Role | Name | Email / login | Password | Student class |
| --- | --- | --- | --- | --- |
| Admin | Ada Admin | `admin@demo-school.edu` | `Admin@123` | — |
| Teacher | Tara Teacher | `teacher@demo-school.edu` | `Teacher@123` | — |
| Student | Sam Student | `student1@demo-school.edu` | `20090101` | Grade 9 / A |
| Student | Sara Student | `student2@demo-school.edu` | `20090102` | Grade 9 / A |

### How to log in

1. Open the English or Hindi frontend URL above.
2. For a student, use the **Student** tab and the student's email and password.
3. For a teacher or admin, select the **Admin** tab, enter `demo-admin-key` when prompted, then enter the staff member's email and password.
4. Log out before switching accounts, especially when switching roles or schools.

The school admin key unlocks the staff login tab and is sent with the staff login request; the API rejects any teacher/admin login without a matching key, so the account password alone is never enough. Teachers also use this tab. Staff accounts are rejected on the student portal, and student accounts are rejected on the staff portal.

The development frontend selects its school using `NEXT_PUBLIC_DEV_HOSTNAME` in `.env.local`, currently `demo-school`. Restart the frontend after changing this value. Changing the URL alone does not select a different school while this development override is in use.

### Seed command

Run from `edugrade-backend-python` after configuring and starting its database:

```powershell
docker compose exec -T api python scripts/seed.py
```

For a backend running directly in the local virtual environment:

```powershell
.\.venv\Scripts\python.exe scripts/seed.py
```

The Python seeder creates both schools independently. If one already exists, it skips that school and continues with the other. Existing passwords and data are preserved, so changed passwords take precedence over these defaults. A new school receives two grades, two divisions, a Grade 9 / A classroom led by its teacher, and an online quiz with two questions.

## Demo Academy 2 accounts — available now

School subdomain: `demo-school-2` · Admin / teacher tab key: `demo-admin-key-2`

| Role | Name | Email / login | Password | Student class |
| --- | --- | --- | --- | --- |
| Admin | Ada Admin | `admin@demo-school-2.edu` | `Admin@123` | — |
| Teacher | Tara Teacher | `teacher@demo-school-2.edu` | `Teacher@123` | — |
| Student | Sam Student | `student1@demo-school-2.edu` | `20090101` | Grade 9 / A |
| Student | Sara Student | `student2@demo-school-2.edu` | `20090102` | Grade 9 / A |

Names and class labels deliberately match the first school's examples, while user, grade, division, classroom, assignment, question, and option IDs are separate. Demo Academy 2 uses green branding and its quiz is titled **Photosynthesis Basics (School 2)**. The first school's quiz is **Photosynthesis Basics**.

## Demonstrate data isolation

1. Set `NEXT_PUBLIC_DEV_HOSTNAME=demo-school` in the frontend's `.env.local` and start the frontend. Sign in as `teacher@demo-school.edu` or `student2@demo-school.edu`. Confirm the school is Demo Academy and its quiz is visible. Record that quiz's assignment ID from its URL.
2. Log out. Change `.env.local` to `NEXT_PUBLIC_DEV_HOSTNAME=demo-school-2` and restart the frontend. Open http://localhost:3000/en or http://localhost:3000/hi. Confirm the school is Demo Academy 2 with green branding.
3. Try the first school's student login on this second school's login page: it must be rejected because the account belongs to a different school. Its admin key `demo-admin-key` must also fail on the second school's Admin tab.
4. Sign in with `student2@demo-school-2.edu` / `20090102`. Only this school's targeted assignments should appear. Opening the first school's assignment ID must return a not-found response and reveal no assignment data.
5. Log out and use the second school's Admin tab with `demo-admin-key-2`, then `admin@demo-school-2.edu` / `Admin@123`. Its user lists should contain only its own two students and one teacher, unless more users were added there.
6. Repeat in the opposite direction: a second-school account/key cannot log in on the first school's portal, and a first-school session cannot access the second school's assignment.
7. Restore `NEXT_PUBLIC_DEV_HOSTNAME=demo-school` and restart when finished.

Existing local smoke-test records may make the first school's lists longer. Those records remain scoped to that school. Creating, editing, or submitting work in either tenant should affect only that tenant.

The backend regression tests exercise these exact seed templates: repeat seeding, adding the second school when the first exists, all eight logins, school-specific keys, user/assignment lists, foreign assignment reads, foreign assignment edits, and foreign classroom targets.

## Automated backend test fixtures

These accounts exist only in isolated, temporary pytest databases. They are not created by `scripts/seed.py` and cannot be used to log in to the running frontend unless explicitly created there.

### Test Academy

Subdomain: `test-academy` · Admin / teacher tab key: `test-admin-key`

| Role | Name | Email / login | Password |
| --- | --- | --- | --- |
| Admin | Ada Admin | `admin@test.edu` | `Admin@123` |
| Teacher | Tara Teacher | `teacher@test.edu` | `Teacher@123` |
| Student | Sam Student | `student@test.edu` | `Student@123` |

The fixture student belongs to Grade 9 / A. Individual tests also create these accounts within Test Academy:

| Role | Email / login | Initial password | Availability |
| --- | --- | --- | --- |
| Student | `fresh@test.edu` | `NewStudent20100101` | Created by the CSV skip-invalid-rows integration test |
| Student | `new@test.edu` | `NewStudent20100101` | Created by the CSV status test, then deactivated later in that test |
| Teacher | `newteacher@test.edu` | `NewTeacher19900101` | Created by the teacher-creation integration test |

The pagination test also creates `bulk0@test.edu` through `bulk24@test.edu` without password hashes. These are list fixtures, with no usable password login.

### Other School — tenant-isolation fixture

Subdomain: `other-school` · Admin key: `other`. The Google login test creates this school without any users. `unknown@test.edu` is deliberately unregistered and is expected to fail authentication.

## Password and SSO notes

- Passwords are case-sensitive. Current demo students use the explicit seed dates `YYYYMMDD`; pytest's `student@test.edu` uses `Student@123` instead.
- CSV-generated passwords use the user's name with whitespace removed followed by the date of birth in `YYYYMMDD` format. For example, `New Student` with DOB `2010-01-01` produces `NewStudent20100101`.
- Accounts created later may have explicit or generated passwords. Password hashes cannot recover those plaintext values; use the initial generated-password response or reset the password through the application.
- The current Python demo seed uses credentials-only authentication. There are no shared Google or Microsoft test passwords in the seed data.
- Google login requires configured OAuth credentials and a registered, active student in the selected school with an auth method that permits Google. Unknown users, wrong-school accounts, and staff are rejected by the student portal.

## Sources and copies

- Development seed: `edugrade-backend-python/scripts/seed.py`.
- Demo isolation regression tests: `edugrade-backend-python/tests/integration/test_demo_seeds.py`.
- Temporary fixtures: `edugrade-backend-python/tests/conftest.py` and integration tests `test_frontend_contracts.py`, `test_question_integrity.py`, `test_google_login.py`, and `test_api.py`.
- Identical copies: `edugrade-frontend/Test Credentials.md` and `edugrade-backend-python/Test Credentials.md`.
