from conftest import git, write

from src.tools import repo_hygiene


# --- tracked secrets --------------------------------------------------------

def test_tracked_env_file_is_flagged_without_gitignore(git_repo):
    write(git_repo, ".env", "API_KEY=dummy\n")
    write(git_repo, "app.py", "print('hi')\n")
    git(git_repo, "add", ".env", "app.py")

    hits = repo_hygiene.find_tracked_secrets(str(git_repo))

    assert [h["file"] for h in hits] == [".env"]
    assert hits[0]["also_gitignored"] is False
    assert "still live in history" not in hits[0]["detail"]


def test_tracked_secret_that_also_matches_gitignore_is_called_out(git_repo):
    # The pattern that motivated this check: .gitignore lists the file, but
    # it was committed anyway (before the rule existed, or with `git add -f`).
    write(git_repo, ".gitignore", "# local config\n/.env\n*.pem\n")
    write(git_repo, ".env", "API_KEY=dummy\n")
    write(git_repo, "certs/server.pem", "dummy\n")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "add", "-f", ".env", "certs/server.pem")

    hits = {h["file"]: h for h in repo_hygiene.find_tracked_secrets(str(git_repo))}

    assert set(hits) == {".env", "certs/server.pem"}
    assert all(h["also_gitignored"] for h in hits.values())
    assert "still live in history" in hits[".env"]["detail"]


def test_untracked_secret_is_not_reported(git_repo):
    write(git_repo, ".gitignore", ".env\n")
    write(git_repo, ".env", "API_KEY=dummy\n")
    git(git_repo, "add", ".gitignore")

    assert repo_hygiene.find_tracked_secrets(str(git_repo)) == []


def test_not_a_git_repo_yields_no_hits(tmp_path):
    write(tmp_path, ".env", "API_KEY=dummy\n")
    assert repo_hygiene.find_tracked_secrets(str(tmp_path)) == []


# --- stray files -------------------------------------------------------------

def test_stray_backup_files_are_flagged(git_repo):
    for rel in ("index.php.bak", "config.orig", "notes.txt~", "sub/.DS_Store", "index.php"):
        write(git_repo, rel, "x\n")
    git(git_repo, "add", "-A")

    flagged = {h["file"] for h in repo_hygiene.find_stray_files(str(git_repo))}

    assert flagged == {"index.php.bak", "config.orig", "notes.txt~", "sub/.DS_Store"}


# --- debug / test scripts ----------------------------------------------------

def test_debug_and_test_scripts_are_classified(git_repo):
    write(git_repo, "debug.php", "<?php echo 1;")
    write(git_repo, "info.php", "<?php phpinfo(); ?>")
    write(git_repo, "test_login.php", "<?php echo 1;")
    write(git_repo, "testimonials.php", "<?php echo 1;")    # not a test script
    write(git_repo, "tests/test_auth.php", "<?php echo 1;")  # real test dir, not served
    git(git_repo, "add", "-A")

    hits = {h["file"]: h for h in repo_hygiene.find_debug_scripts(str(git_repo))}

    assert set(hits) == {"debug.php", "info.php", "test_login.php"}
    assert hits["debug.php"]["kind"] == "debug"
    assert hits["info.php"]["kind"] == "debug" and hits["info.php"]["phpinfo"] is True
    assert hits["test_login.php"]["kind"] == "test"


# --- broken PHP includes -----------------------------------------------------

def _targets(hits: list[dict]) -> dict[str, dict]:
    # detail reads: "require/include target '<path>' does not exist ..."
    return {h["detail"].split("'")[1]: h for h in hits}


def test_broken_includes_skip_existing_and_remote_targets(tmp_path):
    write(tmp_path, "includes/db.php", "<?php // exists")
    write(tmp_path, "index.php", "<?php\nrequire_once 'includes/db.php';\n")
    write(tmp_path, "admin/panel.php", "<?php\ninclude('../includes/db.php');\nrequire 'lib/missing.php';\n")
    write(tmp_path, "remote.php", "<?php include 'https://example.com/x.php';")

    hits = _targets(repo_hygiene.find_broken_php_includes(str(tmp_path)))

    assert set(hits) == {"lib/missing.php"}
    assert hits["lib/missing.php"]["file"] == "admin/panel.php"
    assert hits["lib/missing.php"]["line"] == 3


def test_same_missing_include_from_many_files_is_one_finding(tmp_path):
    # One missing shared include pulled in from a dozen pages is one problem
    # with one fix - not twelve findings drowning out everything else.
    for i in range(12):
        write(tmp_path, f"page{i:02d}.php", "<?php\ninclude 'includes/user_nav.php';\n")
    write(tmp_path, "other.php", "<?php\nrequire 'includes/footer.php';\n")

    hits = _targets(repo_hygiene.find_broken_php_includes(str(tmp_path)))

    assert set(hits) == {"includes/user_nav.php", "includes/footer.php"}
    nav = hits["includes/user_nav.php"]
    assert "Referenced from 12 locations" in nav["detail"]
    assert "(+3 more)" in nav["detail"]  # 11 other call sites, 8 listed
    assert nav["line"] == 2
    assert "Referenced from" not in hits["includes/footer.php"]["detail"]
