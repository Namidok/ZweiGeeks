# vuln_py

A deliberately vulnerable mini project that ZweiGeeks' tests and benchmark
runs audit and fix. Known issues:

| File | Issue | Found by |
|---|---|---|
| app.py | `subprocess.call(..., shell=True)` with a concatenated argument | bandit B602 |
| app.py | hard-coded password `DB_PASSWORD` | bandit B105 |
| app.py | `yaml.load` without a Loader | bandit B506 |
| app.py | `requests.get(..., verify=False)` | bandit B501 |
| app.py | SQL built with an f-string | bandit B608 |
| requirements.txt | `PyYAML==5.3`, `requests==2.19.1` (known CVEs) | pip-audit |
| helpers.py | unused imports, f-string without placeholders (auto-fixable), `== None` | ruff |

It's stored as plain files; tests copy it into a temp dir and `git init`
it there, so there's no nested repository inside ZweiGeeks.
