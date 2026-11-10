# EduGrade UAT credentials

These are fictional development-only accounts. Run `docker compose exec -T api python scripts/seed.py` from the backend directory, or `.\.venv\Scripts\python.exe scripts/seed.py` when using the local Python environment. The seed creates each missing school independently and does not change an existing school or reset its passwords. Earlier demo schools remain untouched.

| School | School key | Admin / teacher tab key |
| --- | --- | --- |
| Riverdale Public School | `riverdale-uat` | `riverdale-uat-admin-key` |
| Greenfield International School | `greenfield-uat` | `greenfield-uat-admin-key` |

Choose a school key on the login page. Staff use the Admin tab and the school's staff key; students use the Student tab. Replace `<school-key>` below with the chosen key.

| Role | Email | Password |
| --- | --- | --- |
| Admin | `admin@<school-key>.edu` | `Admin@123` |
| Teacher | `teacher8a@<school-key>.edu` | `Teacher@123` |
| Teacher | `teacher8b@<school-key>.edu` | `Teacher@123` |
| Teacher | `teacher9a@<school-key>.edu` | `Teacher@123` |
| Teacher | `teacher9b@<school-key>.edu` | `Teacher@123` |
| Student 8A | `student8a.1@<school-key>.edu` through `student8a.4@<school-key>.edu` | `Student@8a1` through `Student@8a4` |
| Student 8B | `student8b.1@<school-key>.edu` through `student8b.4@<school-key>.edu` | `Student@8b1` through `Student@8b4` |
| Student 9A | `student9a.1@<school-key>.edu` through `student9a.4@<school-key>.edu` | `Student@9a1` through `Student@9a4` |
| Student 9B | `student9b.1@<school-key>.edu` through `student9b.4@<school-key>.edu` | `Student@9b1` through `Student@9b4` |

Each student password includes the class and the final number in their email. Each class has a science assessment with questions and a mathematics file-upload assignment. User names and assignment titles show the school and, where applicable, class. Log in as students from different classes or schools to compare their lists. Dates are relative to the initial seed run; rerunning does not refresh existing schools.

These credentials are seed defaults, not a statement about the current database. If an account was edited, its existing credentials still apply.
