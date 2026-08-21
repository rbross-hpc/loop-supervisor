import subprocess
from pathlib import Path

from loop_supervisor.cli import build_parser, cmd_init_copy, cmd_init_in_place


def _run_git(args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _make_fake_template(root: Path) -> None:
    (root / "src" / "loop_supervisor").mkdir(parents=True)
    (root / ".opencode" / "agents").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "src" / "loop_supervisor" / "__init__.py").write_text("")
    (root / ".opencode" / "agents" / "loop-planner.md").write_text("agent\n")
    (root / ".env").write_text("SECRET=do-not-copy\n")
    (root / "README.md").write_text("template\n")


def _make_fake_git_template(root: Path) -> None:
    """A fake template checkout where only some files are actually
    tracked by git — copy-mode bootstrap must only ever copy those."""
    _make_fake_template(root)
    _run_git(["init", "-b", "main"], root)
    _run_git(["config", "user.email", "t@example.com"], root)
    _run_git(["config", "user.name", "T"], root)
    # Track everything except .env (mirroring this repo's real .gitignore).
    _run_git(["add", "pyproject.toml", "src", ".opencode", "README.md"], root)
    _run_git(["commit", "-m", "init"], root)
    # .env remains untracked/ignored — must never be copied.
    (root / "untracked-secret.json").write_text('{"token": "do-not-copy"}\n')
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("//\n")


def test_init_copy_excludes_env_and_git(tmp_path):
    source = tmp_path / "template"
    _make_fake_git_template(source)

    destination = tmp_path / "new-project"
    args = build_parser().parse_args(["init", "--destination", str(destination)])

    rc = cmd_init_copy(args, source=source)
    assert rc == 0
    assert not (destination / ".git").exists()
    assert not (destination / ".env").exists()
    assert (destination / "README.md").exists()
    assert (destination / ".opencode" / "agents" / "loop-planner.md").exists()


def test_init_copy_never_copies_untracked_files(tmp_path):
    source = tmp_path / "template"
    _make_fake_git_template(source)

    destination = tmp_path / "new-project"
    args = build_parser().parse_args(["init", "--destination", str(destination)])

    rc = cmd_init_copy(args, source=source)
    assert rc == 0
    assert not (destination / "untracked-secret.json").exists()
    assert not (destination / "node_modules").exists()


def test_init_copy_rejects_non_git_source(tmp_path):
    source = tmp_path / "template"
    _make_fake_template(source)

    destination = tmp_path / "new-project"
    args = build_parser().parse_args(["init", "--destination", str(destination)])

    rc = cmd_init_copy(args, source=source)
    assert rc == 1
    assert not destination.exists() or not any(destination.iterdir())


def test_init_copy_rejects_nonempty_destination(tmp_path):
    source = tmp_path / "template"
    _make_fake_git_template(source)
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "file.txt").write_text("already here\n")

    args = build_parser().parse_args(["init", "--destination", str(destination)])
    rc = cmd_init_copy(args, source=source)
    assert rc == 1


def test_init_in_place_requires_confirmation(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _make_fake_template(root)
    _run_git(["init", "-b", "main"], root)
    _run_git(["config", "user.email", "t@example.com"], root)
    _run_git(["config", "user.name", "T"], root)
    _run_git(["add", "-A"], root)
    _run_git(["commit", "-m", "init"], root)

    monkeypatch.setattr("builtins.input", lambda prompt="": "not-the-right-phrase")
    args = build_parser().parse_args(["init", "--in-place", "--project", str(root)])
    rc = cmd_init_in_place(args)

    assert rc == 1
    assert (root / ".git").exists()


def test_init_in_place_removes_git_with_yes(tmp_path):
    root = tmp_path / "project"
    _make_fake_template(root)
    _run_git(["init", "-b", "main"], root)
    _run_git(["config", "user.email", "t@example.com"], root)
    _run_git(["config", "user.name", "T"], root)
    _run_git(["add", "-A"], root)
    _run_git(["commit", "-m", "init"], root)

    args = build_parser().parse_args(["init", "--in-place", "--project", str(root), "--yes"])
    rc = cmd_init_in_place(args)

    assert rc == 0
    assert not (root / ".git").exists()
    assert (root / ".env").exists()
    assert (root / "README.md").exists()


def test_init_in_place_refuses_dirty_tree_without_force(tmp_path):
    root = tmp_path / "project"
    _make_fake_template(root)
    _run_git(["init", "-b", "main"], root)
    _run_git(["config", "user.email", "t@example.com"], root)
    _run_git(["config", "user.name", "T"], root)
    _run_git(["add", "-A"], root)
    _run_git(["commit", "-m", "init"], root)
    (root / "dirty.txt").write_text("uncommitted\n")

    args = build_parser().parse_args(["init", "--in-place", "--project", str(root), "--yes"])
    rc = cmd_init_in_place(args)

    assert rc == 1
    assert (root / ".git").exists()


def test_init_in_place_refuses_non_template_directory(tmp_path):
    root = tmp_path / "not-a-template"
    root.mkdir()
    (root / "somefile.txt").write_text("hi\n")

    args = build_parser().parse_args(["init", "--in-place", "--project", str(root), "--yes"])
    rc = cmd_init_in_place(args)
    assert rc == 1


def test_init_in_place_requires_git_dir(tmp_path):
    root = tmp_path / "project"
    _make_fake_template(root)

    args = build_parser().parse_args(["init", "--in-place", "--project", str(root), "--yes"])
    rc = cmd_init_in_place(args)
    assert rc == 1
